import os
import time
import math
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

def get_decimal_places(step: float) -> int:
    step_str = f"{step:.8f}".rstrip('0')
    if '.' not in step_str:
        return 0
    return len(step_str.split('.')[1])

def round_step(val: float, step: float) -> float:
    # Round down or to nearest step
    decimals = get_decimal_places(step)
    # We round to nearest step
    rounded = round(round(val / step) * step, decimals)
    return rounded

symbol = "XRPUSDT"

try:
    # 1. Fetch current price and instrument details
    instr = session.get_instruments_info(category="linear", symbol=symbol)
    symbol_info = instr["result"]["list"][0]
    tick_size = float(symbol_info["priceFilter"]["tickSize"])
    qty_step = float(symbol_info["lotSizeFilter"]["qtyStep"])
    min_qty = float(symbol_info["lotSizeFilter"]["minOrderQty"])
    min_notional = float(symbol_info["lotSizeFilter"].get("minNotionalValue", 5.0))
    
    tickers = session.get_tickers(category="linear", symbol=symbol)
    mark_price = float(tickers["result"]["list"][0]["markPrice"])
    
    print(f"Symbol: {symbol}")
    print(f"Mark Price: {mark_price}")
    print(f"Tick Size: {tick_size}, Qty Step: {qty_step}, Min Qty: {min_qty}, Min Notional: {min_notional}")
    
    # 2. Calculate a safe test price: 10% above mark price (so it won't fill)
    test_price = mark_price * 1.10
    test_price = round_step(test_price, tick_size)
    
    # Calculate qty for 6 USDT notional
    qty = 6.0 / test_price
    qty = round_step(qty, qty_step)
    if qty < min_qty:
        qty = min_qty
        
    print(f"Placing Sell order: Qty={qty}, Price={test_price:.4f}, Notional={qty*test_price:.2f} USDT")
    
    tp_price = test_price * (1 - 0.05)
    sl_price = test_price * (1 + 0.05)
    
    tp_price = round_step(tp_price, tick_size)
    sl_price = round_step(sl_price, tick_size)
    
    order_link_id = f"test_{int(time.time())}"
    
    qty_dec = get_decimal_places(qty_step)
    price_dec = get_decimal_places(tick_size)
    
    qty_str = f"{qty:.{qty_dec}f}"
    price_str = f"{test_price:.{price_dec}f}"
    tp_str = f"{tp_price:.{price_dec}f}"
    sl_str = f"{sl_price:.{price_dec}f}"
    
    print(f"Formatted parameters - Qty: {qty_str}, Price: {price_str}, TP: {tp_str}, SL: {sl_str}")
    
    res = session.place_order(
        category="linear",
        symbol=symbol,
        side="Sell",
        orderType="Limit",
        qty=qty_str,
        price=price_str,
        timeInForce="GTC",
        positionIdx=0,
        orderLinkId=order_link_id,
        takeProfit=tp_str,
        stopLoss=sl_str,
        tpTriggerBy="MarkPrice",
        slTriggerBy="MarkPrice",
    )
    print("Place Order Response:", res)
    
    order_id = res["result"]["orderId"]
    
    # 3. Fetch the open order status
    time.sleep(1)
    orders_res = session.get_open_orders(category="linear", symbol=symbol, orderId=order_id)
    print("Order details:", orders_res["result"]["list"])
    
    # 4. Cancel the order
    print("Cancelling order...")
    cancel_res = session.cancel_order(
        category="linear",
        symbol=symbol,
        orderId=order_id,
    )
    print("Cancel Order Response:", cancel_res)
    
except Exception as e:
    print("Error during order test:", type(e), str(e).encode('utf-8'))
