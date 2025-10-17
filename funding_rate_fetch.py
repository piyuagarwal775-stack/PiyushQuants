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
MINIMUM_BALANCE = float(os.getenv('MINIMUM_BALANCE', '10'))  # Minimum $10 USDT
client = Client(API_KEY, API_SECRET)

# Global variable to track entry price and recent exits
entry_data = {}
recent_exits = {}

def send_telegram_message(message: str):
    print(f"[TELEGRAM] {message}")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def check_api_connection():
    """Check if Binance API is reachable"""
    try:
        client.ping()
        return True
    except Exception as e:
        print(f"[ERROR] API connection check failed: {e}")
        return False

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
        send_telegram_message(f"❌ Funds API error: {e}")
        print(f"[ERROR] get_wallet_equity: {e}")
        return 0.0

def get_funding_interval(symbol):
    """
    Detect if coin uses 4-hour or 8-hour funding
    Returns: 4 or 8 (hours)
    """
    try:
        history = client.futures_funding_rate(symbol=symbol, limit=3)
        if len(history) >= 2:
            time_diff = (int(history[0]['fundingTime']) - int(history[1]['fundingTime'])) / 1000 / 3600
            if time_diff <= 5:
                return 4
            else:
                return 8
        return 8
    except Exception as e:
        return 8

def get_symbol_info(symbol):
    """Get minimum quantity and price precision for symbol"""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        min_qty = float(f['minQty'])
                        return {'min_qty': min_qty}
        return {'min_qty': 0.001}
    except Exception as e:
        print(f"[ERROR] get_symbol_info: {e}")
        return {'min_qty': 0.001}

def fetch_funding_rates():
    print("Fetching PREDICTED funding rates for all perpetual symbols...")
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['contractType'] == 'PERPETUAL' and s['status'] == 'TRADING']
        print(f"Total active perpetual symbols: {len(symbols)}")
        
        rates = {}
        for symbol in symbols:
            try:
                premium_index = client.futures_mark_price(symbol=symbol)
                rate = float(premium_index['lastFundingRate'])
                interval = get_funding_interval(symbol)
                
                rates[symbol] = {
                    'rate': rate,
                    'interval': interval
                }
            except Exception as e:
                pass
        
        negative_count = sum(1 for r in rates.values() if r['rate'] < 0)
        print(f"Funding rates fetched: {len(rates)} symbols, {negative_count} negative")
        return rates
    except Exception as e:
        send_telegram_message(f"❌ Funding rate fetch error: {e}")
        print(f"[ERROR] fetch_funding_rates: {e}")
        return {}

def filter_eligible_symbols(rates, threshold):
    filtered = {}
    for sym, data in rates.items():
        if data['rate'] <= threshold:
            filtered[sym] = data
    print(f"Filtered eligible symbols (<= {threshold}): {len(filtered)} found")
    return filtered

def seconds_to_next_funding(interval=8):
    now_utc = datetime.now(timezone.utc)
    
    if interval == 4:
        next_hour = ((now_utc.hour // 4) + 1) * 4
        if next_hour >= 24:
            next_funding_time = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            next_funding_time = now_utc.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    else:
        next_hour = ((now_utc.hour // 8) + 1) * 8
        if next_hour >= 24:
            next_funding_time = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            next_funding_time = now_utc.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    
    secs = (next_funding_time - now_utc).total_seconds()
    return secs

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

def is_in_entry_window(interval):
    """Check if coin is in 45-47 min window before its funding"""
    secs = seconds_to_next_funding(interval)
    return 2700 <= secs <= 2820  # 45-47 min

def is_in_close_window(interval):
    """Check if coin is in last 60 seconds before its funding"""
    secs = seconds_to_next_funding(interval)
    return 0 < secs <= 60

def position_exists():
    print("Checking for existing open positions...")
    try:
        positions = client.futures_position_information()
        active = any(float(p['positionAmt']) != 0 for p in positions)
        print(f"Active position exists? {active}")
        return active
    except Exception as e:
        send_telegram_message(f"❌ Position check error: {e}")
        print(f"[ERROR] position_exists: {e}")
        return True

def recently_exited(symbol, cooldown_minutes=5):
    """Check if symbol was recently exited (cooldown protection)"""
    global recent_exits
    if symbol in recent_exits:
        exit_time = recent_exits[symbol]
        elapsed = (datetime.now().timestamp() - exit_time) / 60
        if elapsed < cooldown_minutes:
            return True
    return False

def place_long_position(symbol, capital, rate):
    global entry_data
    print(f"Placing LONG position on {symbol} with capital: {capital}")
    
    try:
        # Step 1: API Connection Check
        if not check_api_connection():
            send_telegram_message(f"❌ TRADE CANCELED: API connection lost")
            print("[ERROR] API connection check failed")
            return
        
        # Step 2: Get current price
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        
        # Step 3: Price validation
        if price <= 0:
            send_telegram_message(f"❌ TRADE CANCELED: Invalid price for {symbol}")
            print(f"[ERROR] Invalid price: {price}")
            return
        
        # Step 4: Calculate quantity
        symbol_info = get_symbol_info(symbol)
        min_qty = symbol_info['min_qty']
        quantity = round(capital / price, 3)
        
        # Step 5: Quantity validation
        if quantity < min_qty:
            send_telegram_message(f"❌ TRADE CANCELED: Quantity {quantity} below minimum {min_qty} for {symbol}")
            print(f"[ERROR] Quantity too small: {quantity} < {min_qty}")
            return
        
        # Step 6: Calculate values
        stop_loss_price = round(price * 0.90, 3)
        amount = round(price * quantity, 2)
        
        interval = get_funding_interval(symbol)
        countdown_str = format_countdown(seconds_to_next_funding(interval))
        
        # Step 7: Pre-entry alert
        pre_msg = f"⚠️ PREPARING TO ENTER LONG\n\n"
        pre_msg += f"Coin: {symbol}\n"
        pre_msg += f"Funding Rate: {100*rate:.4f}%\n"
        pre_msg += f"Price: ${price}\n"
        pre_msg += f"Quantity: {quantity}\n"
        pre_msg += f"Amount: ${amount} USDT\n"
        pre_msg += f"Stop Loss: ${stop_loss_price}\n"
        pre_msg += f"Countdown: {countdown_str}\n\n"
        pre_msg += f"🔄 Running final validations..."
        send_telegram_message(pre_msg)
        
        time.sleep(2)
        
        # Step 8: Final position check
        if position_exists():
            send_telegram_message(f"❌ TRADE CANCELED: Active position already exists\nCannot enter {symbol} while another position is open")
            print("[CANCELED] Active position found during final check")
            return
        
        # Step 9: Final balance check
        final_balance = get_wallet_equity()
        if final_balance < MINIMUM_BALANCE:
            send_telegram_message(f"❌ TRADE CANCELED: Balance ${final_balance} below minimum ${MINIMUM_BALANCE}")
            print(f"[CANCELED] Insufficient balance: ${final_balance}")
            return
        
        # Step 10: Cooldown check
        if recently_exited(symbol):
            send_telegram_message(f"❌ TRADE CANCELED: {symbol} in cooldown period (5 min)")
            print(f"[CANCELED] Cooldown active for {symbol}")
            return
        
        # Step 11: Confirmation alert
        confirm_msg = f"✅ ALL CHECKS PASSED\n\n"
        confirm_msg += f"✅ API Connected\n"
        confirm_msg += f"✅ Price Valid: ${price}\n"
        confirm_msg += f"✅ Quantity Valid: {quantity}\n"
        confirm_msg += f"✅ Balance Sufficient: ${final_balance}\n"
        confirm_msg += f"✅ No Active Positions\n"
        confirm_msg += f"✅ {symbol} most negative ({100*rate:.4f}%)\n\n"
        confirm_msg += f"🚀 Entering position NOW..."
        send_telegram_message(confirm_msg)
        
        # Step 12: Place order
        order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_BUY,
            type=Client.ORDER_TYPE_MARKET,
            quantity=quantity,
            positionSide='LONG'
        )
        
        # Step 13: Store entry data
        entry_data[symbol] = {
            'entry_price': price,
            'quantity': quantity,
            'entry_amount': amount
        }
        
        # Step 14: Entry confirmation
        entry_msg = f"✅ LONG OPENED: {symbol}\n"
        entry_msg += f"Order ID: {order['orderId']}\n"
        entry_msg += f"Status: {order['status']}"
        send_telegram_message(entry_msg)
        print(f"LONG order placed for {symbol}. Order: {order}")
        
        # Step 15: Set stop loss
        sl_order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_SELL,
            type='STOP_MARKET',
            stopPrice=stop_loss_price,
            closePosition=True,
            workingType='MARK_PRICE'
        )
        
        sl_msg = f"✅ STOP LOSS SET: ${stop_loss_price} (10% below entry)"
        send_telegram_message(sl_msg)
        print(f"STOPLOSS order placed for {symbol}. Order: {sl_order}")
        
    except Exception as e:
        send_telegram_message(f"❌ Trade error on {symbol}: {e}")
        print(f"[ERROR] place_long_position ({symbol}): {e}")

def square_off_all():
    global entry_data, recent_exits
    print("Squaring off all positions...")
    try:
        positions = client.futures_position_information()
        for position in positions:
            if position['positionSide'] == 'LONG' and float(position['positionAmt']) > 0:
                amt = abs(float(position['positionAmt']))
                sym = position['symbol']
                
                # Get current price
                ticker = client.futures_symbol_ticker(symbol=sym)
                exit_price = float(ticker['price'])
                
                interval = get_funding_interval(sym)
                countdown_str = format_countdown(seconds_to_next_funding(interval))
                
                # Get entry data
                entry_price = entry_data.get(sym, {}).get('entry_price', exit_price)
                entry_amount = entry_data.get(sym, {}).get('entry_amount', 0)
                
                # Pre-close alert
                pre_close_msg = f"⏰ CLOSING POSITION (1 min left)\n\n"
                pre_close_msg += f"Coin: {sym}\n"
                pre_close_msg += f"Entry Price: ${entry_price}\n"
                pre_close_msg += f"Current Price: ${exit_price}\n"
                pre_close_msg += f"Quantity: {amt}\n"
                pre_close_msg += f"Countdown: {countdown_str}\n\n"
                pre_close_msg += f"Closing in 3 seconds..."
                send_telegram_message(pre_close_msg)
                
                time.sleep(3)
                
                # Close position
                close_order = client.futures_create_order(
                    symbol=sym,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=str(amt),
                    positionSide='LONG',
                    reduceOnly=True
                )
                
                # Calculate P&L
                exit_amount = round(exit_price * amt, 2)
                pnl_usdt = round(exit_amount - entry_amount, 2)
                pnl_percent = round((pnl_usdt / entry_amount) * 100, 2) if entry_amount > 0 else 0
                
                # Get final wallet balance
                final_balance = get_wallet_equity()
                
                # Exit confirmation with P&L
                exit_msg = f"✅ POSITION CLOSED: {sym}\n\n"
                exit_msg += f"📊 TRADE SUMMARY:\n"
                exit_msg += f"Entry Price: ${entry_price}\n"
                exit_msg += f"Exit Price: ${exit_price}\n"
                exit_msg += f"Quantity: {amt}\n"
                exit_msg += f"Entry Amount: ${entry_amount} USDT\n"
                exit_msg += f"Exit Amount: ${exit_amount} USDT\n\n"
                
                if pnl_usdt >= 0:
                    exit_msg += f"💰 P&L: +${pnl_usdt} USDT (+{pnl_percent}%)\n"
                    exit_msg += f"✅ Profit\n\n"
                else:
                    exit_msg += f"📉 P&L: ${pnl_usdt} USDT ({pnl_percent}%)\n"
                    exit_msg += f"❌ Loss\n\n"
                
                exit_msg += f"Wallet Balance: ${final_balance} USDT"
                send_telegram_message(exit_msg)
                
                print(f"Position closed: {sym}, qty: {amt}, P&L: ${pnl_usdt}")
                
                # Mark as recently exited (cooldown)
                recent_exits[sym] = datetime.now().timestamp()
                
                # Clear entry data
                if sym in entry_data:
                    del entry_data[sym]
                    
    except Exception as e:
        send_telegram_message(f"❌ Position close error: {e}")
        print(f"[ERROR] square_off_all: {e}")

def track_pnl():
    print("Tracking P&L from funding fees...")
    try:
        income = client.futures_income_history(incomeType='FUNDING_FEE', limit=100)
        total_funding = sum(float(x['income']) for x in income)
        
        recent_funding = [x for x in income if float(x['income']) != 0]
        count = len(recent_funding)
        
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
    send_telegram_message("🚦 Bot started & monitoring PREDICTED funding rates!\n\n✅ 4-hour & 8-hour funding support\n✅ Most negative coin in 45-min window\n✅ Full capital investment\n✅ Enhanced safety checks\n✅ Balance & position validation\n✅ API connection monitoring\n✅ 5-min cooldown protection")
    last_report = time.time()
    
    while True:
        try:
            now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            print(f"\n[{now_str}] New cycle started.")
            
            # Check API connection
            if not check_api_connection():
                send_telegram_message("⚠️ API connection issue - retrying in 1 min...")
                time.sleep(60)
                continue
            
            rates = fetch_funding_rates()
            eligible = filter_eligible_symbols(rates, FUNDING_RATE_THRESHOLD)
            
            # Build scan message
            msg = f"🔍 Funding scan [{now_str}]\n\n"
            
            if not eligible:
                negative_rates = sorted(rates.items(), key=lambda x: x[1]['rate'])[:10]
                msg += f"❌ No coins below -0.3% threshold\n\n"
                msg += f"Top 10 most negative:\n"
                for sym, data in negative_rates:
                    countdown = format_countdown(seconds_to_next_funding(data['interval']))
                    msg += f"{sym}: {100*data['rate']:.4f}% (Next: {countdown})\n"
            else:
                sorted_eligible = sorted(eligible.items(), key=lambda x: x[1]['rate'])
                msg += f"✅ {len(eligible)} coins below -0.3% threshold:\n\n"
                for sym, data in sorted_eligible[:10]:  # Show top 10
                    countdown = format_countdown(seconds_to_next_funding(data['interval']))
                    msg += f"{sym}: {100*data['rate']:.4f}% (Next: {countdown})\n"
                
                # Find coins in 45-min window
                coins_in_window = {k: v for k, v in eligible.items() if is_in_entry_window(v['interval'])}
                if coins_in_window:
                    most_negative_in_window = min(coins_in_window.items(), key=lambda x: x[1]['rate'])
                    msg += f"\n🎯 In 45-min window: {most_negative_in_window[0]} ({100*most_negative_in_window[1]['rate']:.4f}%)"
                else:
                    msg += f"\n⏳ Waiting for 45-min window..."
            
            send_telegram_message(msg)
            print(f"Eligible coins: {len(eligible)}")

            # Entry logic - find coins in 45-min window
            if not position_exists():
                # Check wallet balance first
                current_balance = get_wallet_equity()
                
                if current_balance < MINIMUM_BALANCE:
                    send_telegram_message(f"⚠️ Wallet balance: ${current_balance} USDT\n❌ Below minimum ${MINIMUM_BALANCE} - Skipping entry")
                    print(f"Insufficient balance: ${current_balance}")
                else:
                    coins_in_window = {k: v for k, v in eligible.items() if is_in_entry_window(v['interval'])}
                    
                    if coins_in_window:
                        send_telegram_message(f"⏰ 45-MIN WINDOW OPEN!\n\nStep 1: Re-scanning coins in window...\nStep 2: Checking wallet balance...\n💰 Wallet Balance: ${current_balance} USDT ✅\n\nStep 3: Checking for active positions...")
                        
                        # Double-check no position exists
                        if position_exists():
                            send_telegram_message(f"❌ TRADE CANCELED: Active position found during final check")
                            print("Active position found, skipping entry")
                        else:
                            send_telegram_message(f"✅ No active positions\n\nStep 4: Finding most negative coin...")
                            
                            # Re-scan to get fresh rates
                            fresh_rates = fetch_funding_rates()
                            fresh_eligible = filter_eligible_symbols(fresh_rates, FUNDING_RATE_THRESHOLD)
                            fresh_in_window = {k: v for k, v in fresh_eligible.items() if is_in_entry_window(v['interval'])}
                            
                            if not fresh_in_window:
                                send_telegram_message("❌ No eligible coins in 45-min window at entry time")
                                print("No eligible coins in window at entry time")
                            else:
                                # Find MOST NEGATIVE coin in 45-min window
                                sorted_window = sorted(fresh_in_window.items(), key=lambda x: x[1]['rate'])
                                best_symbol = sorted_window[0][0]
                                best_rate = sorted_window[0][1]['rate']
                                
                                rescan_msg = f"🔄 RE-SCAN (45-min window):\n\n"
                                for sym, data in sorted_window[:5]:
                                    rescan_msg += f"{sym}: {100*data['rate']:.4f}%\n"
                                rescan_msg += f"\n✅ Most negative: {best_symbol} ({100*best_rate:.4f}%)\n\nStep 5: Final validation & entry..."
                                send_telegram_message(rescan_msg)
                                
                                print(f"Best coin in 45-min window: {best_symbol} with rate {best_rate}")
                                place_long_position(best_symbol, current_balance, best_rate)
            else:
                print("Active position exists, skipping entry check")
            
            # Exit logic
            if position_exists():
                positions = client.futures_position_information()
                for position in positions:
                    if position['positionSide'] == 'LONG' and float(position['positionAmt']) > 0:
                        sym = position['symbol']
                        interval = get_funding_interval(sym)
                        if is_in_close_window(interval):
                            square_off_all()
                            break
            
            # Daily P&L report
            if time.time() - last_report > 43200:
                track_pnl()
                balance = get_wallet_equity()
                send_telegram_message(f"📊 Daily status: Bot healthy\n💰 Current Balance: ${balance} USDT")
                last_report = time.time()
            
            print("Cycle complete. Sleeping for 1 hour...\n")
            time.sleep(3600)
            
        except Exception as e:
            send_telegram_message(f"❌ Critical bot error: {e}")
            print(f"[ERROR] Critical bot error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
