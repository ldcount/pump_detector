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

# Mock Telegram Bot
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
        short_size=6.0, # 6 USDT notional
        tp_size=5.0,
        sl_size=5.0,
        order_ttl=1, # 1 minute TTL for testing
        max_open_positions=5,
        trading_enabled=1,
        is_paused=0,
        paused_until_ts=None,
        is_setup_complete=1,
    )
    
    symbol = "ADAUSDT"
    
    # Fetch current price from Bybit
    session = trading.get_session()
    tickers = session.get_tickers(category="linear", symbol=symbol)
    mark_price = float(tickers["result"]["list"][0]["markPrice"])
    print(f"\nCurrent Mark Price of {symbol}: {mark_price}")
    
    # Place limit sell at 20% above mark price (so it won't fill)
    fake_trigger_price = mark_price * 1.20
    
    bot = MockBot()
    print(f"\nTriggering simulated pump signal for {symbol} at trigger price {fake_trigger_price:.4f}...")
    await trading.try_open_trade(bot, symbol, fake_trigger_price)
    
    # Let's check active trades in DB
    active_trades = db.get_active_trades()
    print("Active trades in DB:")
    for t in active_trades:
        print(dict(t))
        
    if not active_trades:
        print("No active trade created.")
        return
        
    trade = active_trades[-1]
    order_link_id = trade["order_link_id"]
    order_id = trade["order_id"]
    
    print("\nRunning periodic active trade management poller once...")
    await trading.manage_active_trades(bot)
    
    # Let's inspect the status in the DB
    updated_trade = db.get_trade(order_link_id)
    print("Updated trade in DB:", dict(updated_trade) if updated_trade else None)
    
    # Let's cancel the order to clean up
    if order_id:
        print(f"\nCleaning up: Cancelling Bybit order {order_id}...")
        try:
            res = session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
            print("Cancel Order response:", res)
            db.update_trade(order_link_id, status="cancelled", closed_timestamp=time.time())
            print("Trade updated to cancelled in DB.")
        except Exception as e:
            print("Error cancelling order:", e)

if __name__ == "__main__":
    asyncio.run(main())
