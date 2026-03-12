"""Global Bybit price collector, pump/dump detection, and alert fan-out."""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from pybit.unified_trading import HTTP as BybitHTTP

import db
from config import (
    BYBIT_TRADE_URL,
    BYBIT_HTTP_TIMEOUT,
    COINGLASS_URL,
    TELEGRAM_SEND_CONCURRENCY,
)

logger = logging.getLogger(__name__)

_dispatch_tasks: set[asyncio.Task] = set()
_send_semaphore: asyncio.Semaphore | None = None
_user_send_locks: dict[int, asyncio.Lock] = {}


# ── Bybit helpers ────────────────────────────────────────

_bybit = BybitHTTP(timeout=BYBIT_HTTP_TIMEOUT)


@dataclass(frozen=True, slots=True)
class Alert:
    """Outgoing Telegram alert."""

    user_id: int
    symbol: str
    text_prefix: str


def _get_send_semaphore() -> asyncio.Semaphore:
    """Lazily create a shared limiter for outgoing Telegram sends."""
    global _send_semaphore
    if _send_semaphore is None:
        _send_semaphore = asyncio.Semaphore(TELEGRAM_SEND_CONCURRENCY)
    return _send_semaphore


def _get_user_send_lock(user_id: int) -> asyncio.Lock:
    """Return the per-user send lock."""
    lock = _user_send_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_send_locks[user_id] = lock
    return lock


def fetch_mark_prices() -> list[tuple[str, float]]:
    """Fetch mark prices for all USDT linear perpetuals on Bybit.

    Returns a list of (symbol, mark_price) tuples.
    """
    try:
        resp = _bybit.get_tickers(category="linear")
        tickers = resp.get("result", {}).get("list", [])
        prices: list[tuple[str, float]] = []
        for t in tickers:
            symbol = t.get("symbol", "")
            mark = t.get("markPrice")
            # Only USDT-margined perps
            if symbol.endswith("USDT") and mark:
                try:
                    prices.append((symbol, float(mark)))
                except (ValueError, TypeError):
                    continue
        return prices
    except Exception:
        logger.exception("Error fetching Bybit tickers")
        return []


# ── Price change detection ───────────────────────────────


def compute_price_changes(
    current_prices: dict[str, float],
    time_window_minutes: int,
) -> dict[str, dict[str, float]]:
    """Return {symbol: {'pump': pct, 'dump': pct}} for a given time window.

    Pump is calculated from the lowest price in the window up to the current price.
    Dump is calculated from the highest price in the window down to the current price.
    Only symbols that have a historical price available are included.
    """
    target_ts = time.time() - time_window_minutes * 60
    extremes = db.get_all_symbols_extremes_since(target_ts)

    changes: dict[str, dict[str, float]] = {}
    for symbol, current in current_prices.items():
        ext = extremes.get(symbol)
        if ext:
            min_p = ext.get("min")
            max_p = ext.get("max")
            pump_pct = (current / min_p - 1) * 100 if min_p and min_p > 0 else 0
            dump_pct = (current / max_p - 1) * 100 if max_p and max_p > 0 else 0
            changes[symbol] = {
                "pump": round(pump_pct, 2),
                "dump": round(dump_pct, 2),
            }
    return changes


# ── Alert fan-out ────────────────────────────────────────


async def collect_and_alert(bot) -> None:
    """Run one collection cycle: fetch prices → detect pumps/dumps → send alerts."""
    prices = await asyncio.to_thread(fetch_mark_prices)
    if not prices:
        return

    alerts = await asyncio.to_thread(_build_alert_batch, prices)
    if not alerts:
        return

    task = asyncio.create_task(_dispatch_alerts(bot, alerts))
    _dispatch_tasks.add(task)
    task.add_done_callback(_dispatch_tasks.discard)


def _build_alert_batch(prices: list[tuple[str, float]]) -> list[Alert]:
    """Persist a fresh market snapshot and build all due alerts."""
    db.save_prices(prices)
    db.purge_old()

    current_map = {sym: px for sym, px in prices}
    users = db.get_all_active_users()
    if not users:
        return []
    cooldown_map = db.get_alert_cooldown_map([user["user_id"] for user in users])

    windows: set[int] = set()
    for user in users:
        windows.add(user["pump_time_window"])
        windows.add(user["dump_time_window"])

    change_results: dict[int, dict[str, dict[str, float]]] = {}
    for window in windows:
        change_results[window] = compute_price_changes(current_map, window)

    now = time.time()
    alerts: list[Alert] = []
    for user in users:
        uid = user["user_id"]
        alerts.extend(_collect_pump_alerts(user, change_results, now, uid, cooldown_map))
        alerts.extend(_collect_dump_alerts(user, change_results, now, uid, cooldown_map))
    return alerts


def _collect_pump_alerts(user, change_results, now, uid, cooldown_map) -> list[Alert]:
    """Build pump alerts for a single user."""
    threshold = user["pump_threshold"]
    tw = user["pump_time_window"]
    changes = change_results.get(tw, {})
    alerts: list[Alert] = []

    for symbol, data in changes.items():
        pct = data.get("pump", 0)
        if pct >= threshold:
            key = (uid, symbol)
            last = cooldown_map.get(key, 0)
            cooldown_seconds = user.get("cooldown_time", 30) * 60
            if now - last < cooldown_seconds:
                continue

            coin = symbol.replace("USDT", "")
            bybit_url = BYBIT_TRADE_URL.format(symbol=symbol)
            coinglass_url = COINGLASS_URL.format(symbol=symbol)
            text_prefix = (
                f"🏦[ByBit]({bybit_url}) – {tw}m – [{coin}]({coinglass_url})\n"
                f"🟢*Pump*: *{pct}%*"
            )
            alerts.append(Alert(user_id=uid, symbol=symbol, text_prefix=text_prefix))
    return alerts


def _collect_dump_alerts(user, change_results, now, uid, cooldown_map) -> list[Alert]:
    """Build dump alerts for a single user."""
    threshold = user["dump_threshold"]
    tw = user["dump_time_window"]
    changes = change_results.get(tw, {})
    alerts: list[Alert] = []

    for symbol, data in changes.items():
        pct = data.get("dump", 0)
        # Dump = negative change whose absolute value exceeds the threshold
        if pct <= -threshold:
            key = (uid, symbol)
            last = cooldown_map.get(key, 0)
            cooldown_seconds = user.get("cooldown_time", 30) * 60
            if now - last < cooldown_seconds:
                continue

            coin = symbol.replace("USDT", "")
            bybit_url = BYBIT_TRADE_URL.format(symbol=symbol)
            coinglass_url = COINGLASS_URL.format(symbol=symbol)
            text_prefix = (
                f"🏦[ByBit]({bybit_url}) – {tw}m – [{coin}]({coinglass_url})\n"
                f"🔴*Dump*: *{pct}%*"
            )
            alerts.append(Alert(user_id=uid, symbol=symbol, text_prefix=text_prefix))
    return alerts


async def _dispatch_alerts(bot, alerts: list[Alert]) -> None:
    """Send all alerts concurrently without blocking the collection cycle."""
    alerts_by_user: dict[int, list[Alert]] = defaultdict(list)
    for alert in alerts:
        alerts_by_user[alert.user_id].append(alert)
    await asyncio.gather(
        *(
            _send_user_alerts(bot, user_id, user_alerts)
            for user_id, user_alerts in alerts_by_user.items()
        )
    )


async def _send_user_alerts(bot, user_id: int, alerts: list[Alert]) -> None:
    """Send a single user's alerts in order so counts stay accurate."""
    lock = _get_user_send_lock(user_id)
    async with lock:
        current_count = await asyncio.to_thread(db.get_daily_alert_count, user_id)
        for alert in alerts:
            next_count = current_count + 1
            text = f"{alert.text_prefix}\n#️⃣Signal 24h: {next_count}"
            if await _send_alert(bot, alert.user_id, text):
                current_count = await asyncio.to_thread(
                    db.increment_and_get_daily_alert_count, user_id
                )
                await asyncio.to_thread(
                    db.set_alert_cooldown, user_id, alert.symbol, time.time()
                )


async def _send_alert(bot, user_id: int, text: str) -> bool:
    """Send a single alert under a shared rate limiter."""
    semaphore = _get_send_semaphore()
    async with semaphore:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return True
        except Exception:
            logger.exception("Failed to send alert to user %s", user_id)
            return False


def cleanup_cooldown_cache() -> None:
    """Remove stale persisted cooldowns (older than 24 hours)."""
    db.purge_old_cooldowns()
