# OptionsPaye - Advanced Options Trading Platform

A specialized Options trading platform with Greeks analysis and strategy tools built with Flask and SQLite.

## Features

- User authentication (login/register)
- Real-time Options prices with Greeks (Delta, Gamma, Theta, Vega)
- Options chain analysis
- Advanced options strategies
- Risk management with Greeks
- P&L tracking with Greeks impact
- Algorithmic trading capabilities
- Options portfolio optimization

## Quick Start

### Using Docker (Recommended)

1. Build and run the container:
```bash
docker-compose up --build
```

2. Access the application at `http://localhost:5000`

### Manual Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Access at `http://localhost:5000`

## Default Setup

- Starting balance: ₹100,000
- Pre-loaded stocks: RELIANCE, TCS, INFY, HDFC, ICICI, WIPRO, BHARTI, ITC
- Prices update every 5 seconds (simulated)

## Usage

1. Create account by entering username/password
2. View market watch with live prices
3. Buy stocks using available balance
4. Sell stocks from portfolio
5. Track P&L in real-time

## Database

SQLite database stores:
- User accounts and balances
- Stock information
- Portfolio holdings
- Order history