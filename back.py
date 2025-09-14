import time
import json
import requests
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timedelta

# Pi42 API credentials
api_key = ""
api_secret = ""

base_url_public = "https://api.pi42.com"
base_url_authenticated = "https://fapi.pi42.com"

# Global States
current_position = None    # "BUY" or "SELL"
entry_price = None
profit_threshold_crossed = False
last_exit_direction = None  # "BUY" or "SELL"
state = "WAIT_FOR_ENTRY"    # ["WAIT_FOR_ENTRY", "IN_TRADE", "WAIT_FOR_OPPOSITE_ENTRY"]

def generate_signature(api_secret, data_to_sign):
    return hmac.new(api_secret.encode('utf-8'), data_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

def fetch_klines(pair, interval, limit=200):
    url = f"{base_url_public}/v1/market/klines"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        response = requests.post(url, json=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching market data: {e}")
        return []

def calculate_heikin_ashi(data):
    ha_data = []
    for i, candle in enumerate(data):
        open_price = float(candle["open"])
        high_price = float(candle["high"])
        low_price = float(candle["low"])
        close_price = float(candle["close"])
        ha_close = (open_price + high_price + low_price + close_price) / 4
        if i == 0:
            ha_open = (open_price + close_price) / 2
        else:
            ha_open = (ha_data[-1]["ha_open"] + ha_data[-1]["ha_close"]) / 2
        ha_high = max(high_price, ha_open, ha_close)
        ha_low = min(low_price, ha_open, ha_close)
        ha_data.append({
            "ha_open": ha_open,
            "ha_close": ha_close,
            "ha_high": ha_high,
            "ha_low": ha_low
        })
    return ha_data

def calculate_z_score(ha_closes, length=200):
    if len(ha_closes) < length:
        return None
    sma = sum(ha_closes[-length:]) / length
    variance = sum((x - sma) ** 2 for x in ha_closes[-length:]) / length
    stdev = variance ** 0.5
    if stdev == 0:
        return None
    return (ha_closes[-1] - sma) / stdev

def get_futures_wallet_details():
    endpoint = "/v1/wallet/futures-wallet/details"
    wallet_details_url = f"{base_url_authenticated}{endpoint}"
    timestamp = str(int(time.time() * 1000))
    params = {'timestamp': timestamp}
    data_to_sign = urllib.parse.urlencode(params)
    signature = generate_signature(api_secret, data_to_sign)
    headers = {
        'api-key': api_key,
        'Content-Type': 'application/json',
        'signature': signature
    }
    try:
        response = requests.get(wallet_details_url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error getting wallet details: {str(e)}")
        return None

def place_order(symbol, side, quantity, order_type="MARKET"):
    url = f"{base_url_authenticated}/v1/order/place-order"
    timestamp = str(int(time.time() * 1000))
    params = {
        "timestamp": timestamp,
        "placeType": "ORDER_FORM",
        "symbol": symbol,
        "side": side,
        "reduceOnly": False,
        "quantity": quantity,
        "type": order_type,
        "marginAsset": "INR",
        "deviceType": "WEB",
        "userCategory": "EXTERNAL",
    }
    data_to_sign = json.dumps(params, separators=(',', ':'))
    signature = generate_signature(api_secret, data_to_sign)
    headers = {
        'api-key': api_key,
        'signature': signature,
        'Content-Type': 'application/json'
    }
    try:
        response = requests.post(url, json=params, headers=headers)
        response.raise_for_status()
        print("Order placed successfully:", response.json())
    except requests.exceptions.RequestException as e:
        print(f"Error placing order: {e}")

def wait_for_next_hour_close():
    now = datetime.now()
    if now.minute < 30:
        close_time = now.replace(minute=30, second=0, microsecond=0)
    else:
        close_time = (now + timedelta(hours=1)).replace(minute=30, second=0, microsecond=0)
    wait_seconds = (close_time - now).total_seconds()
    print(f"Waiting {wait_seconds/60:.2f} minutes for next hourly candle close at {close_time}")
    time.sleep(wait_seconds)

def wait_until_next_5_minute():
    now = datetime.now()
    next_minute = (now.minute // 5 + 1) * 5
    if next_minute >= 60:
        next_time = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_minute, second=0, microsecond=0)
    wait_seconds = (next_time - now).total_seconds()
    print(f"Waiting {wait_seconds/60:.2f} minutes to next 5-minute check at {next_time}")
    time.sleep(wait_seconds)

def check_hourly_zscore_exit_condition():
    global current_position, state
    pair = "BTCUSDT"
    interval = "1h"
    market_data = fetch_klines(pair, interval)
    if not market_data:
        print("No market data for hourly Z-score exit.")
        return
    ha_data = calculate_heikin_ashi(market_data)
    ha_closes = [candle["ha_close"] for candle in ha_data]
    z_score = calculate_z_score(ha_closes)
    if z_score is None:
        print("Not enough data for hourly Z-Score exit check.")
        return

    print(f"Hourly Z-Score for exit check: {z_score}")

    if current_position == "BUY" and z_score < 0:
        print("Z-Score crossed below 0 in BUY trade, exiting.")
        exit_position(pair)
    elif current_position == "SELL" and z_score > 0:
        print("Z-Score crossed above 0 in SELL trade, exiting.")
        exit_position(pair)


def check_entry_condition():
    global current_position, entry_price, last_exit_direction, state
    pair = "BTCUSDT"
    interval = "1h"
    print("Checking Entry Condition...")
    market_data = fetch_klines(pair, interval)
    if not market_data:
        print("No market data.")
        return
    ha_data = calculate_heikin_ashi(market_data)
    ha_closes = [candle["ha_close"] for candle in ha_data]
    z_score = calculate_z_score(ha_closes)
    if z_score is None:
        print("Not enough data for Z-Score.")
        return

    print(f"Z-Score: {z_score}")

    if state == "WAIT_FOR_ENTRY":
        if z_score > 0:
            print("Fresh BUY signal detected.")
            place_order(pair, "BUY", 0.002)
            current_position = "BUY"
            entry_price = ha_closes[-1]
            state = "IN_TRADE"
        if z_score < 0:
            print("Fresh SELL signal detected.")
            place_order(pair, "SELL", 0.002)
            current_position = "SELL"
            entry_price = ha_closes[-1]
            state = "IN_TRADE"
    elif state == "WAIT_FOR_OPPOSITE_ENTRY":
        if last_exit_direction == "BUY" and z_score < 0:
            print("Previous BUY, now SELL signal detected.")
            place_order(pair, "SELL", 0.002)
            current_position = "SELL"
            entry_price = ha_closes[-1]
            state = "IN_TRADE"
            last_exit_direction = None
        elif last_exit_direction == "SELL" and z_score > 0:
            print("Previous SELL, now BUY signal detected.")
            place_order(pair, "BUY", 0.002)
            current_position = "BUY"
            entry_price = ha_closes[-1]
            state = "IN_TRADE"
            last_exit_direction = None

def check_exit_condition():
    global current_position, entry_price, profit_threshold_crossed, last_exit_direction, state
    pair = "BTCUSDT"
    if current_position is None:
        return
    wallet_details = get_futures_wallet_details()
    if not wallet_details:
        print("No wallet details.")
        return
    unrealised_pnl_isolated = float(wallet_details.get("unrealisedPnlIsolated", 0.0))
    print(f"Unrealised PnL: {unrealised_pnl_isolated}")

    if unrealised_pnl_isolated >= 190:
        print("Profit >= 200, exiting position.")
        exit_position(pair)
    elif unrealised_pnl_isolated >= 90:
        profit_threshold_crossed = True
    elif profit_threshold_crossed and unrealised_pnl_isolated <= 20:
        print("Profit dropped after reaching 90+, exiting.")
        exit_position(pair)
    elif unrealised_pnl_isolated <= -90:
        print("Loss >= 90, exiting position.")
        exit_position(pair)

def exit_position(pair):
    global current_position, entry_price, profit_threshold_crossed, last_exit_direction, state
    if current_position == "BUY":
        place_order(pair, "SELL", 0.002)
        last_exit_direction = "BUY"
    elif current_position == "SELL":
        place_order(pair, "BUY", 0.002)
        last_exit_direction = "SELL"
    current_position, entry_price, profit_threshold_crossed = None, None, False
    state = "WAIT_FOR_OPPOSITE_ENTRY"


last_hourly_check = None

if __name__ == "__main__":
    print("Starting Trading Bot...")
    last_hourly_check = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

    while True:
        if state in ["WAIT_FOR_ENTRY", "WAIT_FOR_OPPOSITE_ENTRY"]:
            wait_for_next_hour_close()
            check_entry_condition()
        elif state == "IN_TRADE":
            now = datetime.now()

            # Hourly Z-score exit check once per hour
            if now.minute == 30 and now.replace(second=0, microsecond=0) > last_hourly_check:
                check_hourly_zscore_exit_condition()
                last_hourly_check = now.replace(minute=30, second=0, microsecond=0)

            wait_until_next_5_minute()
            check_exit_condition()

# # MAIN LOOP
# if __name__ == "__main__":
#     print("Starting Trading Bot...")
#     while True:
#         if state == "WAIT_FOR_ENTRY" or state == "WAIT_FOR_OPPOSITE_ENTRY":
#             wait_for_next_hour_close()
#             check_entry_condition()
#         elif state == "IN_TRADE":
#             wait_until_next_5_minute()
#             check_exit_condition()
