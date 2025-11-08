from flask import Flask, render_template, request, jsonify, session
import sqlite3
import hashlib
import random
from datetime import datetime, timedelta
import math
import requests
import configparser
import os
import pytz

app = Flask(__name__)
app.secret_key = 'trading_secret_key'

def load_config():
    """Load configuration from properties file"""
    config_path = os.path.join(os.path.dirname(__file__), 'config.properties')
    
    # Default values
    config_dict = {
        'yahoo_finance_nifty_url': 'https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI',
        'nse_india_url': 'https://www.nseindia.com/api/allIndices',
        'fallback_nifty_price': '24350.75',
        'min_nifty_price': '15000',
        'max_nifty_price': '30000',
        'nifty_lot_size': '75'
    }
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        config_dict[key.strip()] = value.strip()
        except Exception as e:
            print(f"Warning: Could not load config file: {e}")
    
    return config_dict

# Load configuration
CONFIG = load_config()

# Indian market holidays for 2024-2025
MARKET_HOLIDAYS = {
    # 2024 holidays
    '2024-01-26': 'Republic Day',
    '2024-03-08': 'Holi',
    '2024-03-29': 'Good Friday',
    '2024-04-11': 'Id-Ul-Fitr',
    '2024-04-17': 'Ram Navami',
    '2024-05-01': 'Maharashtra Day',
    '2024-06-17': 'Bakri Id',
    '2024-08-15': 'Independence Day',
    '2024-10-02': 'Gandhi Jayanti',
    '2024-11-01': 'Diwali Laxmi Pujan',
    '2024-11-15': 'Guru Nanak Jayanti',
    '2024-12-25': 'Christmas',
    
    # 2025 holidays
    '2025-01-26': 'Republic Day',
    '2025-02-26': 'Holi',
    '2025-03-31': 'Id-Ul-Fitr',
    '2025-04-14': 'Ram Navami',
    '2025-04-18': 'Good Friday',
    '2025-05-01': 'Maharashtra Day',
    '2025-06-07': 'Bakri Id',
    '2025-08-15': 'Independence Day',
    '2025-10-02': 'Gandhi Jayanti',
    '2025-10-20': 'Diwali Laxmi Pujan',
    '2025-11-05': 'Guru Nanak Jayanti',
    '2025-12-25': 'Christmas'
}

def is_market_open():
    """Check if market is currently open"""
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    
    # Check if today is a weekend
    if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False, "Market closed - Weekend"
    
    # Check if today is a holiday
    today_str = now.strftime('%Y-%m-%d')
    if today_str in MARKET_HOLIDAYS:
        return False, f"Market closed - {MARKET_HOLIDAYS[today_str]}"
    
    # Check market hours (9:15 AM to 3:30 PM IST)
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    if now < market_open:
        return False, f"Market opens at 9:15 AM (Current: {now.strftime('%H:%M')})"
    elif now > market_close:
        return False, f"Market closed at 3:30 PM (Current: {now.strftime('%H:%M')})"
    
    return True, "Market is open"

def get_next_market_open():
    """Get next market opening time"""
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    
    # Start with tomorrow if market is closed today
    next_day = now + timedelta(days=1)
    
    # Find next working day
    while True:
        # Skip weekends
        if next_day.weekday() >= 5:
            next_day += timedelta(days=1)
            continue
            
        # Skip holidays
        day_str = next_day.strftime('%Y-%m-%d')
        if day_str in MARKET_HOLIDAYS:
            next_day += timedelta(days=1)
            continue
            
        # Found next market day
        break
    
    next_open = next_day.replace(hour=9, minute=15, second=0, microsecond=0)
    return next_open

def get_real_nifty_price():
    """Fetch real NIFTY 50 closing price from API using configurable URLs"""
    min_price = float(CONFIG.get('min_nifty_price', 15000))
    max_price = float(CONFIG.get('max_nifty_price', 30000))
    
    try:
        # Try Yahoo Finance API first
        url = CONFIG.get('yahoo_finance_nifty_url', 'https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI')
        response = requests.get(url, timeout=5)
        data = response.json()
        
        if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
            result = data['chart']['result'][0]
            if 'meta' in result and 'regularMarketPrice' in result['meta']:
                price = result['meta']['regularMarketPrice']
                # Validate price is realistic
                if min_price <= price <= max_price:
                    return round(price, 2)
    except:
        pass
    
    try:
        # Alternative API - NSE India (if available)
        url = CONFIG.get('nse_india_url', 'https://www.nseindia.com/api/allIndices')
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        
        for index in data['data']:
            if index['index'] == 'NIFTY 50':
                price = float(index['last'])
                if min_price <= price <= max_price:
                    return round(price, 2)
    except:
        pass
    
    # Fallback to configurable NIFTY price
    return float(CONFIG.get('fallback_nifty_price', 24350.75))

def generate_nifty_options():
    """Generate NIFTY 50 options chain for current week and month"""
    today = datetime.now()
    
    # Calculate weekly expiry (Thursday)
    days_until_thursday = (3 - today.weekday()) % 7
    if days_until_thursday == 0 and today.hour >= 15:  # After 3 PM on Thursday
        days_until_thursday = 7
    weekly_expiry = today + timedelta(days=days_until_thursday)
    
    # Calculate monthly expiry (last Thursday of month)
    next_month = today.replace(day=28) + timedelta(days=4)
    last_day = next_month - timedelta(days=next_month.day)
    monthly_expiry = last_day - timedelta(days=(last_day.weekday() - 3) % 7)
    
    # Current NIFTY price (real data)
    nifty_price = get_real_nifty_price()
    
    options = []
    greeks = []
    
    # Generate ATM strikes (±5 strikes around current price)
    atm_strikes = []
    for i in range(-5, 6):  # 11 ATM strikes
        strike = round((nifty_price + (i * 50)) / 50) * 50  # Round to nearest 50
        atm_strikes.append(strike)
    
    # Generate all strikes for search functionality
    all_strikes = []
    for i in range(-50, 51):  # 101 strikes
        strike = round((nifty_price + (i * 50)) / 50) * 50
        all_strikes.append(strike)
    
    # Calculate current and next weekly expiry (Thursdays)
    current_week_expiry = weekly_expiry
    next_week_expiry = current_week_expiry + timedelta(days=7)
    
    # Include both weekly expiries and monthly
    expiries = [
        (current_week_expiry.strftime('%Y-%m-%d'), f'W{current_week_expiry.day:02d}'),
        (next_week_expiry.strftime('%Y-%m-%d'), f'W{next_week_expiry.day:02d}'),
        (monthly_expiry.strftime('%Y-%m-%d'), 'MN')
    ]
    
    def create_option(strike, expiry_date, exp_type, opt_type):
        moneyness = strike / nifty_price
        
        # Use realistic market prices based on actual data patterns
        strike_diff = strike - nifty_price
        
        if opt_type == 'CE':
            if strike_diff <= -200:  # Deep ITM
                price = abs(strike_diff) + random.uniform(20, 60)
            elif strike_diff <= -50:  # ITM
                price = abs(strike_diff) + random.uniform(50, 150)
            elif abs(strike_diff) <= 50:  # ATM
                price = random.uniform(200, 400)
            elif strike_diff <= 200:  # OTM
                price = random.uniform(10, 80)
            else:  # Deep OTM
                price = random.uniform(0.5, 15)
            delta = max(0.05, min(0.95, 0.5 + strike_diff / -1000))
        else:  # PE
            if strike_diff >= 200:  # Deep ITM
                price = strike_diff + random.uniform(20, 60)
            elif strike_diff >= 50:  # ITM
                price = strike_diff + random.uniform(50, 150)
            elif abs(strike_diff) <= 50:  # ATM
                price = random.uniform(200, 400)
            elif strike_diff >= -200:  # OTM
                price = random.uniform(10, 80)
            else:  # Deep OTM
                price = random.uniform(0.5, 15)
            delta = max(-0.95, min(-0.05, -0.5 + strike_diff / 1000))
        
        # Generate realistic change percentage
        change_pct = random.uniform(-15, 15)
        
        symbol = f'NIFTY{expiry_date.replace("-", "")[-4:]}{int(strike)}{exp_type}{opt_type}'
        
        gamma = 0.001 + random.uniform(0, 0.005)
        theta = -random.uniform(5, 20)
        vega = random.uniform(10, 25)
        iv = random.uniform(0.12, 0.25)
        
        nifty_lot_size = int(CONFIG.get('nifty_lot_size', 75))
        return (symbol, strike, expiry_date, opt_type, round(price, 2), round(change_pct, 2), nifty_lot_size), (symbol, delta, gamma, theta, vega, iv)
    
    # Create ATM options for display
    for expiry_date, exp_type in expiries:
        for strike in atm_strikes:
            ce_opt, ce_greek = create_option(strike, expiry_date, exp_type, 'CE')
            pe_opt, pe_greek = create_option(strike, expiry_date, exp_type, 'PE')
            
            options.extend([ce_opt, pe_opt])
            greeks.extend([ce_greek, pe_greek])
        
        # Store all strikes for search (but don't add to main options yet)
        for strike in all_strikes:
            if strike not in atm_strikes:
                ce_opt, ce_greek = create_option(strike, expiry_date, exp_type, 'CE')
                pe_opt, pe_greek = create_option(strike, expiry_date, exp_type, 'PE')
                
                # Store in separate table for search
                options.extend([ce_opt, pe_opt])
                greeks.extend([ce_greek, pe_greek])
    
    return options, greeks, nifty_price

def init_db():
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, balance REAL DEFAULT 100000)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS stocks
                 (symbol TEXT PRIMARY KEY, name TEXT, price REAL, change_percent REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio
                 (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, quantity INTEGER, avg_price REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, type TEXT, quantity INTEGER, 
                  price REAL, status TEXT, timestamp TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS futures
                 (symbol TEXT PRIMARY KEY, name TEXT, expiry TEXT, price REAL, change_percent REAL, lot_size INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS options
                 (id INTEGER PRIMARY KEY, symbol TEXT, strike REAL, expiry TEXT, type TEXT, 
                  price REAL, change_percent REAL, lot_size INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS fo_portfolio
                 (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, instrument_type TEXT, 
                  strike REAL, expiry TEXT, quantity INTEGER, avg_price REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS strategies
                 (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, type TEXT, 
                  conditions TEXT, status TEXT, created_at TEXT, execution_count INTEGER DEFAULT 0)''')
    
    # Add execution_count column if it doesn't exist
    try:
        c.execute('ALTER TABLE strategies ADD COLUMN execution_count INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    c.execute('''CREATE TABLE IF NOT EXISTS greeks
                 (symbol TEXT PRIMARY KEY, delta REAL, gamma REAL, theta REAL, vega REAL, iv REAL)''')
    
    stocks = [
        ('RELIANCE', 'Reliance Industries', 2500.0, 1.2),
        ('TCS', 'Tata Consultancy Services', 3200.0, -0.8),
        ('INFY', 'Infosys', 1450.0, 2.1),
        ('HDFC', 'HDFC Bank', 1600.0, 0.5),
        ('ICICI', 'ICICI Bank', 950.0, -1.2),
        ('WIPRO', 'Wipro', 420.0, 1.8),
        ('BHARTI', 'Bharti Airtel', 850.0, 0.9),
        ('ITC', 'ITC Limited', 310.0, -0.3)
    ]
    
    c.executemany('INSERT OR REPLACE INTO stocks VALUES (?, ?, ?, ?)', stocks)
    
    nifty_lot_size = int(CONFIG.get('nifty_lot_size', 75))
    futures = [
        ('NIFTY24DEC', 'NIFTY 50', '2024-12-26', 24500.0, 0.8, nifty_lot_size),
        ('BANKNIFTY24DEC', 'BANK NIFTY', '2024-12-26', 52000.0, -0.5, 15),
        ('RELIANCE24DEC', 'RELIANCE FUT', '2024-12-26', 2505.0, 1.1, 250)
    ]
    
    c.executemany('INSERT OR REPLACE INTO futures VALUES (?, ?, ?, ?, ?, ?)', futures)
    
    # Generate NIFTY options chain
    options, greeks_data, current_nifty = generate_nifty_options()
    
    for opt in options:
        c.execute('INSERT OR REPLACE INTO options (symbol, strike, expiry, type, price, change_percent, lot_size) VALUES (?, ?, ?, ?, ?, ?, ?)', opt)
    
    c.executemany('INSERT OR REPLACE INTO greeks VALUES (?, ?, ?, ?, ?, ?)', greeks_data)
    
    conn.commit()
    conn.close()

@app.route('/')
def index():
    if 'user_id' not in session:
        return render_template('login.html')
    return render_template('dashboard.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form['username']
    password = hashlib.md5(request.form['password'].encode()).hexdigest()
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE username=? AND password=?', (username, password))
    user = c.fetchone()
    
    if user:
        session['user_id'] = user[0]
        session['username'] = username
        conn.close()
        return jsonify({'success': True})
    
    try:
        c.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, password))
        conn.commit()
        session['user_id'] = c.lastrowid
        session['username'] = username
        conn.close()
        return jsonify({'success': True})
    except:
        conn.close()
        return jsonify({'success': False, 'error': 'Login failed'})

@app.route('/logout')
def logout():
    session.clear()
    return render_template('login.html')

@app.route('/api/stocks')
def get_stocks():
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('SELECT * FROM stocks')
    stocks = c.fetchall()
    conn.close()
    
    stock_list = []
    for stock in stocks:
        price_change = random.uniform(-2, 2)
        new_price = round(stock[2] + (stock[2] * price_change / 100), 2)
        stock_list.append({
            'symbol': stock[0],
            'name': stock[1],
            'price': new_price,
            'change': round(price_change, 2)
        })
    
    return jsonify(stock_list)

@app.route('/api/portfolio')
def get_portfolio():
    if 'user_id' not in session:
        return jsonify([])
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('''SELECT p.symbol, p.quantity, p.avg_price, s.name, s.price 
                 FROM portfolio p JOIN stocks s ON p.symbol = s.symbol 
                 WHERE p.user_id = ?''', (session['user_id'],))
    portfolio = c.fetchall()
    conn.close()
    
    portfolio_list = []
    for item in portfolio:
        current_value = item[1] * item[4]
        invested_value = item[1] * item[2]
        pnl = current_value - invested_value
        portfolio_list.append({
            'symbol': item[0],
            'name': item[3],
            'quantity': item[1],
            'avg_price': item[2],
            'current_price': item[4],
            'pnl': round(pnl, 2),
            'pnl_percent': round((pnl / invested_value) * 100, 2) if invested_value > 0 else 0
        })
    
    return jsonify(portfolio_list)

@app.route('/api/balance')
def get_balance():
    if 'user_id' not in session:
        return jsonify({'balance': 0})
    
    try:
        conn = sqlite3.connect('trading.db', timeout=10)
        c = conn.cursor()
        c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
        result = c.fetchone()
        balance = result[0] if result else 0
        conn.close()
        return jsonify({'balance': balance})
    except Exception as e:
        return jsonify({'balance': 0, 'error': str(e)})

@app.route('/api/buy', methods=['POST'])
def buy_stock():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    data = request.json
    symbol = data['symbol']
    quantity = int(data['quantity'])
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    c.execute('SELECT price FROM stocks WHERE symbol = ?', (symbol,))
    price = c.fetchone()[0]
    
    total_cost = price * quantity
    
    c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
    balance = c.fetchone()[0]
    
    if balance < total_cost:
        conn.close()
        return jsonify({'success': False, 'error': 'Insufficient balance'})
    
    c.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (total_cost, session['user_id']))
    
    c.execute('SELECT quantity, avg_price FROM portfolio WHERE user_id = ? AND symbol = ?', 
              (session['user_id'], symbol))
    existing = c.fetchone()
    
    if existing:
        new_quantity = existing[0] + quantity
        new_avg_price = ((existing[0] * existing[1]) + (quantity * price)) / new_quantity
        c.execute('UPDATE portfolio SET quantity = ?, avg_price = ? WHERE user_id = ? AND symbol = ?',
                  (new_quantity, new_avg_price, session['user_id'], symbol))
    else:
        c.execute('INSERT INTO portfolio (user_id, symbol, quantity, avg_price) VALUES (?, ?, ?, ?)',
                  (session['user_id'], symbol, quantity, price))
    
    c.execute('INSERT INTO orders (user_id, symbol, type, quantity, price, status, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)',
              (session['user_id'], symbol, 'BUY', quantity, price, 'COMPLETED', datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/sell', methods=['POST'])
def sell_stock():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    data = request.json
    symbol = data['symbol']
    quantity = int(data['quantity'])
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    c.execute('SELECT quantity FROM portfolio WHERE user_id = ? AND symbol = ?', 
              (session['user_id'], symbol))
    existing = c.fetchone()
    
    if not existing or existing[0] < quantity:
        conn.close()
        return jsonify({'success': False, 'error': 'Insufficient shares'})
    
    c.execute('SELECT price FROM stocks WHERE symbol = ?', (symbol,))
    price = c.fetchone()[0]
    
    total_value = price * quantity
    
    c.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (total_value, session['user_id']))
    
    new_quantity = existing[0] - quantity
    if new_quantity == 0:
        c.execute('DELETE FROM portfolio WHERE user_id = ? AND symbol = ?', (session['user_id'], symbol))
    else:
        c.execute('UPDATE portfolio SET quantity = ? WHERE user_id = ? AND symbol = ?',
                  (new_quantity, session['user_id'], symbol))
    
    c.execute('INSERT INTO orders (user_id, symbol, type, quantity, price, status, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)',
              (session['user_id'], symbol, 'SELL', quantity, price, 'COMPLETED', datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/buy-option', methods=['POST'])
def buy_option():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    data = request.json
    symbol = data['symbol']
    quantity = int(data['quantity'])
    order_type = data['order_type']  # NRML or MIS
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    # Get option details
    c.execute('SELECT strike, expiry, type, price, lot_size FROM options WHERE symbol = ?', (symbol,))
    option = c.fetchone()
    
    if not option:
        conn.close()
        return jsonify({'success': False, 'error': 'Option not found'})
    
    strike, expiry, opt_type, price, lot_size = option
    total_lots = quantity
    total_quantity = total_lots * lot_size
    total_cost = price * total_quantity
    
    # Calculate margin (simplified)
    if order_type == 'NRML':
        margin = total_cost  # Full premium for buying
    else:  # MIS
        margin = total_cost * 0.8  # 80% margin for intraday
    
    c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
    balance = c.fetchone()[0]
    
    if balance < margin:
        conn.close()
        return jsonify({'success': False, 'error': f'Insufficient balance. Required: ₹{margin:.2f}'})
    
    # Update balance
    c.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (margin, session['user_id']))
    
    # Add to F&O portfolio
    c.execute('SELECT quantity FROM fo_portfolio WHERE user_id = ? AND symbol = ?', (session['user_id'], symbol))
    existing = c.fetchone()
    
    if existing:
        new_quantity = existing[0] + total_lots
        c.execute('UPDATE fo_portfolio SET quantity = ? WHERE user_id = ? AND symbol = ?', 
                  (new_quantity, session['user_id'], symbol))
    else:
        c.execute('INSERT INTO fo_portfolio (user_id, symbol, instrument_type, strike, expiry, quantity, avg_price) VALUES (?, ?, ?, ?, ?, ?, ?)',
                  (session['user_id'], symbol, opt_type, strike, expiry, total_lots, price))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/sell-option', methods=['POST'])
def sell_option():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    data = request.json
    symbol = data['symbol']
    quantity = int(data['quantity'])
    order_type = data['order_type']
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    # Check existing position
    c.execute('SELECT quantity FROM fo_portfolio WHERE user_id = ? AND symbol = ?', (session['user_id'], symbol))
    existing = c.fetchone()
    
    if not existing or existing[0] < quantity:
        conn.close()
        return jsonify({'success': False, 'error': 'Insufficient position'})
    
    # Get option details
    c.execute('SELECT price, lot_size FROM options WHERE symbol = ?', (symbol,))
    option = c.fetchone()
    price, lot_size = option
    
    total_quantity = quantity * lot_size
    total_value = price * total_quantity
    
    # Add to balance
    c.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (total_value, session['user_id']))
    
    # Update position
    new_quantity = existing[0] - quantity
    if new_quantity == 0:
        c.execute('DELETE FROM fo_portfolio WHERE user_id = ? AND symbol = ?', (session['user_id'], symbol))
    else:
        c.execute('UPDATE fo_portfolio SET quantity = ? WHERE user_id = ? AND symbol = ?',
                  (new_quantity, session['user_id'], symbol))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/fo-portfolio')
def get_fo_portfolio():
    if 'user_id' not in session:
        return jsonify([])
    
    try:
        conn = sqlite3.connect('trading.db', timeout=10)
        c = conn.cursor()
        c.execute('''SELECT fp.symbol, fp.quantity, fp.avg_price, fp.strike, fp.expiry, fp.instrument_type,
                            o.price as current_price, o.lot_size
                     FROM fo_portfolio fp 
                     JOIN options o ON fp.symbol = o.symbol 
                     WHERE fp.user_id = ?''', (session['user_id'],))
        portfolio = c.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)})
    
    portfolio_list = []
    for item in portfolio:
        current_value = item[1] * item[7] * item[6]  # quantity * lot_size * current_price
        invested_value = item[1] * item[7] * item[2]  # quantity * lot_size * avg_price
        pnl = current_value - invested_value
        
        portfolio_list.append({
            'symbol': item[0],
            'quantity': item[1],
            'avg_price': item[2],
            'strike': item[3],
            'expiry': item[4],
            'type': item[5],
            'current_price': item[6],
            'lot_size': item[7],
            'pnl': round(pnl, 2),
            'pnl_percent': round((pnl / invested_value) * 100, 2) if invested_value > 0 else 0,
            'total_value': round(current_value, 2),
            'invested_value': round(invested_value, 2)
        })
    
    return jsonify(portfolio_list)

@app.route('/api/futures')
def get_futures():
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('SELECT * FROM futures')
    futures = c.fetchall()
    conn.close()
    
    futures_list = []
    for fut in futures:
        price_change = random.uniform(-3, 3)
        new_price = round(fut[3] + (fut[3] * price_change / 100), 2)
        futures_list.append({
            'symbol': fut[0],
            'name': fut[1],
            'expiry': fut[2],
            'price': new_price,
            'change': round(price_change, 2),
            'lot_size': fut[5]
        })
    
    return jsonify(futures_list)

@app.route('/api/options')
def get_options():
    search = request.args.get('search', '')
    atm_only = request.args.get('atm', 'true').lower() == 'true'
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    if atm_only and not search:
        # Get current NIFTY price and show ATM options
        nifty_price = get_real_nifty_price()
        atm_range = 250  # ±5 strikes (5 * 50)
        
        c.execute('''SELECT o.*, g.delta, g.gamma, g.theta, g.vega, g.iv 
                     FROM options o LEFT JOIN greeks g ON o.symbol = g.symbol
                     WHERE o.strike BETWEEN ? AND ?
                     ORDER BY o.strike, o.type''', 
                  (nifty_price - atm_range, nifty_price + atm_range))
    elif search:
        # Search functionality
        c.execute('''SELECT o.*, g.delta, g.gamma, g.theta, g.vega, g.iv 
                     FROM options o LEFT JOIN greeks g ON o.symbol = g.symbol
                     WHERE o.symbol LIKE ? OR CAST(o.strike AS TEXT) LIKE ?
                     ORDER BY o.strike, o.type''', 
                  (f'%{search}%', f'%{search}%'))
    else:
        c.execute('''SELECT o.*, g.delta, g.gamma, g.theta, g.vega, g.iv 
                     FROM options o LEFT JOIN greeks g ON o.symbol = g.symbol
                     ORDER BY o.strike, o.type''')
    
    options = c.fetchall()
    conn.close()
    
    options_list = []
    for opt in options:
        # Use stored price directly without random changes
        actual_price = opt[5]  # Use the price from database
        price_change = opt[6]  # Use stored change percentage
        
        options_list.append({
            'id': opt[0],
            'symbol': opt[1],
            'strike': opt[2],
            'expiry': opt[3],
            'type': opt[4],
            'price': actual_price,
            'change': price_change,
            'lot_size': opt[7],
            'delta': opt[8] if opt[8] else 0,
            'gamma': opt[9] if opt[9] else 0,
            'theta': opt[10] if opt[10] else 0,
            'vega': opt[11] if opt[11] else 0,
            'iv': opt[12] if opt[12] else 0
        })
    
    return jsonify(options_list)

@app.route('/api/strategies')
def get_strategies():
    if 'user_id' not in session:
        return jsonify([])
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('SELECT * FROM strategies WHERE user_id = ?', (session['user_id'],))
    strategies = c.fetchall()
    
    strategy_list = []
    for s in strategies:
        conditions = eval(s[4]) if s[4] else {}  # Parse conditions JSON
        execution_count = s[7] if len(s) > 7 else 0
        
        # Check if strategy has active positions
        strategy_symbols = get_strategy_symbols(s[0], conditions, s[3])  # Pass strategy type
        has_active_positions = False
        for symbol in strategy_symbols:
            c.execute('SELECT quantity FROM fo_portfolio WHERE user_id = ? AND symbol = ?', (session['user_id'], symbol))
            if c.fetchone():
                has_active_positions = True
                break
        
        # Update status based on positions
        current_status = 'executed' if has_active_positions else 'active'
        
        strategy_list.append({
            'id': s[0],
            'name': s[2],
            'type': s[3],
            'status': current_status,
            'execution_count': execution_count,
            'maxLossPercent': conditions.get('max_loss_percent', 0),
            'strike': conditions.get('strike', 0),
            'lots': conditions.get('lots', 1),
            'stopLossType': conditions.get('stop_loss_type', ''),
            'stopLossPercent': conditions.get('stop_loss_percent', 0),
            'targetProfit': conditions.get('target_profit', 0)
        })
    
    conn.close()
    return jsonify(strategy_list)

def get_strategy_symbols(strategy_id, conditions, strategy_type=None):
    """Get expected symbols for a strategy based on its configuration"""
    strike = conditions.get('strike', 24500)
    if not strategy_type:
        strategy_type = conditions.get('type', 'long_straddle')
    
    symbols = []
    if strategy_type in ['long_straddle', 'short_straddle']:
        symbols = [f'NIFTY{strike}CE', f'NIFTY{strike}PE']
    elif strategy_type in ['long_strangle', 'short_strangle']:
        ce_strike = strike + 100
        pe_strike = strike - 100
        symbols = [f'NIFTY{ce_strike}CE', f'NIFTY{pe_strike}PE']
    else:
        symbols = [f'NIFTY{strike}CE', f'NIFTY{strike}PE']
    
    return symbols

@app.route('/api/refresh-options', methods=['POST'])
def refresh_options():
    """Refresh NIFTY options chain with latest data"""
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    # Clear existing options and greeks
    c.execute('DELETE FROM options')
    c.execute('DELETE FROM greeks')
    
    # Generate fresh options
    options, greeks_data, current_nifty = generate_nifty_options()
    
    for opt in options:
        c.execute('INSERT INTO options (symbol, strike, expiry, type, price, change_percent, lot_size) VALUES (?, ?, ?, ?, ?, ?, ?)', opt)
    
    c.executemany('INSERT INTO greeks VALUES (?, ?, ?, ?, ?, ?)', greeks_data)
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': f'Generated {len(options)} options'})

@app.route('/api/nifty-price')
def get_nifty_price():
    """Get current NIFTY 50 price"""
    price = get_real_nifty_price()
    
    # Add some realistic market data
    change = random.uniform(-50, 50)  # Realistic daily change
    change_percent = (change / price) * 100
    
    return jsonify({
        'price': price, 
        'symbol': 'NIFTY 50',
        'change': round(change, 2),
        'changePercent': round(change_percent, 2)
    })

def get_real_options_data():
    """Fetch real NIFTY options data from NSE"""
    try:
        # NSE Options Chain API
        url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get('records', {}).get('data', [])
    except:
        pass
    
    return None

@app.route('/api/real-options')
def get_real_options():
    """Get real options data from NSE"""
    real_data = get_real_options_data()
    
    if not real_data:
        # Fallback to existing logic if API fails
        return get_options()
    
    options_list = []
    nifty_price = get_real_nifty_price()
    
    for item in real_data[:20]:  # Limit to 20 strikes around ATM
        strike = item.get('strikePrice', 0)
        
        # Skip if too far from current price
        if abs(strike - nifty_price) > 500:
            continue
            
        # CE Option
        if 'CE' in item:
            ce_data = item['CE']
            options_list.append({
                'symbol': f'NIFTY{strike}CE',
                'strike': strike,
                'expiry': '2024-11-28',  # Current month expiry
                'type': 'CE',
                'price': ce_data.get('lastPrice', 0),
                'change': ce_data.get('change', 0),
                'lot_size': int(CONFIG.get('nifty_lot_size', 75)),
                'delta': 0.5,  # Simplified
                'gamma': 0.001,
                'theta': -10,
                'vega': 15,
                'iv': ce_data.get('impliedVolatility', 20) / 100
            })
        
        # PE Option
        if 'PE' in item:
            pe_data = item['PE']
            options_list.append({
                'symbol': f'NIFTY{strike}PE',
                'strike': strike,
                'expiry': '2024-11-28',
                'type': 'PE',
                'price': pe_data.get('lastPrice', 0),
                'change': pe_data.get('change', 0),
                'lot_size': 25,
                'delta': -0.5,  # Simplified
                'gamma': 0.001,
                'theta': -10,
                'vega': 15,
                'iv': pe_data.get('impliedVolatility', 20) / 100
            })
    
    return jsonify(options_list)

@app.route('/api/autocomplete-options')
def autocomplete_options():
    """Auto-complete options based on strike and type"""
    query = request.args.get('q', '').strip().upper()
    
    if len(query) < 3:  # Minimum 3 characters
        return jsonify([])
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    # Parse query for strike and type (e.g., "25500 CE" or "25600 PE")
    parts = query.split()
    if len(parts) >= 2:
        try:
            strike = int(parts[0])
            opt_type = parts[1]
            
            # Get options for this strike and type across weekly expiries
            c.execute('''SELECT o.*, g.delta, g.gamma, g.theta, g.vega, g.iv 
                         FROM options o LEFT JOIN greeks g ON o.symbol = g.symbol
                         WHERE o.strike = ? AND o.type = ?
                         ORDER BY o.expiry''', (strike, opt_type))
        except ValueError:
            # If not a valid strike, search by symbol
            c.execute('''SELECT o.*, g.delta, g.gamma, g.theta, g.vega, g.iv 
                         FROM options o LEFT JOIN greeks g ON o.symbol = g.symbol
                         WHERE o.symbol LIKE ?
                         ORDER BY o.strike, o.expiry''', (f'%{query}%',))
    else:
        # Search by partial match
        c.execute('''SELECT o.*, g.delta, g.gamma, g.theta, g.vega, g.iv 
                     FROM options o LEFT JOIN greeks g ON o.symbol = g.symbol
                     WHERE CAST(o.strike AS TEXT) LIKE ? OR o.type LIKE ?
                     ORDER BY o.strike, o.expiry
                     LIMIT 20''', (f'%{query}%', f'%{query}%'))
    
    options = c.fetchall()
    conn.close()
    
    suggestions = []
    for opt in options:
        price_change = random.uniform(-5, 5)
        new_price = max(0.05, round(opt[5] + (opt[5] * price_change / 100), 2))
        suggestions.append({
            'symbol': opt[1],
            'strike': opt[2],
            'expiry': opt[3],
            'type': opt[4],
            'price': new_price,
            'change': round(price_change, 2),
            'lot_size': opt[7],
            'delta': opt[8] if opt[8] else 0,
            'gamma': opt[9] if opt[9] else 0,
            'theta': opt[10] if opt[10] else 0,
            'vega': opt[11] if opt[11] else 0,
            'iv': opt[12] if opt[12] else 0
        })
    
    return jsonify(suggestions[:8])  # Limit to 8 suggestions

@app.route('/api/exit-all-positions', methods=['POST'])
def exit_all_positions():
    """Exit all F&O positions for the user"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    # Get all positions for the user
    c.execute('SELECT symbol, quantity FROM fo_portfolio WHERE user_id = ?', (session['user_id'],))
    positions = c.fetchall()
    
    if not positions:
        conn.close()
        return jsonify({'success': False, 'error': 'No positions to exit'})
    
    total_value = 0
    positions_count = len(positions)
    
    for symbol, quantity in positions:
        # Get current option price
        c.execute('SELECT price, lot_size FROM options WHERE symbol = ?', (symbol,))
        option = c.fetchone()
        
        if option:
            price, lot_size = option
            position_value = price * quantity * lot_size
            total_value += position_value
    
    # Add total value to balance
    c.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (total_value, session['user_id']))
    
    # Clear all positions
    c.execute('DELETE FROM fo_portfolio WHERE user_id = ?', (session['user_id'],))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True, 
        'message': f'Exited {positions_count} positions. ₹{total_value:.2f} credited to account.'
    })

@app.route('/api/create-strategy', methods=['POST'])
def create_strategy():
    """Create a new options strategy"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    data = request.json
    name = data['name']
    strategy_type = data['type']
    strike = data['strike']
    expiry = data['expiry']
    lots = data['lots']
    max_loss_percent = data['maxLossPercent']
    
    # Get user's current balance for risk calculation
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
    balance = c.fetchone()[0]
    
    max_loss_amount = (balance * max_loss_percent) / 100
    
    # Create strategy conditions JSON
    conditions = {
        'strike': strike,
        'expiry': expiry,
        'lots': lots,
        'type': strategy_type,
        'max_loss_percent': max_loss_percent,
        'max_loss_amount': max_loss_amount
    }
    
    # Add custom strategy options if provided
    if 'stopLossType' in data:
        conditions['stop_loss_type'] = data['stopLossType']
        conditions['stop_loss_percent'] = data['stopLossPercent']
        conditions['target_profit'] = data['targetProfit']
    
    # Insert strategy
    c.execute('INSERT INTO strategies (user_id, name, type, conditions, status, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              (session['user_id'], name, strategy_type, str(conditions), 'active', datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/market-status')
def get_market_status():
    """Get current market status"""
    market_open, message = is_market_open()
    
    if not market_open:
        next_open = get_next_market_open()
        ist = pytz.timezone('Asia/Kolkata')
        now = datetime.now(ist)
        time_until_open = next_open - now
        
        return jsonify({
            'isOpen': False,
            'message': message,
            'nextOpen': next_open.strftime('%Y-%m-%d %H:%M:%S'),
            'hoursUntilOpen': int(time_until_open.total_seconds() // 3600),
            'minutesUntilOpen': int((time_until_open.total_seconds() % 3600) // 60)
        })
    
    return jsonify({
        'isOpen': True,
        'message': message
    })

@app.route('/api/place-strategy-order/<int:strategy_id>', methods=['POST'])
def place_strategy_order(strategy_id):
    """Place orders for a strategy (allowed even when market is closed)"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    # Get strategy details
    c.execute('SELECT * FROM strategies WHERE id = ? AND user_id = ?', 
              (strategy_id, session['user_id']))
    strategy = c.fetchone()
    
    if not strategy:
        conn.close()
        return jsonify({'success': False, 'error': 'Strategy not found'})
    
    strategy_name, strategy_type, conditions_str = strategy[2], strategy[3], strategy[4]
    execution_count = strategy[7] if len(strategy) > 7 and strategy[7] else 0
    conditions = eval(conditions_str) if conditions_str else {}
    
    strike = conditions.get('strike', 24500)
    lots = conditions.get('lots', 1)
    
    # Get current NIFTY price for strategy execution
    nifty_price = get_real_nifty_price()
    
    orders_placed = []
    total_cost = 0
    
    try:
        # Execute different strategy types
        if strategy_type == 'long_straddle':
            # Buy ATM Call + Buy ATM Put
            ce_symbol = f'NIFTY{strike}CE'
            pe_symbol = f'NIFTY{strike}PE'
            
            # Get option prices (simplified)
            ce_price = 150  # Simplified pricing
            pe_price = 150
            
            # Place CE order
            place_option_order(session['user_id'], ce_symbol, lots, ce_price, 'CE', strike)
            orders_placed.append(f'{lots} lots {ce_symbol}')
            nifty_lot_size = int(CONFIG.get('nifty_lot_size', 75))
            total_cost += ce_price * lots * nifty_lot_size
            
            # Place PE order
            place_option_order(session['user_id'], pe_symbol, lots, pe_price, 'PE', strike)
            orders_placed.append(f'{lots} lots {pe_symbol}')
            total_cost += pe_price * lots * nifty_lot_size
            
        elif strategy_type == 'long_strangle':
            # Buy OTM Call + Buy OTM Put
            ce_strike = strike + 100
            pe_strike = strike - 100
            ce_symbol = f'NIFTY{ce_strike}CE'
            pe_symbol = f'NIFTY{pe_strike}PE'
            
            ce_price = 80
            pe_price = 80
            
            place_option_order(session['user_id'], ce_symbol, lots, ce_price, 'CE', ce_strike)
            place_option_order(session['user_id'], pe_symbol, lots, pe_price, 'PE', pe_strike)
            orders_placed.extend([f'{lots} lots {ce_symbol}', f'{lots} lots {pe_symbol}'])
            total_cost += (ce_price + pe_price) * lots * nifty_lot_size
            
        else:
            # Custom or other strategies
            ce_symbol = f'NIFTY{strike}CE'
            pe_symbol = f'NIFTY{strike}PE'
            ce_price = pe_price = 120
            
            place_option_order(session['user_id'], ce_symbol, lots, ce_price, 'CE', strike)
            place_option_order(session['user_id'], pe_symbol, lots, pe_price, 'PE', strike)
            orders_placed.extend([f'{lots} lots {ce_symbol}', f'{lots} lots {pe_symbol}'])
            total_cost += (ce_price + pe_price) * lots * nifty_lot_size
        
        # Update user balance
        c.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (total_cost, session['user_id']))
        
        # Update strategy execution count and status
        c.execute('UPDATE strategies SET execution_count = ?, status = ? WHERE id = ?', 
                  (execution_count + 1, 'executed', strategy_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': f'Strategy executed! Orders: {", ".join(orders_placed)}. Cost: ₹{total_cost:.2f}'
        })
        
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': f'Failed to execute strategy: {str(e)}'})

def place_option_order(user_id, symbol, lots, price, option_type, strike):
    """Helper function to place individual option orders"""
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    # Ensure option exists in options table for portfolio display
    c.execute('SELECT symbol FROM options WHERE symbol = ?', (symbol,))
    if not c.fetchone():
        # Create option entry if it doesn't exist
        nifty_lot_size = int(CONFIG.get('nifty_lot_size', 75))
        c.execute('INSERT INTO options (symbol, strike, expiry, type, price, change_percent, lot_size) VALUES (?, ?, ?, ?, ?, ?, ?)',
                  (symbol, strike, '2024-11-28', option_type, price, 0, nifty_lot_size))
    
    # Add to F&O portfolio
    c.execute('SELECT quantity FROM fo_portfolio WHERE user_id = ? AND symbol = ?', (user_id, symbol))
    existing = c.fetchone()
    
    if existing:
        new_quantity = existing[0] + lots
        c.execute('UPDATE fo_portfolio SET quantity = ? WHERE user_id = ? AND symbol = ?', 
                  (new_quantity, user_id, symbol))
    else:
        c.execute('INSERT INTO fo_portfolio (user_id, symbol, instrument_type, strike, expiry, quantity, avg_price) VALUES (?, ?, ?, ?, ?, ?, ?)',
                  (user_id, symbol, option_type, strike, '2024-11-28', lots, price))
    
    conn.commit()
    conn.close()

@app.route('/api/delete-strategy/<int:strategy_id>', methods=['DELETE'])
def delete_strategy(strategy_id):
    """Delete a strategy"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('DELETE FROM strategies WHERE id = ? AND user_id = ?', (strategy_id, session['user_id']))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/add-funds', methods=['POST'])
def add_funds():
    """Add funds to user wallet"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    data = request.json
    print(f"Add funds request: {data}")
    amount = float(data.get('amount', 0))
    payment_method = data.get('paymentMethod', '')
    
    if amount < 100 or amount > 500000:
        print(f"Add funds: Invalid amount {amount}")
        return jsonify({'success': False, 'error': 'Invalid amount. Must be between ₹100 and ₹5,00,000'})
    
    try:
        conn = sqlite3.connect('trading.db', timeout=10)
        c = conn.cursor()
        # Check current balance before update
        c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
        result = c.fetchone()
        if not result:
            conn.close()
            return jsonify({'success': False, 'error': 'User not found'})
        old_balance = result[0]
        print(f"Old balance: {old_balance}")
        
        c.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (amount, session['user_id']))
        
        # Check new balance after update
        c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
        balance_result = c.fetchone()
        if not balance_result:
            conn.close()
            return jsonify({'success': False, 'error': 'Failed to update balance'})
        new_balance = balance_result[0]
        print(f"New balance: {new_balance}")
        
        conn.commit()
        conn.close()
        
        print(f"Add funds successful: {amount} added via {payment_method}")
        return jsonify({
            'success': True, 
            'message': f'₹{amount:,.2f} added successfully via {payment_method}'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': f'Database error: {str(e)}'})

@app.route('/api/withdraw-funds', methods=['POST'])
def withdraw_funds():
    """Withdraw funds from user wallet"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    data = request.json
    amount = float(data.get('amount', 0))
    
    if amount < 100 or amount > 500000:
        return jsonify({'success': False, 'error': 'Invalid amount. Must be between ₹100 and ₹5,00,000'})
    
    try:
        conn = sqlite3.connect('trading.db', timeout=10)
        c = conn.cursor()
        
        # Check current balance
        c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
        result = c.fetchone()
        current_balance = result[0] if result else 0
        
        if current_balance < amount:
            conn.close()
            return jsonify({'success': False, 'error': 'Insufficient balance'})
        
        # Deduct funds from user balance
        c.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (amount, session['user_id']))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': f'₹{amount:,.2f} withdrawn successfully. Funds will be credited to your bank account within 2-3 business days.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': f'Database error: {str(e)}'})

@app.route('/api/update-lot-sizes', methods=['POST'])
def update_lot_sizes():
    """Update NIFTY options lot sizes to current config value"""
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    
    nifty_lot_size = int(CONFIG.get('nifty_lot_size', 75))
    
    # Update all NIFTY options to use current lot size
    c.execute('UPDATE options SET lot_size = ? WHERE symbol LIKE "NIFTY%"', (nifty_lot_size,))
    
    # Update futures lot size too
    c.execute('UPDATE futures SET lot_size = ? WHERE symbol LIKE "NIFTY%"', (nifty_lot_size,))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True, 
        'message': f'Updated all NIFTY options and futures to lot size {nifty_lot_size}'
    })

@app.route('/api/update-option-prices', methods=['POST'])
def update_option_prices():
    """Update option prices with realistic fluctuations - DISABLED to prevent database locks"""
    # Disabled to prevent database locking issues during fund operations
    return jsonify({
        'success': True, 
        'message': 'Option price updates disabled to prevent database locks'
    })

if __name__ == '__main__':
    init_db()
    # Update lot sizes on startup to ensure consistency
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    nifty_lot_size = int(CONFIG.get('nifty_lot_size', 75))
    c.execute('UPDATE options SET lot_size = ? WHERE symbol LIKE "NIFTY%"', (nifty_lot_size,))
    c.execute('UPDATE futures SET lot_size = ? WHERE symbol LIKE "NIFTY%"', (nifty_lot_size,))
    conn.commit()
    conn.close()
    app.run(host='0.0.0.0', port=5000, debug=True)