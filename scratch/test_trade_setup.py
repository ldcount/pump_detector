import os
import sys
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from pybit.exceptions import InvalidRequestError

load_dotenv()

api_key = os.getenv("BYBIT_API_KEY")
api_secret = os.getenv("BYBIT_API_SECRET")

session = HTTP(
    testnet=False,
    api_key=api_key,
    api_secret=api_secret,
)

symbol = "SOLUSDT"

try:
    print("Setting leverage to 10x...")
    res = session.set_leverage(
        category="linear",
        symbol=symbol,
        buyLeverage="10",
        sellLeverage="10",
    )
    print("Set leverage response:", res)
except InvalidRequestError as e:
    if e.status_code == 110043 or "leverage not modified" in str(e):
        print("Leverage is already 10x (ignored exception).")
    else:
        print("InvalidRequestError setting leverage:", e.status_code, str(e).encode('utf-8'))
except Exception as e:
    print("Error setting leverage:", type(e), str(e).encode('utf-8'))

try:
    print("Checking/Setting position mode to one-way...")
    # Bybit: switch_position_mode
    # For linear, mode=0 (one-way), mode=3 (hedge)
    res = session.switch_position_mode(
        category="linear",
        symbol=symbol,
        mode=0, # 0: Merged Single (One-way), 3: Both Sides (Hedge)
    )
    print("Switch position mode response:", res)
except InvalidRequestError as e:
    if e.status_code == 110025 or "Position mode is not modified" in str(e):
        print("Position mode is already one-way (ignored exception).")
    else:
        print("InvalidRequestError switching position mode:", e.status_code, str(e).encode('utf-8'))
except Exception as e:
    print("Error switching position mode:", type(e), str(e).encode('utf-8'))
