"""Global Bybit price collector, pump detection, and alert fan-out."""

import logging
import time

from pybit.unified_trading import HTTP as BybitHTTP

import db
from config import (
    ALERT_COOLDOWN_SECONDS,
    BYBIT_TRADE_URL,
    GLOBAL_TICK_INTERVAL,
)

logger = logging.getLogger(__name__)

# ── In-memory alert cooldown tracker ─────────────────────
# Key: (user_id, symbol, time_window)  →  last_alert_ts
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


# ── Pump detection ───────────────────────────────────────


def compute_pumps(
    current_prices: dict[str, float],
    time_window_minutes: int,
) -> dict[str, float]:
    """Return {symbol: pump_pct} for a given time window.

    Only symbols that have a historical price available are included.
    """
    target_ts = time.time() - time_window_minutes * 60
    old_prices = db.get_all_symbols_price_at(target_ts)

    pumps: dict[str, float] = {}
    for symbol, current in current_prices.items():
        old = old_prices.get(symbol)
        if old and old > 0:
            pct = (current / old - 1) * 100
            pumps[symbol] = round(pct, 2)
    return pumps


# ── Alert fan-out ────────────────────────────────────────


async def collect_and_alert(bot) -> None:
    """Run one collection cycle: fetch prices → detect pumps → send alerts."""
    prices = fetch_mark_prices()
    if not prices:
        return

    # 1. Store prices
    db.save_prices(prices)

    # 2. Purge old data
    db.purge_old()

    current_map = {sym: px for sym, px in prices}

    # 3. Get active users and their distinct time windows
    users = db.get_all_active_users()
    if not users:
        return

    windows = {u["time_window"] for u in users}

    # 4. Compute pumps per window
    pump_results: dict[int, dict[str, float]] = {}
    for w in windows:
        pump_results[w] = compute_pumps(current_map, w)

    # 5. Fan-out alerts
    now = time.time()
    for user in users:
        uid = user["user_id"]
        threshold = user["pump_threshold"]
        tw = user["time_window"]
        pumps = pump_results.get(tw, {})

        for symbol, pct in pumps.items():
            if pct >= threshold:
                key = (uid, symbol, tw)
                last = _alert_cooldown.get(key, 0)
                if now - last < ALERT_COOLDOWN_SECONDS:
                    continue  # suppress duplicate alert

                _alert_cooldown[key] = now

                # Build readable coin name (strip trailing "USDT")
                coin = symbol.replace("USDT", "")
                url = BYBIT_TRADE_URL.format(symbol=symbol)
                text = f"🟢Pump: [{coin}]({url}): {pct}%"

                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=text,
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send alert to user %s", uid)


def cleanup_cooldown_cache() -> None:
    """Remove stale entries from the cooldown dict."""
    cutoff = time.time() - ALERT_COOLDOWN_SECONDS * 2
    stale = [k for k, ts in _alert_cooldown.items() if ts < cutoff]
    for k in stale:
        del _alert_cooldown[k]
