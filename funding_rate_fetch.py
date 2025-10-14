import os
import time
import requests
from datetime import datetime, timezone, timedelta
from binance.client import Client
from dotenv import load_dotenv

# 1. SETUP & CONFIGURATION
load_dotenv()
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
FUNDING_RATE_THRESHOLD = float(os.getenv('FUNDING_RATE_THRESHOLD', '-0.005'))
client = Client(API_KEY, API_SECRET)

def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_wallet_equity():
    try:
        acc = client.futures_account_balance()
        usdt = next((x for x in acc if x['asset'] == 'USDT'), None)
        if usdt: return float(usdt['balance'])
        else: return 0.0
    except Exception as e:
        send_telegram_message(f"Funds API error: {e}")
        return 0.0

def fetch_funding_rates():
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['contractType'] == 'PERPETUAL' and s['status'] == 'TRADING']
        rates = {}
        all_rates = client.futures_funding_rate()
        rate_map = {rate['symbol']: float(rate['fundingRate']) for rate in all_rates}
        for symbol in symbols:
            if symbol in rate_map:
                rates[symbol] = rate_map[symbol]
        return rates
    except Exception as e:
        send_telegram_message(f"Funding rate fetch error: {e}")
        return {}

def filter_eligible_symbols(rates, threshold):
    return {sym: rate for sym, rate in rates.items() if rate <= threshold}

def seconds_to_next_funding():
    now_utc = datetime.now(timezone.utc)
    next_hour = ((now_utc.hour // 8) + 1) * 8
    if next_hour >= 24:
        next_funding_time = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        next_funding_time = now_utc.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    return (next_funding_time - now_utc).total_seconds()

def is_entry_window_open():
    return 0 < seconds_to_next_funding() <= 45 * 60

def is_close_window_open():
    return 0 < seconds_to_next_funding() <= 60

def position_exists():
    try:
        positions = client.futures_position_information()
        active = any(float(p['positionAmt']) != 0 for p in positions)
        return active
    except Exception as e:
        send_telegram_message(f"Position check error: {e}")
        return True

def place_long_position(symbol, capital):
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        quantity = round(capital / price, 3)
        stop_loss_price = round(price * 0.90, 3)
        order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_BUY,
            type=Client.ORDER_TYPE_MARKET,
            quantity=quantity,
            positionSide='LONG'
        )
        send_telegram_message(f"LONG order: {symbol} QTY {quantity} @ {price}. SL: {stop_loss_price}")
        client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_SELL,
            type='STOP_MARKET',
            stopPrice=stop_loss_price,
            closePosition=True,
            workingType='MARK_PRICE'
        )
        send_telegram_message(f"STOPLOSS placed for {symbol} at {stop_loss_price}")
    except Exception as e:
        send_telegram_message(f"Trade error on {symbol}: {e}")

def square_off_all():
    try:
        positions = client.futures_position_information()
        for position in positions:
            if position['positionSide'] == 'LONG' and float(position['positionAmt']) > 0:
                amt = str(abs(float(position['positionAmt'])))
                sym = position['symbol']
                client.futures_create_order(
                    symbol=sym,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=amt,
                    positionSide='LONG',
                    reduceOnly=True
                )
                send_telegram_message(f"Position closed: {sym} QTY {amt}")
    except Exception as e:
        send_telegram_message(f"Position close error: {e}")

def run_bot():
    send_telegram_message("🚦Bot started & monitoring! [Health OK]")
    last_report = time.time()
    while True:
        try:
            now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            send_telegram_message(f"🔍 Funding scan start [{now_str}]")
            rates = fetch_funding_rates()
            eligible = filter_eligible_symbols(rates, FUNDING_RATE_THRESHOLD)
            if not eligible:
                send_telegram_message("No eligible coins found below threshold.")
            else:
                msg = "Eligible coins below threshold (-0.5%):\n" + "\n".join([f"{k}: {100*v:.2f}%" for k,v in eligible.items()])
                send_telegram_message(msg)
            if position_exists():
                send_telegram_message("Active position exists, skipping new entries this cycle.")
            else:
                if is_entry_window_open():
                    send_telegram_message("Entry window open, rechecking shortlisted rates...")
                    capital = get_wallet_equity()
                    for sym in eligible:
                        rate = fetch_funding_rates()[sym]
                        if rate <= FUNDING_RATE_THRESHOLD:
                            place_long_position(sym, capital)
                            break
                        else:
                            send_telegram_message(f"{sym} skipped, current rate {100*rate:.2f}% above threshold.")
                else:
                    send_telegram_message("Entry window not open.")
            if is_close_window_open() and position_exists():
                send_telegram_message("Close window, squaring off all positions.")
                square_off_all()
            if time.time() - last_report > 43200:
                send_telegram_message("Daily status: Bot healthy, no critical issues.")
                last_report = time.time()
            time.sleep(3600)
        except Exception as e:
            send_telegram_message(f"Critical bot error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
