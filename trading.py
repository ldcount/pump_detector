import logging
import os
import time
from pybit.unified_trading import HTTP
from pybit.exceptions import InvalidRequestError
import db
import config

logger = logging.getLogger(__name__)

_session = None

def get_session():
    global _session
    if _session is None:
        api_key = os.getenv("BYBIT_API_KEY")
        api_secret = os.getenv("BYBIT_API_SECRET")
        _session = HTTP(testnet=False, api_key=api_key, api_secret=api_secret)
    return _session

def get_decimal_places(step: float) -> int:
    step_str = f"{step:.8f}".rstrip('0')
    if '.' not in step_str:
        return 0
    return len(step_str.split('.')[1])

def round_step(val: float, step: float) -> float:
    decimals = get_decimal_places(step)
    return round(round(val / step) * step, decimals)

async def try_open_trade(bot, symbol: str, trigger_price: float) -> None:
    # 1. Check if admin ID is set
    admin_id = config.ADMIN_TELEGRAM_ID
    if not admin_id:
        logger.error("ADMIN_TELEGRAM_ID is not set in config/env")
        return

    # 2. Get admin user settings
    admin_settings = db.get_user(admin_id)
    if not admin_settings or not admin_settings.get("is_setup_complete"):
        return

    # 3. Check if trading is enabled and not paused
    if not admin_settings.get("trading_enabled"):
        return

    is_paused = admin_settings.get("is_paused", 0)
    paused_until_ts = admin_settings.get("paused_until_ts")
    if is_paused or (paused_until_ts and paused_until_ts > time.time()):
        return

    # 4. Check position constraints
    try:
        session = get_session()
        pos_res = session.get_positions(category="linear", settleCoin="USDT")
        positions = pos_res.get("result", {}).get("list", [])
        
        # Count all open positions in the linear perps account (both bot and manual)
        open_positions = [p for p in positions if float(p.get("size", 0)) > 0]
        total_open_count = len(open_positions)
        
        # Check if symbol has an open position
        has_pos = any(p["symbol"] == symbol for p in open_positions)
        if has_pos:
            await bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ [Trade Skipped] Position already exists for {symbol}."
            )
            return

        # Check if there is an active pending entry order for this symbol
        orders_res = session.get_open_orders(category="linear", symbol=symbol)
        open_orders = [o for o in orders_res.get("result", {}).get("list", []) if o.get("orderStatus") in ("New", "PartiallyFilled")]
        if open_orders:
            await bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ [Trade Skipped] Active pending order already exists for {symbol}."
            )
            return
            
        # Check max open positions cap
        max_pos_cap = admin_settings.get("max_open_positions", 5)
        if total_open_count >= max_pos_cap:
            await bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ [Trade Skipped] Max open positions cap ({max_pos_cap}) reached. Current positions: {total_open_count}."
            )
            return

    except Exception as e:
        logger.exception("Error checking Bybit positions/orders before opening trade")
        await bot.send_message(
            chat_id=admin_id,
            text=f"❌ [Trade Error] Failed to check Bybit status: {str(e)}"
        )
        return

    # 5. Fetch symbol constraints
    try:
        instr = session.get_instruments_info(category="linear", symbol=symbol)
        symbol_info = instr["result"]["list"][0]
        tick_size = float(symbol_info["priceFilter"]["tickSize"])
        qty_step = float(symbol_info["lotSizeFilter"]["qtyStep"])
        min_qty = float(symbol_info["lotSizeFilter"]["minOrderQty"])
        min_notional = float(symbol_info["lotSizeFilter"].get("minNotionalValue", 5.0))
    except Exception as e:
        logger.exception("Error fetching instrument info for %s", symbol)
        await bot.send_message(
            chat_id=admin_id,
            text=f"❌ [Trade Error] Failed to fetch instrument info for {symbol}: {str(e)}"
        )
        return

    # 6. Calculate trade parameters
    offset_pct = admin_settings.get("offset", 1.0)
    short_size_usdt = admin_settings.get("short_size", 100.0)
    tp_pct = admin_settings.get("tp_size", 5.0)
    sl_pct = admin_settings.get("sl_size", 5.0)

    limit_price = trigger_price * (1 - offset_pct / 100.0)
    limit_price = round_step(limit_price, tick_size)

    qty = short_size_usdt / limit_price
    qty = round_step(qty, qty_step)

    notional = qty * limit_price
    if qty < min_qty or notional < min_notional:
        msg = (
            f"⚠️ [Trade Skipped] Trade size too small for {symbol}.\n"
            f"Calculated Qty: {qty} (min: {min_qty})\n"
            f"Calculated Notional: {notional:.2f} USDT (min: {min_notional} USDT)"
        )
        await bot.send_message(chat_id=admin_id, text=msg)
        return

    tp_price = limit_price * (1 - tp_pct / 100.0)
    sl_price = limit_price * (1 + sl_pct / 100.0)

    tp_price = round_step(tp_price, tick_size)
    sl_price = round_step(sl_price, tick_size)

    signal_ts = int(time.time())
    order_link_id = f"pump_{symbol}_{signal_ts}"

    qty_dec = get_decimal_places(qty_step)
    price_dec = get_decimal_places(tick_size)
    qty_str = f"{qty:.{qty_dec}f}"
    price_str = f"{limit_price:.{price_dec}f}"
    tp_str = f"{tp_price:.{price_dec}f}"
    sl_str = f"{sl_price:.{price_dec}f}"

    # 7. Persist trade record as 'pending'
    try:
        db.create_trade(
            symbol=symbol,
            trigger_price=trigger_price,
            timestamp=signal_ts,
            order_link_id=order_link_id,
            qty=qty,
            status="pending",
            tp_price=tp_price,
            sl_price=sl_price,
        )
    except Exception as e:
        logger.exception("Error saving pending trade to database")
        await bot.send_message(
            chat_id=admin_id,
            text=f"❌ [Trade Error] Database write failed for {symbol}: {str(e)}"
        )
        return

    # 8. Configure Leverage & Position mode on Bybit
    try:
        try:
            session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage="10",
                sellLeverage="10",
            )
        except InvalidRequestError as le:
            if le.status_code != 110043 and "leverage not modified" not in str(le):
                raise le
        
        try:
            session.switch_position_mode(
                category="linear",
                symbol=symbol,
                mode=0,
            )
        except InvalidRequestError as pe:
            if pe.status_code != 110025 and "Position mode is not modified" not in str(pe):
                raise pe
    except Exception as e:
        logger.exception("Error setting leverage/position mode for %s", symbol)
        db.update_trade(order_link_id, status="error")
        await bot.send_message(
            chat_id=admin_id,
            text=f"❌ [Trade Error] Failed to set leverage/position mode for {symbol}: {str(e)}"
        )
        return

    # 9. Place Order
    try:
        res = session.place_order(
            category="linear",
            symbol=symbol,
            side="Sell",
            orderType="Limit",
            qty=qty_str,
            price=price_str,
            timeInForce="GTC",
            positionIdx=0,
            orderLinkId=order_link_id,
            takeProfit=tp_str,
            stopLoss=sl_str,
            tpTriggerBy="MarkPrice",
            slTriggerBy="MarkPrice",
        )
        order_id = res["result"]["orderId"]
        
        db.update_trade(order_link_id, order_id=order_id)
        
        msg = (
            f"🚀 *Short Order Placed*\n\n"
            f"🪙 Symbol: *{symbol.replace('USDT', '')}*\n"
            f"📉 Limit Price: *{price_str} USDT*\n"
            f"💰 Qty: *{qty_str}* (Notional ~{notional:.2f} USDT)\n"
            f"🎯 TP: *{tp_str} USDT* (-{tp_pct}%)\n"
            f"🛡 SL: *{sl_str} USDT* (+{sl_pct}%)\n"
            f"🔗 ID: `{order_id}`"
        )
        await bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
        
    except Exception as e:
        logger.exception("Error placing Bybit order for %s", symbol)
        db.update_trade(order_link_id, status="error")
        await bot.send_message(
            chat_id=admin_id,
            text=f"❌ [Trade Error] Failed to place order for {symbol}: {str(e)}"
        )

async def manage_active_trades(bot) -> None:
    admin_id = config.ADMIN_TELEGRAM_ID
    if not admin_id:
        return

    admin_settings = db.get_user(admin_id)
    if not admin_settings:
        return
    
    order_ttl_min = admin_settings.get("order_ttl", 10)

    active_trades = db.get_active_trades()
    if not active_trades:
        return

    try:
        session = get_session()
        pos_res = session.get_positions(category="linear", settleCoin="USDT")
        open_positions = {p["symbol"]: p for p in pos_res.get("result", {}).get("list", []) if float(p.get("size", 0)) > 0}
    except Exception as e:
        logger.exception("Error querying positions from Bybit in manage_active_trades")
        return

    now_ts = time.time()

    for trade in active_trades:
        symbol = trade["symbol"]
        order_link_id = trade["order_link_id"]
        order_id = trade["order_id"]
        trade_status = trade["status"]

        if trade_status == "pending":
            try:
                if order_id:
                    orders_res = session.get_open_orders(category="linear", symbol=symbol, orderId=order_id)
                else:
                    orders_res = session.get_open_orders(category="linear", symbol=symbol, orderLinkId=order_link_id)
                
                open_orders = orders_res.get("result", {}).get("list", [])
                # Filter for truly pending orders
                pending_orders = [o for o in open_orders if o.get("orderStatus") in ("New", "PartiallyFilled")]
                
                if pending_orders:
                    age_seconds = now_ts - trade["timestamp"]
                    if age_seconds > order_ttl_min * 60:
                        try:
                            session.cancel_order(category="linear", symbol=symbol, orderId=order_id or pending_orders[0]["orderId"])
                            db.update_trade(order_link_id, status="cancelled", closed_timestamp=now_ts)
                            await bot.send_message(
                                chat_id=admin_id,
                                text=f"⏳ *Order Expired (TTL)*\nPending entry order for *{symbol.replace('USDT', '')}* was cancelled after {order_ttl_min} minutes.",
                                parse_mode="Markdown"
                            )
                        except Exception as ce:
                            logger.exception("Error cancelling expired order %s", order_id)
                else:
                    hist_res = session.get_order_history(
                        category="linear",
                        symbol=symbol,
                        orderId=order_id if order_id else None,
                        orderLinkId=order_link_id if not order_id else None,
                    )
                    hist_list = hist_res.get("result", {}).get("list", [])
                    if hist_list:
                        hist_order = hist_list[0]
                        status = hist_order["orderStatus"]
                        if status == "Filled":
                            avg_price = float(hist_order.get("avgPrice") or hist_order.get("price") or 0.0)
                            db.update_trade(order_link_id, status="open", entry_price=avg_price)
                            await bot.send_message(
                                chat_id=admin_id,
                                text=f"✅ *Entry Order Filled*\n{symbol.replace('USDT', '')} short filled at *{avg_price} USDT*.",
                                parse_mode="Markdown"
                            )
                        elif status in ("Cancelled", "Deactivated"):
                            db.update_trade(order_link_id, status="cancelled", closed_timestamp=now_ts)
                            await bot.send_message(
                                chat_id=admin_id,
                                text=f"ℹ️ *Entry Order Cancelled*\n{symbol.replace('USDT', '')} entry order was cancelled.",
                                parse_mode="Markdown"
                            )
                        elif status == "Rejected":
                            db.update_trade(order_link_id, status="error", closed_timestamp=now_ts)
                            await bot.send_message(
                                chat_id=admin_id,
                                text=f"❌ *Entry Order Rejected*\n{symbol.replace('USDT', '')} entry order was rejected.",
                                parse_mode="Markdown"
                            )
            except Exception as e:
                logger.exception("Error polling pending trade %s", order_link_id)

        elif trade_status == "open":
            if symbol not in open_positions:
                try:
                    pnl_res = session.get_closed_pnl(category="linear", symbol=symbol, limit=1)
                    pnl_list = pnl_res.get("result", {}).get("list", [])
                    if pnl_list:
                        last_pnl = pnl_list[0]
                        closed_pnl = float(last_pnl.get("closedPnl", 0.0))
                        exit_price = float(last_pnl.get("avgExitPrice", 0.0))
                        closing_order_id = last_pnl.get("orderId")
                        
                        stop_type = None
                        if closing_order_id:
                            try:
                                close_order_res = session.get_order_history(
                                    category="linear",
                                    symbol=symbol,
                                    orderId=closing_order_id,
                                )
                                close_order_list = close_order_res.get("result", {}).get("list", [])
                                if close_order_list:
                                    close_order = close_order_list[0]
                                    stop_type = close_order.get("stopOrderType") or close_order.get("createType")
                            except Exception:
                                logger.exception("Failed to query closing order history for details")
                        
                        pnl_sign = "+" if closed_pnl >= 0 else ""
                        if stop_type == "TakeProfit" or (stop_type and "TakeProfit" in stop_type):
                            new_status = "tp_hit"
                            msg = f"🎯 *Take Profit Hit!*\n\n🪙 Symbol: *{symbol.replace('USDT', '')}*\n🚪 Exit Price: *{exit_price} USDT*\n💰 Realised PnL: *{pnl_sign}{closed_pnl:.4f} USDT*"
                        elif stop_type == "StopLoss" or (stop_type and "StopLoss" in stop_type):
                            new_status = "sl_hit"
                            msg = f"🛡 *Stop Loss Hit*\n\n🪙 Symbol: *{symbol.replace('USDT', '')}*\n🚪 Exit Price: *{exit_price} USDT*\n💰 Realised PnL: *{pnl_sign}{closed_pnl:.4f} USDT*"
                        else:
                            new_status = "closed"
                            msg = f"🚪 *Position Closed*\n\n🪙 Symbol: *{symbol.replace('USDT', '')}*\n🚪 Exit Price: *{exit_price} USDT*\n💰 Realised PnL: *{pnl_sign}{closed_pnl:.4f} USDT*"

                        db.update_trade(
                            order_link_id,
                            status=new_status,
                            realized_pnl=closed_pnl,
                            closed_timestamp=now_ts
                        )
                        await bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
                    else:
                        db.update_trade(order_link_id, status="closed", closed_timestamp=now_ts)
                        await bot.send_message(
                            chat_id=admin_id,
                            text=f"🚪 *Position Closed*\n{symbol.replace('USDT', '')} position is no longer active.",
                            parse_mode="Markdown"
                        )
                except Exception as e:
                    logger.exception("Error checking closed P&L for open trade %s", order_link_id)

def get_bot_positions_info():
    active_trades = db.get_active_trades()
    open_bot_trades = {t["symbol"]: t for t in active_trades if t["status"] == "open"}
    if not open_bot_trades:
        return []

    try:
        session = get_session()
        pos_res = session.get_positions(category="linear", settleCoin="USDT")
        positions = pos_res.get("result", {}).get("list", [])
        
        info = []
        for p in positions:
            sym = p["symbol"]
            size = float(p.get("size", 0))
            if sym in open_bot_trades and size > 0:
                trade = open_bot_trades[sym]
                info.append({
                    "symbol": sym,
                    "side": p["side"],
                    "size": size,
                    "avg_price": float(p.get("avgPrice", 0)),
                    "current_price": float(p.get("markPrice", 0)),
                    "unrealised_pnl": float(p.get("unrealisedPnl", 0)),
                    "tp_price": trade["tp_price"],
                    "sl_price": trade["sl_price"],
                })
        return info
    except Exception:
        logger.exception("Error getting bot positions info")
        return []
