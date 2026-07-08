"""Tests for trading.py with a mocked pybit session and a temp SQLite DB.

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
import trading

ADMIN_ID = 111


class FakeSession:
    """Canned-response stand-in for pybit's HTTP session."""

    def __init__(self):
        self.positions = []        # list of {"symbol", "size", ...}
        self.open_orders = []      # list of {"orderStatus", "orderId", ...}
        self.order_history = []    # list of {"orderStatus", "avgPrice", ...}
        self.closed_pnl = []       # list of {"closedPnl", "avgExitPrice", "orderId"}
        self.instrument = {
            "priceFilter": {"tickSize": "0.001"},
            "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"},
            "leverageFilter": {"maxLeverage": "50.00"},
        }
        self.calls = []            # (method_name, kwargs) in call order
        self.place_order_result = {"result": {"orderId": "OID-1"}}
        self.raise_on = {}         # method_name -> exception to raise

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if name in self.raise_on:
            raise self.raise_on[name]

    def calls_to(self, name):
        return [kw for n, kw in self.calls if n == name]

    def get_positions(self, **kw):
        self._record("get_positions", kw)
        return {"result": {"list": self.positions}}

    def get_open_orders(self, **kw):
        self._record("get_open_orders", kw)
        return {"result": {"list": self.open_orders}}

    def get_instruments_info(self, **kw):
        self._record("get_instruments_info", kw)
        return {"result": {"list": [self.instrument]}}

    def set_leverage(self, **kw):
        self._record("set_leverage", kw)
        return {"result": {}}

    def switch_position_mode(self, **kw):
        self._record("switch_position_mode", kw)
        return {"result": {}}

    def place_order(self, **kw):
        self._record("place_order", kw)
        return self.place_order_result

    def cancel_order(self, **kw):
        self._record("cancel_order", kw)
        return {"result": {}}

    def get_order_history(self, **kw):
        self._record("get_order_history", kw)
        return {"result": {"list": self.order_history}}

    def get_closed_pnl(self, **kw):
        self._record("get_closed_pnl", kw)
        return {"result": {"list": self.closed_pnl}}


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))


class TradingTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()
        db.upsert_user(
            ADMIN_ID,
            is_setup_complete=1,
            trading_enabled=1,
            is_paused=0,
            paused_until_ts=None,
            offset=1.0,
            short_size=300.0,
            tp_size=7.0,
            order_ttl=10,
            sl_size=7.0,
            max_open_positions=5,
        )

        self._orig_admin = config.ADMIN_TELEGRAM_ID
        config.ADMIN_TELEGRAM_ID = ADMIN_ID

        self.session = FakeSession()
        self._orig_session = trading._session
        trading._session = self.session

        self.bot = FakeBot()

    def tearDown(self):
        trading._session = self._orig_session
        config.ADMIN_TELEGRAM_ID = self._orig_admin
        db.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ── helpers ──────────────────────────────────────────

    def open_trade(self, symbol="AAAUSDT", trigger_price=2.0):
        asyncio.run(trading.try_open_trade(self.bot, symbol, trigger_price))

    def manage(self):
        asyncio.run(trading.manage_active_trades(self.bot))

    def all_texts(self):
        return "\n---\n".join(t for _, t in self.bot.messages)

    # ── try_open_trade ───────────────────────────────────

    def test_happy_path_places_order_with_correct_params(self):
        self.open_trade("AAAUSDT", trigger_price=2.0)

        orders = self.session.calls_to("place_order")
        self.assertEqual(len(orders), 1)
        o = orders[0]
        # limit = 2.0 * 0.99 = 1.98 ; qty = 300 / 1.98 = 151.5 → step 0.1
        self.assertEqual(o["side"], "Sell")
        self.assertEqual(o["orderType"], "Limit")
        self.assertEqual(o["price"], "1.980")
        self.assertEqual(o["qty"], "151.5")
        # TP = 1.98 * 0.93 = 1.8414 → 1.841 ; SL = 1.98 * 1.07 = 2.1186 → 2.119
        self.assertEqual(o["takeProfit"], "1.841")
        self.assertEqual(o["stopLoss"], "2.119")
        self.assertEqual(o["positionIdx"], 0)
        self.assertTrue(o["orderLinkId"].startswith("pump_AAAUSDT_"))

        trade = db.get_trade(o["orderLinkId"])
        self.assertEqual(trade["status"], "pending")
        self.assertEqual(trade["order_id"], "OID-1")
        self.assertIn("Short Order Placed", self.all_texts())

    def test_leverage_clamped_to_symbol_max(self):
        self.session.instrument["leverageFilter"]["maxLeverage"] = "5.00"
        self.open_trade()
        lev = self.session.calls_to("set_leverage")
        self.assertEqual(len(lev), 1)
        self.assertEqual(lev[0]["buyLeverage"], "5")
        self.assertEqual(lev[0]["sellLeverage"], "5")

    def test_leverage_defaults_to_10_when_symbol_allows_more(self):
        self.open_trade()
        lev = self.session.calls_to("set_leverage")
        self.assertEqual(lev[0]["buyLeverage"], "10")

    def test_skip_when_position_exists(self):
        self.session.positions = [{"symbol": "AAAUSDT", "size": "10"}]
        self.open_trade("AAAUSDT")
        self.assertEqual(self.session.calls_to("place_order"), [])
        self.assertIn("Position already exists", self.all_texts())
        self.assertEqual(db.get_active_trades(), [])

    def test_skip_when_pending_order_exists(self):
        self.session.open_orders = [{"orderStatus": "New", "orderId": "X"}]
        self.open_trade("AAAUSDT")
        self.assertEqual(self.session.calls_to("place_order"), [])
        self.assertIn("Active pending order already exists", self.all_texts())

    def test_skip_when_cap_reached(self):
        self.session.positions = [
            {"symbol": f"S{i}USDT", "size": "1"} for i in range(5)
        ]
        self.open_trade("AAAUSDT")
        self.assertEqual(self.session.calls_to("place_order"), [])
        self.assertIn("Max open positions cap", self.all_texts())

    def test_skip_when_below_min_notional(self):
        self.session.instrument["lotSizeFilter"]["minNotionalValue"] = "1000"
        self.open_trade()
        self.assertEqual(self.session.calls_to("place_order"), [])
        self.assertIn("Trade size too small", self.all_texts())

    def test_no_trade_when_trading_disabled(self):
        db.upsert_user(ADMIN_ID, trading_enabled=0)
        self.open_trade()
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.bot.messages, [])

    def test_no_trade_when_paused(self):
        db.upsert_user(ADMIN_ID, is_paused=1)
        self.open_trade()
        self.assertEqual(self.session.calls, [])

    def test_no_trade_when_timed_pause_active(self):
        db.upsert_user(ADMIN_ID, paused_until_ts=time.time() + 3600)
        self.open_trade()
        self.assertEqual(self.session.calls, [])

    def test_order_error_marks_trade_error_and_notifies(self):
        self.session.raise_on["place_order"] = RuntimeError("boom")
        self.open_trade("AAAUSDT")
        trades = [db.get_trade(kw["orderLinkId"]) for kw in [self.session.calls_to("place_order")[0]]]
        self.assertEqual(trades[0]["status"], "error")
        self.assertIn("Failed to place order", self.all_texts())

    # ── manage_active_trades ─────────────────────────────

    def _seed_trade(self, status, symbol="BBBUSDT", ts=None, order_id="OID-9"):
        link = f"pump_{symbol}_{int(ts or time.time())}"
        db.create_trade(
            symbol=symbol, trigger_price=2.0, timestamp=ts or time.time(),
            order_link_id=link, qty=100.0, status="pending",
            tp_price=1.8, sl_price=2.2,
        )
        db.update_trade(link, status=status, order_id=order_id)
        return link

    def test_ttl_expiry_cancels_order(self):
        link = self._seed_trade("pending", ts=time.time() - 700)  # ttl 10 min
        self.session.open_orders = [{"orderStatus": "New", "orderId": "OID-9"}]
        self.manage()
        self.assertEqual(len(self.session.calls_to("cancel_order")), 1)
        self.assertEqual(db.get_trade(link)["status"], "cancelled")
        self.assertIn("Order Expired (TTL)", self.all_texts())

    def test_young_pending_order_not_cancelled(self):
        self._seed_trade("pending", ts=time.time() - 60)
        self.session.open_orders = [{"orderStatus": "New", "orderId": "OID-9"}]
        self.manage()
        self.assertEqual(self.session.calls_to("cancel_order"), [])

    def test_fill_detected_via_history(self):
        link = self._seed_trade("pending")
        self.session.open_orders = []
        self.session.order_history = [{"orderStatus": "Filled", "avgPrice": "1.99"}]
        self.manage()
        trade = db.get_trade(link)
        self.assertEqual(trade["status"], "open")
        self.assertEqual(trade["entry_price"], 1.99)
        self.assertIn("Entry Order Filled", self.all_texts())

    def test_tp_hit_classified_and_pnl_recorded(self):
        link = self._seed_trade("open", symbol="CCCUSDT")
        self.session.positions = []  # position gone
        self.session.closed_pnl = [{"closedPnl": "21.5", "avgExitPrice": "1.85", "orderId": "CLOSE-1"}]
        self.session.order_history = [{"orderStatus": "Filled", "stopOrderType": "TakeProfit"}]
        self.manage()
        trade = db.get_trade(link)
        self.assertEqual(trade["status"], "tp_hit")
        self.assertEqual(trade["realized_pnl"], 21.5)
        self.assertIn("Take Profit Hit", self.all_texts())
        self.assertAlmostEqual(db.get_realized_pnl_today(), 21.5)

    def test_sl_hit_classified(self):
        link = self._seed_trade("open", symbol="DDDUSDT")
        self.session.positions = []
        self.session.closed_pnl = [{"closedPnl": "-21.0", "avgExitPrice": "2.14", "orderId": "CLOSE-2"}]
        self.session.order_history = [{"orderStatus": "Filled", "stopOrderType": "StopLoss"}]
        self.manage()
        self.assertEqual(db.get_trade(link)["status"], "sl_hit")
        self.assertIn("Stop Loss Hit", self.all_texts())

    def test_open_position_still_alive_untouched(self):
        link = self._seed_trade("open", symbol="EEEUSDT")
        self.session.positions = [{"symbol": "EEEUSDT", "size": "100"}]
        self.manage()
        self.assertEqual(db.get_trade(link)["status"], "open")
        self.assertEqual(self.bot.messages, [])

    def test_manage_noop_without_active_trades(self):
        self.manage()
        self.assertEqual(self.session.calls, [])

    # ── event-loop safety ────────────────────────────────

    def test_slow_api_does_not_block_event_loop(self):
        """While try_open_trade waits on a slow Bybit call, other coroutines must run."""
        loop_ticks = []

        orig = self.session.get_positions
        def slow_get_positions(**kw):
            time.sleep(0.5)
            return orig(**kw)
        self.session.get_positions = slow_get_positions

        async def ticker():
            for _ in range(5):
                loop_ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        async def main():
            await asyncio.gather(
                trading.try_open_trade(self.bot, "AAAUSDT", 2.0),
                ticker(),
            )

        asyncio.run(main())
        # If the loop were blocked for 0.5s, the ticker gaps would show it.
        gaps = [b - a for a, b in zip(loop_ticks, loop_ticks[1:])]
        self.assertLess(max(gaps), 0.3, f"event loop stalled: gaps={gaps}")


if __name__ == "__main__":
    unittest.main()
