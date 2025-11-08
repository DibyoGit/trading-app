from flask import Flask, render_template, request, jsonify, session
import sqlite3
import hashlib
import random
from datetime import datetime, timedelta
import math
import requests

app = Flask(__name__)
app.secret_key = 'trading_secret_key'

def get_real_nifty_price():
    """Fetch real NIFTY 50 closing price from API"""
    try:
        # Try Yahoo Finance API first
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
        response = requests.get(url, timeout=5)
        data = response.json()
        
        if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
            result = data['chart']['result'][0]
            if 'meta' in result and 'regularMarketPrice' in result['meta']:
                price = result['meta']['regularMarketPrice']
                # Validate price is realistic (between 15000-30000)
                if 15000 <= price <= 30000:
                    return round(price, 2)
    except:
        pass
    
    try:
        # Alternative API - NSE India (if available)
        url = "https://www.nseindia.com/api/allIndices"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        
        for index in data['data']:
            if index['index'] == 'NIFTY 50':
                price = float(index['last'])
                if 15000 <= price <= 30000:
                    return round(price, 2)
    except:
        pass
    
    # Fallback to realistic current NIFTY price
    return 24350.75

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
        
        if opt_type == 'CE':
            price = max(5, (nifty_price - strike) + random.uniform(10, 100)) if strike <= nifty_price else random.uniform(5, 50)
            delta = max(0.05, min(0.95, 0.5 + (nifty_price - strike) / 1000))
        else:  # PE
            price = max(5, (strike - nifty_price) + random.uniform(10, 100)) if strike >= nifty_price else random.uniform(5, 50)
            delta = max(-0.95, min(-0.05, -0.5 + (nifty_price - strike) / 1000))
        
        symbol = f'NIFTY{expiry_date.replace("-", "")[-4:]}{int(strike)}{exp_type}{opt_type}'
        
        gamma = 0.001 + random.uniform(0, 0.005)
        theta = -random.uniform(5, 20)
        vega = random.uniform(10, 25)
        iv = random.uniform(0.12, 0.25)
        
        return (symbol, strike, expiry_date, opt_type, price, random.uniform(-5, 5), 25), (symbol, delta, gamma, theta, vega, iv)
    
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
                  conditions TEXT, status TEXT, created_at TEXT)''')
    
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
    
    futures = [
        ('NIFTY24DEC', 'NIFTY 50', '2024-12-26', 24500.0, 0.8, 25),
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
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('SELECT balance FROM users WHERE id = ?', (session['user_id'],))
    balance = c.fetchone()[0]
    conn.close()
    
    return jsonify({'balance': balance})

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
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('''SELECT fp.symbol, fp.quantity, fp.avg_price, fp.strike, fp.expiry, fp.instrument_type,
                        o.price as current_price, o.lot_size
                 FROM fo_portfolio fp 
                 JOIN options o ON fp.symbol = o.symbol 
                 WHERE fp.user_id = ?''', (session['user_id'],))
    portfolio = c.fetchall()
    conn.close()
    
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
            'pnl_percent': round((pnl / invested_value) * 100, 2) if invested_value > 0 else 0
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
        price_change = random.uniform(-5, 5)
        new_price = max(0.05, round(opt[5] + (opt[5] * price_change / 100), 2))
        options_list.append({
            'id': opt[0],
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
    
    return jsonify(options_list)

@app.route('/api/strategies')
def get_strategies():
    if 'user_id' not in session:
        return jsonify([])
    
    conn = sqlite3.connect('trading.db')
    c = conn.cursor()
    c.execute('SELECT * FROM strategies WHERE user_id = ?', (session['user_id'],))
    strategies = c.fetchall()
    conn.close()
    
    strategy_list = []
    for s in strategies:
        conditions = eval(s[4]) if s[4] else {}  # Parse conditions JSON
        strategy_list.append({
            'id': s[0],
            'name': s[2],
            'type': s[3],
            'status': s[5],
            'maxLossPercent': conditions.get('max_loss_percent', 0),
            'strike': conditions.get('strike', 0),
            'lots': conditions.get('lots', 1)
        })
    
    return jsonify(strategy_list)

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
        'max_loss_percent': max_loss_percent,
        'max_loss_amount': max_loss_amount
    }
    
    # Insert strategy
    c.execute('INSERT INTO strategies (user_id, name, type, conditions, status, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              (session['user_id'], name, strategy_type, str(conditions), 'active', datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

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

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)