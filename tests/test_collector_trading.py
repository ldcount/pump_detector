"""Tests for detection-time trade triggering in collector.py.

Run:  python -m unittest discover tests -v
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import db
import collector
import trading

ADMIN_ID = 111
OTHER_ID = 222


class FailingBot:
    """Bot whose Telegram sends always fail."""

    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kw):
        raise RuntimeError("telegram down")


class OkBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))


class CollectorTradingTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

        # Admin: pump 10% over 15 min
        db.upsert_user(
            ADMIN_ID,
            is_setup_complete=1,
            is_paused=0,
            paused_until_ts=None,
            pump_threshold=10.0,
            pump_time_window=15,
            dump_threshold=10.0,
            dump_time_window=15,
            cooldown_time=30,
            trading_enabled=1,
        )

        self._orig_admin_cfg = config.ADMIN_TELEGRAM_ID
        self._orig_admin_col = collector.ADMIN_TELEGRAM_ID
        config.ADMIN_TELEGRAM_ID = ADMIN_ID
        collector.ADMIN_TELEGRAM_ID = ADMIN_ID

        # Record trade attempts instead of hitting trading logic
        self.trade_calls = []
        self._orig_try_open = trading.try_open_trade

        async def record_trade(bot, symbol, trigger_price):
            self.trade_calls.append((symbol, trigger_price))

        trading.try_open_trade = record_trade
        collector._inflight_trade_symbols.clear()

    def tearDown(self):
        trading.try_open_trade = self._orig_try_open
        collector.ADMIN_TELEGRAM_ID = self._orig_admin_col
        config.ADMIN_TELEGRAM_ID = self._orig_admin_cfg
        collector._inflight_trade_symbols.clear()
        db.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ── helpers ──────────────────────────────────────────

    def seed_history(self, symbol, price, minutes_ago):
        """Insert a historical price row."""
        ts = time.time() - minutes_ago * 60
        with db._conn() as con:
            con.execute(
                "INSERT INTO price_log (symbol, price, timestamp) VALUES (?, ?, ?)",
                (symbol, price, ts),
            )

    def run_cycle(self, bot, prices):
        """Run one collect_and_alert cycle with fetch_mark_prices stubbed."""
        orig_fetch = collector.fetch_mark_prices
        collector.fetch_mark_prices = lambda: prices
        try:
            async def main():
                await collector.collect_and_alert(bot)
                # Let spawned dispatch/trade tasks finish
                for _ in range(10):
                    pending = [t for t in collector._dispatch_tasks if not t.done()]
                    if not pending:
                        break
                    await asyncio.gather(*pending, return_exceptions=True)
            asyncio.run(main())
        finally:
            collector.fetch_mark_prices = orig_fetch

    # ── tests ────────────────────────────────────────────

    def test_pump_signal_triggers_trade_with_tick_price(self):
        self.seed_history("AAAUSDT", 1.0, minutes_ago=10)
        self.run_cycle(OkBot(), [("AAAUSDT", 1.25)])  # +25% pump
        self.assertEqual(self.trade_calls, [("AAAUSDT", 1.25)])

    def test_trade_fires_even_when_telegram_send_fails(self):
        self.seed_history("AAAUSDT", 1.0, minutes_ago=10)
        self.run_cycle(FailingBot(), [("AAAUSDT", 1.25)])
        self.assertEqual(self.trade_calls, [("AAAUSDT", 1.25)])

    def test_dump_signal_does_not_trigger_trade(self):
        self.seed_history("BBBUSDT", 1.0, minutes_ago=10)
        self.run_cycle(OkBot(), [("BBBUSDT", 0.75)])  # -25% dump
        self.assertEqual(self.trade_calls, [])

    def test_non_admin_pump_does_not_trigger_trade(self):
        db.upsert_user(
            OTHER_ID,
            is_setup_complete=1, is_paused=0,
            pump_threshold=10.0, pump_time_window=15,
            dump_threshold=10.0, dump_time_window=15,
            cooldown_time=30,
        )
        # Pause the admin so only the other user gets the alert
        db.upsert_user(ADMIN_ID, is_paused=1)
        self.seed_history("CCCUSDT", 1.0, minutes_ago=10)
        self.run_cycle(OkBot(), [("CCCUSDT", 1.25)])
        self.assertEqual(self.trade_calls, [])

    def test_below_threshold_no_trade(self):
        self.seed_history("DDDUSDT", 1.0, minutes_ago=10)
        self.run_cycle(OkBot(), [("DDDUSDT", 1.05)])  # +5% < 10%
        self.assertEqual(self.trade_calls, [])

    def test_alert_cooldown_gates_repeat_signals(self):
        self.seed_history("EEEUSDT", 1.0, minutes_ago=10)
        bot = OkBot()
        self.run_cycle(bot, [("EEEUSDT", 1.25)])
        # Cooldown was written on successful send; same pump next tick → no new signal
        self.run_cycle(bot, [("EEEUSDT", 1.30)])
        self.assertEqual(self.trade_calls, [("EEEUSDT", 1.25)])

    def test_inflight_guard_blocks_overlapping_attempts(self):
        """A still-running trade for a symbol suppresses a second spawn."""
        self.seed_history("FFFUSDT", 1.0, minutes_ago=10)

        calls = self.trade_calls

        async def slow_trade(bot, symbol, trigger_price):
            calls.append((symbol, trigger_price))
            await asyncio.sleep(0.2)

        trading.try_open_trade = slow_trade

        orig_fetch = collector.fetch_mark_prices
        collector.fetch_mark_prices = lambda: [("FFFUSDT", 1.25)]
        try:
            async def main():
                bot = FailingBot()  # sends fail → no cooldown written
                await collector.collect_and_alert(bot)
                await asyncio.sleep(0.05)  # trade still running
                await collector.collect_and_alert(bot)  # same signal again
                await asyncio.sleep(0.3)  # let everything finish
            asyncio.run(main())
        finally:
            collector.fetch_mark_prices = orig_fetch

        self.assertEqual(calls, [("FFFUSDT", 1.25)])

    def test_inflight_slot_released_after_trade_finishes(self):
        self.seed_history("GGGUSDT", 1.0, minutes_ago=10)
        bot = FailingBot()  # no cooldown → signal repeats each tick
        self.run_cycle(bot, [("GGGUSDT", 1.25)])
        self.run_cycle(bot, [("GGGUSDT", 1.30)])
        # Both attempts went through because the first finished before the second tick
        self.assertEqual(self.trade_calls, [("GGGUSDT", 1.25), ("GGGUSDT", 1.30)])


if __name__ == "__main__":
    unittest.main()
