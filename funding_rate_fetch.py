import os
import time
import requests
from datetime import datetime, timezone, timedelta
from binance.client import Client
from dotenv import load_dotenv
import pytz

load_dotenv()
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
FUNDING_RATE_THRESHOLD = float(os.getenv('FUNDING_RATE_THRESHOLD', '-0.003'))
MINIMUM_BALANCE = float(os.getenv('MINIMUM_BALANCE', '10'))
client = Client(API_KEY, API_SECRET)

entry_data = {}
recent_exits = {}
IST = pytz.timezone('Asia/Kolkata')

def send_telegram_message(message: str):
    print(f"[TELEGRAM] {message}")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def check_api_connection():
    try:
        client.ping()
        return True
    except Exception as e:
        print(f"[ERROR] API connection check failed: {e}")
        return False

def get_wallet_equity():
    try:
        acc = client.futures_account_balance()
        usdt = next((x for x in acc if x['asset'] == 'USDT'), None)
        if usdt: 
            return float(usdt['balance'])
        else: 
            return 0.0
    except Exception as e:
        send_telegram_message(f"❌ Funds API error: {e}")
        return 0.0

def get_funding_interval(symbol):
    try:
        history = client.futures_funding_rate(symbol=symbol, limit=3)
        if len(history) >= 2:
            time_diff = (int(history[0]['fundingTime']) - int(history[1]['fundingTime'])) / 1000 / 3600
            if time_diff <= 5:
                return 4
            else:
                return 8
        return 8
    except:
        return 8

def get_symbol_info(symbol):
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                min_qty = 0.001
                step_size = 0.001
                precision = 3
                price_precision = 2
                
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        min_qty = float(f['minQty'])
                        step_size = float(f['stepSize'])
                        
                        # Calculate precision from stepSize
                        step_str = f['stepSize'].rstrip('0')
                        if '.' in step_str:
                            precision = len(step_str.split('.')[1])
                        else:
                            precision = 0
                    
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size = f['tickSize'].rstrip('0')
                        if '.' in tick_size:
                            price_precision = len(tick_size.split('.')[1])
                        else:
                            price_precision = 0
                
                return {
                    'min_qty': min_qty,
                    'step_size': step_size,
                    'precision': precision,
                    'price_precision': price_precision
                }
        
        return {'min_qty': 0.001, 'step_size': 0.001, 'precision': 3, 'price_precision': 2}
    except Exception as e:
        print(f"[ERROR] Symbol info: {e}")
        return {'min_qty': 0.001, 'step_size': 0.001, 'precision': 3, 'price_precision': 2}

def fetch_funding_rates():
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['contractType'] == 'PERPETUAL' and s['status'] == 'TRADING']
        
        rates = {}
        for symbol in symbols:
            try:
                premium_index = client.futures_mark_price(symbol=symbol)
                rate = float(premium_index['lastFundingRate'])
                interval = get_funding_interval(symbol)
                rates[symbol] = {'rate': rate, 'interval': interval}
            except:
                pass
        
        return rates
    except Exception as e:
        send_telegram_message(f"❌ Funding rate fetch error: {e}")
        return {}

def filter_eligible_symbols(rates, threshold):
    return {sym: data for sym, data in rates.items() if data['rate'] <= threshold}

def seconds_to_next_funding(interval=8):
    now_utc = datetime.now(timezone.utc)
    
    if interval == 4:
        next_hour = ((now_utc.hour // 4) + 1) * 4
    else:
        next_hour = ((now_utc.hour // 8) + 1) * 8
    
    if next_hour >= 24:
        next_funding_time = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        next_funding_time = now_utc.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    
    return (next_funding_time - now_utc).total_seconds()

def format_countdown(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

def format_time_ist(timestamp):
    dt_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    dt_ist = dt_utc.astimezone(IST)
    return dt_ist.strftime("%I:%M %p IST")

def find_nearest_funding_coin(eligible_coins):
    if not eligible_coins:
        return None
    
    # Calculate funding times for all coins
    coins_with_time = []
    for symbol, data in eligible_coins.items():
        time_left = seconds_to_next_funding(data['interval'])
        coins_with_time.append((symbol, data, time_left))
    
    # Find the minimum funding time
    min_time = min(coins_with_time, key=lambda x: x[2])[2]
    
    # Filter coins within 3 seconds of minimum time
    nearest_coins = [
        (symbol, data, time_left) 
        for symbol, data, time_left in coins_with_time 
        if abs(time_left - min_time) <= 3
    ]
    
    # If multiple coins are within 3 seconds, pick most negative
    if len(nearest_coins) > 1:
        most_negative = min(nearest_coins, key=lambda x: x[1]['rate'])
        return most_negative
    else:
        # Only one coin in nearest window, return it
        return nearest_coins[0]

def position_exists():
    try:
        positions = client.futures_position_information()
        return any(float(p['positionAmt']) != 0 for p in positions)
    except:
        return True

def recently_exited(symbol, cooldown_minutes=5):
    global recent_exits
    if symbol in recent_exits:
        elapsed = (datetime.now().timestamp() - recent_exits[symbol]) / 60
        if elapsed < cooldown_minutes:
            return True
    return False

def place_long_position(symbol, capital, rate):
    global entry_data
    
    try:
        if not check_api_connection():
            send_telegram_message(f"❌ TRADE CANCELED: API connection lost")
            return
        
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        
        if price <= 0:
            send_telegram_message(f"❌ TRADE CANCELED: Invalid price for {symbol}")
            return
        
        symbol_info = get_symbol_info(symbol)
        min_qty = symbol_info['min_qty']
        precision = symbol_info['precision']
        price_precision = symbol_info['price_precision']
        
        # Calculate quantity with correct precision
        quantity = round(capital / price, precision)
        
        if quantity < min_qty:
            send_telegram_message(f"❌ TRADE CANCELED: Quantity {quantity} below minimum {min_qty}")
            return
        
        # Calculate stop loss with PRICE precision
        stop_loss_price = round(price * 0.90, price_precision)
        amount = round(price * quantity, 2)
        interval = get_funding_interval(symbol)
        
        # Calculate times
        now = datetime.now().timestamp()
        exit_seconds = seconds_to_next_funding(interval) - 60
        exit_time = now + exit_seconds
        hold_duration = exit_seconds / 60
        
        # Pre-entry alert
        pre_msg = f"⚠️ PREPARING TO ENTER LONG\n\n"
        pre_msg += f"Coin: {symbol}\n"
        pre_msg += f"Funding Rate: {100*rate:.4f}%\n"
        pre_msg += f"Price: ${price}\n"
        pre_msg += f"Quantity: {quantity}\n"
        pre_msg += f"Amount: ${amount} USDT\n"
        pre_msg += f"Stop Loss: ${stop_loss_price}\n"
        pre_msg += f"Entry Time: {format_time_ist(now)}\n"
        pre_msg += f"Exit Time: {format_time_ist(exit_time)}\n"
        pre_msg += f"Hold Duration: ~{int(hold_duration)} minutes\n\n"
        pre_msg += f"🔄 Running final validations..."
        send_telegram_message(pre_msg)
        
        time.sleep(2)
        
        if position_exists():
            send_telegram_message(f"❌ TRADE CANCELED: Active position found")
            return
        
        final_balance = get_wallet_equity()
        if final_balance < MINIMUM_BALANCE:
            send_telegram_message(f"❌ TRADE CANCELED: Balance ${final_balance} below ${MINIMUM_BALANCE}")
            return
        
        if recently_exited(symbol):
            send_telegram_message(f"❌ TRADE CANCELED: {symbol} in cooldown (5 min)")
            return
        
        confirm_msg = f"✅ ALL CHECKS PASSED\n🚀 Entering position NOW..."
        send_telegram_message(confirm_msg)
        
        order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_BUY,
            type=Client.ORDER_TYPE_MARKET,
            quantity=quantity,
            positionSide='LONG'
        )
        
        entry_data[symbol] = {
            'entry_price': price,
            'quantity': quantity,
            'entry_amount': amount,
            'entry_time': now,
            'exit_time': exit_time
        }
        
        entry_msg = f"✅ LONG OPENED: {symbol}\n"
        entry_msg += f"Order ID: {order['orderId']}\n"
        entry_msg += f"Entry: {format_time_ist(now)}\n"
        entry_msg += f"Exit: {format_time_ist(exit_time)}\n"
        entry_msg += f"Hold: ~{int(hold_duration)} min"
        send_telegram_message(entry_msg)
        
        # Set 10% stop loss
        try:
            sl_order = client.futures_create_order(
                symbol=symbol,
                side=Client.SIDE_SELL,
                type='STOP_MARKET',
                stopPrice=stop_loss_price,
                closePosition=True,
                positionSide='LONG',
                workingType='MARK_PRICE'
            )
            send_telegram_message(f"✅ STOP LOSS SET: ${stop_loss_price}")
        except Exception as sl_error:
            send_telegram_message(f"❌ Stop loss error: {sl_error}\nPosition open but no SL!")
        
    except Exception as e:
        send_telegram_message(f"❌ Trade error: {e}")

def square_off_all():
    global entry_data, recent_exits
    
    try:
        positions = client.futures_position_information()
        for position in positions:
            if position['positionSide'] == 'LONG' and float(position['positionAmt']) > 0:
                amt = abs(float(position['positionAmt']))
                sym = position['symbol']
                
                ticker = client.futures_symbol_ticker(symbol=sym)
                exit_price = float(ticker['price'])
                
                entry_price = entry_data.get(sym, {}).get('entry_price', exit_price)
                entry_amount = entry_data.get(sym, {}).get('entry_amount', 0)
                entry_time = entry_data.get(sym, {}).get('entry_time', datetime.now().timestamp())
                
                exit_time = datetime.now().timestamp()
                hold_duration = (exit_time - entry_time) / 60
                
                pre_msg = f"⏰ CLOSING POSITION (1 min left)\n\n"
                pre_msg += f"Coin: {sym}\n"
                pre_msg += f"Entry: ${entry_price}\n"
                pre_msg += f"Current: ${exit_price}\n"
                pre_msg += f"Quantity: {amt}\n\n"
                pre_msg += f"Closing in 3 seconds..."
                send_telegram_message(pre_msg)
                
                time.sleep(3)
                
                close_order = client.futures_create_order(
                    symbol=sym,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=str(amt),
                    positionSide='LONG',
                    reduceOnly=True
                )
                
                exit_amount = round(exit_price * amt, 2)
                pnl_usdt = round(exit_amount - entry_amount, 2)
                pnl_percent = round((pnl_usdt / entry_amount) * 100, 2) if entry_amount > 0 else 0
                final_balance = get_wallet_equity()
                
                exit_msg = f"✅ POSITION CLOSED: {sym}\n\n"
                exit_msg += f"Position held: {int(hold_duration)} minutes\n"
                exit_msg += f"Entry Time: {format_time_ist(entry_time)}\n"
                exit_msg += f"Exit Time: {format_time_ist(exit_time)}\n\n"
                exit_msg += f"📊 TRADE SUMMARY:\n"
                exit_msg += f"Entry: ${entry_price}\n"
                exit_msg += f"Exit: ${exit_price}\n"
                exit_msg += f"Quantity: {amt}\n"
                exit_msg += f"Entry Amount: ${entry_amount}\n"
                exit_msg += f"Exit Amount: ${exit_amount}\n\n"
                
                if pnl_usdt >= 0:
                    exit_msg += f"💰 P&L: +${pnl_usdt} (+{pnl_percent}%)\n✅ Profit\n\n"
                else:
                    exit_msg += f"📉 P&L: ${pnl_usdt} ({pnl_percent}%)\n❌ Loss\n\n"
                
                exit_msg += f"Balance: ${final_balance}"
                send_telegram_message(exit_msg)
                
                recent_exits[sym] = datetime.now().timestamp()
                
                if sym in entry_data:
                    del entry_data[sym]
                    
    except Exception as e:
        send_telegram_message(f"❌ Close error: {e}")

def track_pnl():
    try:
        income = client.futures_income_history(incomeType='FUNDING_FEE', limit=100)
        total = sum(float(x['income']) for x in income)
        count = len([x for x in income if float(x['income']) != 0])
        last_24h = sum(float(x['income']) for x in income if (time.time() - int(x['time'])/1000) <= 86400)
        
        msg = f"💰 Funding P&L:\n"
        msg += f"Total: ${total:.4f}\n"
        msg += f"24h: ${last_24h:.4f}\n"
        msg += f"Payments: {count}"
        send_telegram_message(msg)
    except Exception as e:
        print(f"[ERROR] P&L: {e}")

def run_bot():
    send_telegram_message("🚦 Bot started!\n✅ 4h & 8h funding\n✅ Smart wait system\n✅ Enhanced safety\n✅ IST timezone")
    last_report = time.time()
    
    while True:
        try:
            # Check if position exists and handle exit
            if position_exists():
                positions = client.futures_position_information()
                for position in positions:
                    if position['positionSide'] == 'LONG' and float(position['positionAmt']) > 0:
                        sym = position['symbol']
                        
                        # Get exit time from entry_data
                        if sym in entry_data and 'exit_time' in entry_data[sym]:
                            exit_time = entry_data[sym]['exit_time']
                            current_time = datetime.now().timestamp()
                            time_until_exit = exit_time - current_time
                            
                            if time_until_exit <= 0:
                                # Exit time reached or passed
                                square_off_all()
                                break
                            else:
                                # Sleep until exit time
                                print(f"Position active. Exiting in {int(time_until_exit)} seconds...")
                                time.sleep(min(60, time_until_exit))
                                continue
                        else:
                            # Fallback: check based on funding time
                            interval = get_funding_interval(sym)
                            if 0 < seconds_to_next_funding(interval) <= 60:
                                square_off_all()
                                break
                
                # If position still exists, wait before next check
                time.sleep(30)
                continue
            
            # No position - scan for entry opportunities
            now_str = datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p IST")
            
            if not check_api_connection():
                send_telegram_message("⚠️ API issue - retrying in 1 min")
                time.sleep(60)
                continue
            
            rates = fetch_funding_rates()
            eligible = filter_eligible_symbols(rates, FUNDING_RATE_THRESHOLD)
            
            # Build scan message
            msg = f"🔍 Scan [{now_str}]\n\n"
            
            if not eligible:
                negative_rates = sorted(rates.items(), key=lambda x: x[1]['rate'])[:10]
                msg += f"❌ No coins below -0.3%\n\nTop 10:\n"
                for sym, data in negative_rates:
                    countdown = format_countdown(seconds_to_next_funding(data['interval']))
                    msg += f"{sym}: {100*data['rate']:.4f}% ({countdown})\n"
            else:
                sorted_eligible = sorted(eligible.items(), key=lambda x: x[1]['rate'])
                msg += f"✅ {len(eligible)} coins below -0.3%:\n\n"
                for sym, data in sorted_eligible[:10]:
                    countdown = format_countdown(seconds_to_next_funding(data['interval']))
                    msg += f"{sym}: {100*data['rate']:.4f}% ({countdown})\n"
            
            send_telegram_message(msg)
            
            # Entry logic - ONLY if coin has less than 60 minutes
            if eligible:
                nearest = find_nearest_funding_coin(eligible)
                if nearest:
                    symbol, data, time_left = nearest
                    
                    # ONLY activate smart wait if less than 60 minutes
                    if time_left < 3600:  # Less than 60 minutes
                        current_balance = get_wallet_equity()
                        
                        if current_balance < MINIMUM_BALANCE:
                            send_telegram_message(f"⚠️ Balance: ${current_balance} (below ${MINIMUM_BALANCE})")
                        else:
                            # Time left is less than 60 min - proceed with smart wait
                            if time_left > 3000:  # More than 50 min
                                wait_time = time_left - 3000  # Wait until 50-min mark
                                wait_minutes = wait_time / 60
                                
                                now = datetime.now().timestamp()
                                rescan_time = now + wait_time  # 50 min mark
                                entry_time = rescan_time + 300  # 45 min mark (50 - 5 min for scanning)
                                exit_time = now + time_left - 60
                                hold_duration = (time_left - 120) / 60
                                
                                wait_msg = f"⏰ SMART WAIT ACTIVATED\n\n"
                                wait_msg += f"Nearest funding: {format_countdown(time_left)}\n"
                                wait_msg += f"Most negative: {symbol} ({100*data['rate']:.4f}%)\n\n"
                                wait_msg += f"Will re-scan at: {format_time_ist(rescan_time)} (50 min mark)\n"
                                wait_msg += f"Will enter at: {format_time_ist(entry_time)} (45 min mark)\n"
                                wait_msg += f"Will exit at: {format_time_ist(exit_time)}\n"
                                wait_msg += f"Hold duration: ~{int(hold_duration)} min\n\n"
                                wait_msg += f"💤 Waiting {int(wait_minutes)} minutes..."
                                send_telegram_message(wait_msg)
                                
                                time.sleep(wait_time)
                                
                                # Re-scan at 50-min mark
                                send_telegram_message(f"🔍 50-MIN MARK REACHED!\n\nRe-scanning for best coin...")
                                fresh_rates = fetch_funding_rates()
                                fresh_eligible = filter_eligible_symbols(fresh_rates, FUNDING_RATE_THRESHOLD)
                                
                                if fresh_eligible:
                                    # Find most negative coin with 45-50 min left
                                    fresh_in_window = {k: v for k, v in fresh_eligible.items() 
                                                     if 2700 <= seconds_to_next_funding(v['interval']) <= 3000}
                                    
                                    if fresh_in_window:
                                        sorted_window = sorted(fresh_in_window.items(), key=lambda x: x[1]['rate'])
                                        best_symbol = sorted_window[0][0]
                                        best_rate = sorted_window[0][1]['rate']
                                        best_time_left = seconds_to_next_funding(sorted_window[0][1]['interval'])
                                        
                                        rescan_msg = f"✅ FOUND {len(fresh_in_window)} COINS IN WINDOW:\n\n"
                                        for sym, data in sorted_window[:5]:
                                            countdown = format_countdown(seconds_to_next_funding(data['interval']))
                                            rescan_msg += f"{sym}: {100*data['rate']:.4f}% ({countdown})\n"
                                        rescan_msg += f"\n🎯 Best: {best_symbol} ({100*best_rate:.4f}%)\n\n"
                                        
                                        # Calculate exact wait time to hit 45-min mark
                                        wait_for_entry = best_time_left - 2700  # Wait until exactly 45 min
                                        rescan_msg += f"⏳ Waiting {int(wait_for_entry)} seconds to enter at 45-min mark..."
                                        send_telegram_message(rescan_msg)
                                        
                                        time.sleep(wait_for_entry)
                                        
                                        send_telegram_message(f"⏰ 45-MIN MARK! Entering {best_symbol}...")
                                        place_long_position(best_symbol, current_balance, best_rate)
                                    else:
                                        send_telegram_message(f"❌ No coins in 45-50 min window during re-scan")
                                else:
                                    send_telegram_message(f"❌ No coins below -0.3% during 50-min re-scan")
                            
                            elif 2700 <= time_left <= 3000:  # Already at 45-50 min window
                                # Calculate wait time to enter at exactly 45 min
                                wait_for_entry = time_left - 2700
                                send_telegram_message(f"⏰ ALREADY IN 45-50 MIN WINDOW!\n\nWaiting {int(wait_for_entry)} seconds to enter at 45-min mark...")
                                time.sleep(wait_for_entry)
                                place_long_position(symbol, current_balance, data['rate'])
                    else:
                        # More than 60 minutes - just log and continue scanning
                        print(f"Coin {symbol} has {int(time_left/60)} minutes left. Continuing hourly scans...")
            
            # P&L report
            if time.time() - last_report > 43200:
                track_pnl()
                last_report = time.time()
            
            print("Sleeping 1 hour...\n")
            time.sleep(3600)
            
        except Exception as e:
            send_telegram_message(f"❌ Critical error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
