"""Global Bybit price collector, pump/dump detection, and alert fan-out."""

import asyncio
import logging
import time
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

# ── In-memory alert cooldown tracker ─────────────────────
# Key: (user_id, symbol)  →  last_alert_ts
_alert_cooldown: dict[tuple, float] = {}
_dispatch_tasks: set[asyncio.Task] = set()
_send_semaphore: asyncio.Semaphore | None = None


# ── Bybit helpers ────────────────────────────────────────

_bybit = BybitHTTP(timeout=BYBIT_HTTP_TIMEOUT)


@dataclass(frozen=True, slots=True)
class Alert:
    """Outgoing Telegram alert."""

    user_id: int
    text: str


def _get_send_semaphore() -> asyncio.Semaphore:
    """Lazily create a shared limiter for outgoing Telegram sends."""
    global _send_semaphore
    if _send_semaphore is None:
        _send_semaphore = asyncio.Semaphore(TELEGRAM_SEND_CONCURRENCY)
    return _send_semaphore


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
        alerts.extend(_collect_pump_alerts(user, change_results, now, uid))
        alerts.extend(_collect_dump_alerts(user, change_results, now, uid))
    return alerts


def _collect_pump_alerts(user, change_results, now, uid) -> list[Alert]:
    """Build pump alerts for a single user."""
    threshold = user["pump_threshold"]
    tw = user["pump_time_window"]
    changes = change_results.get(tw, {})
    alerts: list[Alert] = []

    for symbol, data in changes.items():
        pct = data.get("pump", 0)
        if pct >= threshold:
            key = (uid, symbol)
            last = _alert_cooldown.get(key, 0)
            cooldown_seconds = user.get("cooldown_time", 30) * 60
            if now - last < cooldown_seconds:
                continue

            _alert_cooldown[key] = now
            coin = symbol.replace("USDT", "")
            bybit_url = BYBIT_TRADE_URL.format(symbol=symbol)
            coinglass_url = COINGLASS_URL.format(symbol=symbol)
            signal_count = db.increment_and_get_daily_alert_count(uid)

            text = (
                f"🏦[ByBit]({bybit_url}) – {tw}m – [{coin}]({coinglass_url})\n"
                f"🟢*Pump*: *{pct}%*\n"
                f"#️⃣Signal 24h: {signal_count}"
            )
            alerts.append(Alert(user_id=uid, text=text))
    return alerts


def _collect_dump_alerts(user, change_results, now, uid) -> list[Alert]:
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
            last = _alert_cooldown.get(key, 0)
            cooldown_seconds = user.get("cooldown_time", 30) * 60
            if now - last < cooldown_seconds:
                continue

            _alert_cooldown[key] = now
            coin = symbol.replace("USDT", "")
            bybit_url = BYBIT_TRADE_URL.format(symbol=symbol)
            coinglass_url = COINGLASS_URL.format(symbol=symbol)
            signal_count = db.increment_and_get_daily_alert_count(uid)

            text = (
                f"🏦[ByBit]({bybit_url}) – {tw}m – [{coin}]({coinglass_url})\n"
                f"🔴*Dump*: *{pct}%*\n"
                f"#️⃣Signal 24h: {signal_count}"
            )
            alerts.append(Alert(user_id=uid, text=text))
    return alerts


async def _dispatch_alerts(bot, alerts: list[Alert]) -> None:
    """Send all alerts concurrently without blocking the collection cycle."""
    await asyncio.gather(*(_send_alert(bot, alert) for alert in alerts))


async def _send_alert(bot, alert: Alert) -> None:
    """Send a single alert under a shared rate limiter."""
    semaphore = _get_send_semaphore()
    async with semaphore:
        try:
            await bot.send_message(
                chat_id=alert.user_id,
                text=alert.text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Failed to send alert to user %s", alert.user_id)


def cleanup_cooldown_cache() -> None:
    """Remove stale entries from the cooldown dict (older than 24 hours)."""
    cutoff = time.time() - 86400
    stale = [k for k, ts in _alert_cooldown.items() if ts < cutoff]
    for k in stale:
        del _alert_cooldown[k]
