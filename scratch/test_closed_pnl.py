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
    res = session.get_closed_pnl(
        category="linear",
        limit=1,
    )
    closed_list = res.get("result", {}).get("list", [])
    if closed_list:
        import pprint
        pprint.pprint(dict(closed_list[0]))
except Exception as e:
    print("Error:", e)
