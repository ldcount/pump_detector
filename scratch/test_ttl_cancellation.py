import os
import sys
import asyncio
import time
from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db
import config
import trading

load_dotenv()

class MockBot:
    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None, reply_markup=None):
        safe_text = text.encode('ascii', errors='replace').decode('ascii')
        print(f"\n[Mock Bot Notification for User {chat_id}]:\n{safe_text}\n")
        return True

async def main():
    db.init_db()
    
    admin_id = config.ADMIN_TELEGRAM_ID
    if not admin_id:
        print("Error: ADMIN_TELEGRAM_ID not set in env.")
        return
        
    print(f"Setting up admin user {admin_id} settings in DB...")
    db.upsert_user(
        admin_id,
        offset=1.0,
        short_size=6.0,
        tp_size=5.0,
        sl_size=5.0,
        order_ttl=1,
        max_open_positions=5,
        trading_enabled=1,
        is_paused=0,
        paused_until_ts=None,
        is_setup_complete=1,
    )
    
    symbol = "ADAUSDT"
    session = trading.get_session()
    tickers = session.get_tickers(category="linear", symbol=symbol)
    mark_price = float(tickers["result"]["list"][0]["markPrice"])
    fake_trigger_price = mark_price * 1.20
    
    bot = MockBot()
    print(f"\nPlacing order for TTL test...")
    await trading.try_open_trade(bot, symbol, fake_trigger_price)
    
    active_trades = db.get_active_trades()
    if not active_trades:
        print("Error: Trade not created.")
        return
        
    trade = active_trades[-1]
    order_link_id = trade["order_link_id"]
    order_id = trade["order_id"]
    
    # Modify timestamp in DB to be 2 minutes ago
    expired_ts = time.time() - 120
    db.update_trade(order_link_id, timestamp=expired_ts)
    
    # Run manage_active_trades
    print("\nRunning manage_active_trades poller...")
    await trading.manage_active_trades(bot)
    
    # Wait 1.5 seconds for exchange state synchronization
    print("\nWaiting 1.5s for Bybit synchronization...")
    await asyncio.sleep(1.5)
    
    # Verify open orders
    print("\nVerifying Bybit open orders for the ID...")
    open_orders_res = session.get_open_orders(category="linear", symbol=symbol, orderId=order_id)
    open_list = open_orders_res.get("result", {}).get("list", [])
    
    # Only consider active status
    active_open_orders = [o for o in open_list if o.get("orderStatus") in ("New", "PartiallyFilled")]
    
    print("Bybit active open orders returned list:", active_open_orders)
    if active_open_orders:
        print("Warning: Order is still open on Bybit!")
        session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
    else:
        print("Success: Order is no longer open on Bybit!")

if __name__ == "__main__":
    asyncio.run(main())
