import os
import sys
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

api_key = os.getenv("BYBIT_API_KEY")
api_secret = os.getenv("BYBIT_API_SECRET")

print("API Key:", api_key)
if not api_key or not api_secret:
    print("Error: API credentials missing")
    sys.exit(1)

try:
    session = HTTP(
        testnet=False,
        api_key=api_key,
        api_secret=api_secret,
    )
    # Check wallet balance for USDT perp (linear) or unified
    print("Checking balance...")
    res = session.get_wallet_balance(accountType="UNIFIED")
    print("Unified Balance Response status:", res.get("retMsg"))
    if "list" in res.get("result", {}):
        print("Balances:", res["result"]["list"])
except Exception as e:
    print("Unified check failed, trying CLASSIC CONTRACT:")
    try:
        session = HTTP(
            testnet=False,
            api_key=api_key,
            api_secret=api_secret,
        )
        res = session.get_wallet_balance(accountType="CONTRACT")
        print("Contract Balance Response status:", res.get("retMsg"))
    except Exception as e2:
        print("Both failed.")
        print("Error 1:", e)
        print("Error 2:", e2)
