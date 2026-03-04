"""Global Bybit price collector, pump/dump detection, and alert fan-out."""

import logging
import time

from pybit.unified_trading import HTTP as BybitHTTP

import db
from config import (
    ALERT_COOLDOWN_SECONDS,
    BYBIT_TRADE_URL,
)

logger = logging.getLogger(__name__)

# ── In-memory alert cooldown tracker ─────────────────────
# Key: (user_id, symbol, time_window, direction)  →  last_alert_ts
_alert_cooldown: dict[tuple, float] = {}


# ── Bybit helpers ────────────────────────────────────────

_bybit = BybitHTTP()


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
    prices = fetch_mark_prices()
    if not prices:
        return

    # 1. Store prices
    db.save_prices(prices)

    # 2. Purge old data
    db.purge_old()

    current_map = {sym: px for sym, px in prices}

    # 3. Get active users
    users = db.get_all_active_users()
    if not users:
        return

    # 4. Collect all distinct time windows (pump + dump)
    windows: set[int] = set()
    for u in users:
        windows.add(u["pump_time_window"])
        windows.add(u["dump_time_window"])

    # 5. Compute price changes per window
    change_results: dict[int, dict[str, dict[str, float]]] = {}
    for w in windows:
        change_results[w] = compute_price_changes(current_map, w)

    # 6. Fan-out alerts
    now = time.time()
    for user in users:
        uid = user["user_id"]
        await _check_pumps(bot, user, change_results, now, uid)
        await _check_dumps(bot, user, change_results, now, uid)


async def _check_pumps(bot, user, change_results, now, uid):
    """Send pump alerts for a single user."""
    threshold = user["pump_threshold"]
    tw = user["pump_time_window"]
    changes = change_results.get(tw, {})

    for symbol, data in changes.items():
        pct = data.get("pump", 0)
        if pct >= threshold:
            key = (uid, symbol, tw, "pump")
            last = _alert_cooldown.get(key, 0)
            if now - last < ALERT_COOLDOWN_SECONDS:
                continue

            _alert_cooldown[key] = now
            coin = symbol.replace("USDT", "")
            url = BYBIT_TRADE_URL.format(symbol=symbol)
            text = f"🟢Pump - {tw}m: [{coin}]({url}): {pct}%"

            try:
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Failed to send pump alert to user %s", uid)


async def _check_dumps(bot, user, change_results, now, uid):
    """Send dump alerts for a single user."""
    threshold = user["dump_threshold"]
    tw = user["dump_time_window"]
    changes = change_results.get(tw, {})

    for symbol, data in changes.items():
        pct = data.get("dump", 0)
        # Dump = negative change whose absolute value exceeds the threshold
        if pct <= -threshold:
            key = (uid, symbol, tw, "dump")
            last = _alert_cooldown.get(key, 0)
            if now - last < ALERT_COOLDOWN_SECONDS:
                continue

            _alert_cooldown[key] = now
            coin = symbol.replace("USDT", "")
            url = BYBIT_TRADE_URL.format(symbol=symbol)
            text = f"🔴Dump - {tw}m: [{coin}]({url}): {pct}%"

            try:
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Failed to send dump alert to user %s", uid)


def cleanup_cooldown_cache() -> None:
    """Remove stale entries from the cooldown dict."""
    cutoff = time.time() - ALERT_COOLDOWN_SECONDS * 2
    stale = [k for k, ts in _alert_cooldown.items() if ts < cutoff]
    for k in stale:
        del _alert_cooldown[k]
