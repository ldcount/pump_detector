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

symbol = "XRPUSDT"

try:
    print("Fetching order history for XRPUSDT...")
    res = session.get_order_history(
        category="linear",
        symbol=symbol,
        limit=5,
    )
    history = res.get("result", {}).get("list", [])
    print(f"History list length: {len(history)}")
    if history:
        o = history[0]
        print(f"OrderId: {o['orderId']}, Status: {o['orderStatus']}, Qty: {o['qty']}, Price: {o['price']}, CumExecQty: {o['cumExecQty']}, AvgPrice: {o.get('avgPrice')}")
except Exception as e:
    print("Error:", e)
