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
    res = session.get_order_history(
        category="linear",
        symbol="ARPAUSDT",
        orderId="3065147e-116c-4602-a5ce-b07886cc0ae8",
    )
    order_list = res.get("result", {}).get("list", [])
    if order_list:
        import pprint
        pprint.pprint(dict(order_list[0]))
except Exception as e:
    print("Error:", e)
