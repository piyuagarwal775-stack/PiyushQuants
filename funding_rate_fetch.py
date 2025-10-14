import os
import time
import requests
from datetime import datetime, timezone, timedelta
from binance.client import Client
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
FUNDING_RATE_THRESHOLD = float(os.getenv('FUNDING_RATE_THRESHOLD', '-0.003'))  # -0.3%
client = Client(API_KEY, API_SECRET)

def send_telegram_message(message: str):
    print(f"[TELEGRAM] {message}")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_wallet_equity():
    print("Fetching wallet equity...")
    try:
        acc = client.futures_account_balance()
        usdt = next((x for x in acc if x['asset'] == 'USDT'), None)
        print(f"Account balance (USDT): {usdt}")
        if usdt: return float(usdt['balance'])
        else: return 0.0
    except Exception as e:
        send_telegram_message(f"Funds API error: {e}")
        print(f"[ERROR] get_wallet_equity: {e}")
        return 0.0

def fetch_funding_rates():
    print("Fetching live funding rates for all perpetual symbols...")
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['contractType'] == 'PERPETUAL' and s['status'] == 'TRADING']
        print(f"Active symbols: {symbols}")
        rates = {}
        for symbol in symbols:
            try:
                idx = client.futures_premium_index(symbol=symbol)
                rate = float(idx['lastFundingRate'])
                rates[symbol] = rate
            except Exception as e:
                print(f"[WARNING] Funding rate fetch failed for {symbol}: {e}")
        print(f"Funding rates fetched (predicted): {rates}")
        return rates
    except Exception as e:
        send_telegram_message(f"Funding rate fetch error: {e}")
        print(f"[ERROR] fetch_funding_rates: {e}")
        return {}

def filter_eligible_symbols(rates, threshold):
    filtered = {sym: rate for sym, rate in rates.items() if rate <= threshold}
    print(f"Filtered eligible symbols (<= {threshold}): {filtered}")
    return filtered

def seconds_to_next_funding():
    now_utc = datetime.now(timezone.utc)
    next_hour = ((now_utc.hour // 8) + 1) * 8
    if next_hour >= 24:
        next_funding_time = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        next_funding_time = now_utc.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    secs = (next_funding_time - now_utc).total_seconds()
    print(f"Seconds to next funding: {secs}")
    return secs

def is_entry_window_open():
    open_ = 0 < seconds_to_next_funding() <= 45 * 60
    print(f"Entry window open? {open_}")
    return open_

def is_close_window_open():
    open_ = 0 < seconds_to_next_funding() <= 60
    print(f"Close window open? {open_}")
    return open_

def position_exists():
    print("Checking for existing open positions...")
    try:
        positions = client.futures_position_information()
        active = any(float(p['positionAmt']) != 0 for p in positions)
        print(f"Active position exists? {active}")
        return active
    except Exception as e:
        send_telegram_message(f"Position check error: {e}")
        print(f"[ERROR] position_exists: {e}")
        return True

def place_long_position(symbol, capital):
    print(f"Placing LONG position on {symbol} with capital: {capital}")
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
        print(f"LONG order placed for {symbol}. Order: {order}")
        sl_order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_SELL,
            type='STOP_MARKET',
            stopPrice=stop_loss_price,
            closePosition=True,
            workingType='MARK_PRICE'
        )
        send_telegram_message(f"STOPLOSS placed for {symbol} at {stop_loss_price}")
        print(f"STOPLOSS order placed for {symbol}. Order: {sl_order}")
    except Exception as e:
        send_telegram_message(f"Trade error on {symbol}: {e}")
        print(f"[ERROR] place_long_position ({symbol}): {e}")

def square_off_all():
    print("Squaring off all positions...")
    try:
        positions = client.futures_position_information()
        for position in positions:
            if position['positionSide'] == 'LONG' and float(position['positionAmt']) > 0:
                amt = str(abs(float(position['positionAmt'])))
                sym = position['symbol']
                close_order = client.futures_create_order(
                    symbol=sym,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=amt,
                    positionSide='LONG',
                    reduceOnly=True
                )
                send_telegram_message(f"Position closed: {sym} QTY {amt}")
                print(f"Position closed: {sym}, qty: {amt}. Close order: {close_order}")
    except Exception as e:
        send_telegram_message(f"Position close error: {e}")
        print(f"[ERROR] square_off_all: {e}")

def run_bot():
    print("##### Bot starting... #####")
    send_telegram_message("🚦Bot started & monitoring! [Health OK]")
    last_report = time.time()
    while True:
        try:
            now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            send_telegram_message(f"🔍 Funding scan start [{now_str}]")
            print(f"[{now_str}] New cycle started.")
            rates = fetch_funding_rates()
            
            # --- DEBUG PATCH: Print ALL rates in Telegram for troubleshooting ---
            if not rates:
                send_telegram_message("DEBUG: No rates fetched at all!")
            else:
                debug_rates_msg = "DEBUG: All fetched live rates:\n" + "\n".join(
                    [f"{k}: {100*v:.4f}%" for k, v in sorted(rates.items())]
                )
                send_telegram_message(debug_rates_msg)
            # --------------------------------------------------

            eligible = filter_eligible_symbols(rates, FUNDING_RATE_THRESHOLD)

            if not eligible:
                send_telegram_message("No coins currently below threshold (-0.3%).")
            else:
                msg = "Funding screener (all coins below threshold):\n" + "\n".join([f"{k}: {100*v:.4f}%" for k,v in eligible.items()])
                send_telegram_message(msg)
                print(f"Funding Screener Eligible: {eligible}")

            if position_exists():
                send_telegram_message("Active position exists, skipping new entries this cycle.")
                print("Active position found, skipping new entry.")
            else:
                if is_entry_window_open():
                    send_telegram_message("Entry window open, rechecking shortlisted rates...")
                    capital = get_wallet_equity()
                    print("Entry window: Checking eligible coins live for entry...")
                    for sym in eligible:
                        try:
                            idx = client.futures_premium_index(symbol=sym)
                            rate = float(idx['lastFundingRate'])
                        except Exception as e:
                            print(f"Live refetch failed for {sym}: {e}")
                            continue
                        print(f"{sym} new fetched rate: {rate}")
                        if rate <= FUNDING_RATE_THRESHOLD:
                            place_long_position(sym, capital)
                            break
                        else:
                            send_telegram_message(f"{sym} skipped, current rate {100*rate:.2f}% above threshold.")
                            print(f"{sym} skipped, current rate {100*rate:.2f}% above threshold.")
                else:
                    send_telegram_message("Entry window not open.")
                    print("Entry window not open.")
            if is_close_window_open() and position_exists():
                send_telegram_message("Close window, squaring off all positions.")
                print("Close window: Squaring off all positions.")
                square_off_all()
            if time.time() - last_report > 43200:
                send_telegram_message("Daily status: Bot healthy, no critical issues.")
                print("Daily health report sent to Telegram.")
                last_report = time.time()
            print("Cycle complete. Sleeping for 1 hour...\n")
            time.sleep(3600)
        except Exception as e:
            send_telegram_message(f"Critical bot error: {e}")
            print(f"[ERROR] Critical bot error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
