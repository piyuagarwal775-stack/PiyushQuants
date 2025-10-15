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
        if usdt: 
            return float(usdt['balance'])
        else: 
            return 0.0
    except Exception as e:
        send_telegram_message(f"Funds API error: {e}")
        print(f"[ERROR] get_wallet_equity: {e}")
        return 0.0

def fetch_funding_rates():
    print("Fetching PREDICTED funding rates (next funding) for all perpetual symbols...")
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['contractType'] == 'PERPETUAL' and s['status'] == 'TRADING']
        print(f"Total active perpetual symbols: {len(symbols)}")
        rates = {}
        for symbol in symbols:
            try:
                # Get PREDICTED funding rate (what will be charged next)
                premium_index = client.futures_mark_price(symbol=symbol)
                rate = float(premium_index['lastFundingRate'])
                rates[symbol] = rate
            except Exception as e:
                print(f"[WARNING] Funding rate fetch failed for {symbol}: {e}")
        
        # Show summary
        negative_count = sum(1 for r in rates.values() if r < 0)
        print(f"Funding rates fetched: {len(rates)} symbols, {negative_count} with negative rates")
        return rates
    except Exception as e:
        send_telegram_message(f"Funding rate fetch error: {e}")
        print(f"[ERROR] fetch_funding_rates: {e}")
        return {}

def filter_eligible_symbols(rates, threshold):
    filtered = {sym: rate for sym, rate in rates.items() if rate <= threshold}
    print(f"Filtered eligible symbols (<= {threshold}): {len(filtered)} found")
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

def format_countdown(seconds):
    """Convert seconds to readable format: Xh Ym Zs"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

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
        send_telegram_message(f"✅ LONG order: {symbol} QTY {quantity} @ {price}. SL: {stop_loss_price}")
        print(f"LONG order placed for {symbol}. Order: {order}")
        sl_order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_SELL,
            type='STOP_MARKET',
            stopPrice=stop_loss_price,
            closePosition=True,
            workingType='MARK_PRICE'
        )
        send_telegram_message(f"🛡️ STOPLOSS placed for {symbol} at {stop_loss_price}")
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
                send_telegram_message(f"✅ Position closed: {sym} QTY {amt}")
                print(f"Position closed: {sym}, qty: {amt}. Close order: {close_order}")
    except Exception as e:
        send_telegram_message(f"Position close error: {e}")
        print(f"[ERROR] square_off_all: {e}")

def track_pnl():
    """Track P&L from funding fees - NEWLY ADDED"""
    print("Tracking P&L from funding fees...")
    try:
        income = client.futures_income_history(incomeType='FUNDING_FEE', limit=100)
        total_funding = sum(float(x['income']) for x in income)
        
        # Count recent funding payments
        recent_funding = [x for x in income if float(x['income']) != 0]
        count = len(recent_funding)
        
        # Get last 24h funding
        last_24h = sum(float(x['income']) for x in income if (time.time() - int(x['time'])/1000) <= 86400)
        
        msg = f"💰 Funding P&L Report:\n"
        msg += f"Total collected: ${total_funding:.4f} USDT\n"
        msg += f"Last 24h: ${last_24h:.4f} USDT\n"
        msg += f"Funding payments: {count}"
        
        send_telegram_message(msg)
        print(f"P&L Report - Total: ${total_funding:.4f}, 24h: ${last_24h:.4f}, Count: {count}")
    except Exception as e:
        send_telegram_message(f"P&L tracking error: {e}")
        print(f"[ERROR] track_pnl: {e}")

def run_bot():
    print("##### Bot starting... #####")
    send_telegram_message("🚦 Bot started & monitoring PREDICTED funding rates! [Health OK]")
    last_report = time.time()
    while True:
        try:
            now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            
            # Get countdown
            countdown_secs = seconds_to_next_funding()
            countdown_str = format_countdown(countdown_secs)
            
            send_telegram_message(f"🔍 PREDICTED Funding scan start [{now_str}]\n⏱️ Next funding in: {countdown_str}")
            print(f"[{now_str}] New cycle started. Countdown: {countdown_str}")
            
            rates = fetch_funding_rates()
            eligible = filter_eligible_symbols(rates, FUNDING_RATE_THRESHOLD)
            
            # --- Funding screener logic ---
            if not eligible:
                negative_rates = {k: v for k, v in sorted(rates.items(), key=lambda x: x[1])[:10]}
                msg = f"❌ No coins below -0.3% threshold.\n⏱️ Countdown: {countdown_str}\n\nTop 10 most negative rates:\n"
                msg += "\n".join([f"{k}: {100*v:.4f}%" for k,v in negative_rates.items()])
                send_telegram_message(msg)
            else:
                msg = f"✅ {len(eligible)} coins below -0.3% threshold:\n⏱️ Countdown: {countdown_str}\n\n"
                sorted_eligible = sorted(eligible.items(), key=lambda x: x[1])
                msg += "\n".join([f"{k}: {100*v:.4f}%" for k,v in sorted_eligible])
                msg += f"\n\n🎯 Will enter: {sorted_eligible[0][0]} (most negative)"
                send_telegram_message(msg)
                print(f"Funding Screener Eligible: {eligible}")

            # --- Main entry/exit logic ---
            if position_exists():
                send_telegram_message(f"📊 Active position exists, skipping new entries.\n⏱️ Countdown: {countdown_str}")
                print("Active position found, skipping new entry.")
            else:
                if is_entry_window_open():
                    send_telegram_message(f"⏰ Entry window OPEN! Rechecking rates...\n⏱️ Countdown: {countdown_str}")
                    capital = get_wallet_equity()
                    print("Entry window: Checking eligible coins live for entry...")
                    
                    # Sort eligible by most negative first
                    sorted_eligible = sorted(eligible.items(), key=lambda x: x[1])
                    
                    for sym, _ in sorted_eligible:
                        try:
                            # Re-fetch PREDICTED rate to confirm
                            premium_index = client.futures_mark_price(symbol=sym)
                            rate = float(premium_index['lastFundingRate'])
                        except Exception as e:
                            print(f"Live refetch failed for {sym}: {e}")
                            continue
                        print(f"{sym} new fetched rate: {rate}")
                        if rate <= FUNDING_RATE_THRESHOLD:
                            send_telegram_message(f"🎯 Selected: {sym} with rate {100*rate:.4f}%")
                            place_long_position(sym, capital)
                            break
                        else:
                            send_telegram_message(f"{sym} skipped, current rate {100*rate:.2f}% above threshold.")
                            print(f"{sym} skipped, current rate {100*rate:.2f}% above threshold.")
                else:
                    print(f"Entry window not open. Countdown: {countdown_str}")
            
            if is_close_window_open() and position_exists():
                send_telegram_message(f"⏰ Close window OPEN! Squaring off all positions.\n⏱️ Countdown: {countdown_str}")
                print("Close window: Squaring off all positions.")
                square_off_all()
            
            # Daily report with P&L tracking
            if time.time() - last_report > 43200:
                track_pnl()  # NEW: Track P&L every 12 hours
                send_telegram_message("📊 Daily status: Bot healthy, no critical issues.")
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
