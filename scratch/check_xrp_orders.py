import os
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

api_key = os.getenv("BYBIT_API_KEY")
api_secret = os.getenv("BYBIT_API_SECRET")

session = HTTP(
    testnet=False,
    api_key=api_key,
    api_secret=api_secret,
)

try:
    print("Checking open orders for XRPUSDT...")
    res = session.get_open_orders(category="linear", symbol="XRPUSDT")
    orders = res.get("result", {}).get("list", [])
    print(f"Open orders count: {len(orders)}")
    for o in orders:
        print(f"OrderId: {o['orderId']}, Status: {o['orderStatus']}, Side: {o['side']}, Qty: {o['qty']}, Price: {o['price']}, stopOrderType: {o.get('stopOrderType')}")
except Exception as e:
    print("Error:", e)
