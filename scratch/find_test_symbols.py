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
    res = session.get_instruments_info(category="linear")
    symbols = res["result"]["list"]
    eligible = []
    for s in symbols:
        name = s["symbol"]
        if not name.endswith("USDT"):
            continue
        lot = s.get("lotSizeFilter", {})
        min_qty = float(lot.get("minOrderQty", 0))
        # Sometimes minNotionalValue is not present or is empty string
        notional_str = lot.get("minNotionalValue", "0")
        min_notional = float(notional_str) if notional_str else 0.0
        
        eligible.append((name, min_qty, min_notional))
        
    print(f"Total USDT symbols: {len(eligible)}")
    
    # Get tickers to check prices
    tickers_res = session.get_tickers(category="linear")
    prices = {t["symbol"]: float(t["markPrice"]) for t in tickers_res["result"]["list"] if "markPrice" in t}
    
    count = 0
    for name, min_qty, min_notional in eligible:
        if name in prices:
            price = prices[name]
            cost_of_min_qty = min_qty * price
            # We want cost of min qty to be <= 5.0, and min_notional <= 5.0
            if cost_of_min_qty <= 5.0 and min_notional <= 5.0:
                print(f"Symbol: {name:15} Price: {price:10.5f} MinQty: {min_qty:10.5f} MinNotional: {min_notional:5.2f} CostOfMinQty: {cost_of_min_qty:5.2f}")
                count += 1
                if count >= 20:
                    break
except Exception as e:
    print("Error:", e)
