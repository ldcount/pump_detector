import os
import sys
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

# Test get_instruments_info
symbol = "SOLUSDT"
try:
    print(f"Fetching instruments info for {symbol}...")
    res = session.get_instruments_info(category="linear", symbol=symbol)
    symbol_info = res["result"]["list"][0]
    print("Price Filter (tickSize):", symbol_info.get("priceFilter", {}).get("tickSize"))
    print("Lot Size Filter (qtyStep, minOrderQty):", symbol_info.get("lotSizeFilter", {}))
except Exception as e:
    print("Error fetching instruments info:", e)

# Test get_positions
try:
    print("\nFetching open positions...")
    res = session.get_positions(category="linear", settleCoin="USDT")
    positions = res.get("result", {}).get("list", [])
    print(f"Total positions in response: {len(positions)}")
    open_positions = [p for p in positions if float(p.get("size", 0)) > 0]
    print(f"Open positions (size > 0): {len(open_positions)}")
    for pos in open_positions:
        print(f"Symbol: {pos['symbol']}, Side: {pos['side']}, Size: {pos['size']}, AvgPrice: {pos['avgPrice']}, unrealisedPnl: {pos['unrealisedPnl']}")
except Exception as e:
    print("Error fetching positions:", e)

# Test get_open_orders
try:
    print("\nFetching open orders...")
    res = session.get_open_orders(category="linear", settleCoin="USDT")
    orders = res.get("result", {}).get("list", [])
    print(f"Total open orders in response: {len(orders)}")
    for order in orders:
        print(f"OrderId: {order['orderId']}, Symbol: {order['symbol']}, Side: {order['side']}, Qty: {order['qty']}, Price: {order['price']}, OrderLinkId: {order.get('orderLinkId')}")
except Exception as e:
    print("Error fetching open orders:", e)
