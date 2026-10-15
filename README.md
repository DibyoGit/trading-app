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

## Trading terminal (home page): virtual-money index F&O

After logging in, `/` opens the terminal: a watchlist with search, an intraday chart, an option chain, an order ticket, and Orders / Positions / Funds views.

- **Instruments:** NIFTY and BANKNIFTY options and futures. NIFTY has weekly expiries every Tuesday plus monthly ones; BANKNIFTY and all futures are monthly (last Tuesday). When an expiry falls on an NSE holiday it moves to the previous trading day (2026 list built in; override with `nse_holidays`).
- **Prices:** the index level comes from Yahoo (delayed). Option premiums are modelled with Black-Scholes: at-the-money IV = live India VIX, a put skew fitted to NSE weekly quotes, and time to expiry counted in trading minutes. In a check against live Kite quotes the model landed within about 4% of the market price. Futures use cost of carry. Bid/ask spreads are synthetic.
- **Orders:** Market, Limit and SL-M. Products are MIS (auto squared off at 15:20 IST) and NRML. Expired contracts settle at intrinsic value.
- **Margin:** buying options needs the full premium. Short options and futures block 12% of notional. Charges are ₹20 per order plus 0.1% STT on option sells.
- **Accounts:** ₹10,00,000 virtual capital, which can be reset at any time.
- **Login:** salted scrypt password hashes. Accounts from the legacy app (unsalted MD5) are upgraded automatically at their next login.
- Settings live in `config.properties`: `virtual_capital`, `max_lots_per_order`, `enforce_market_hours`, `order_match_seconds`.
- The old dashboard (random demo prices) is still reachable at `/legacy`.

```
trader/
  clock.py        IST clock, market hours, MIS square-off time
  instruments.py  expiry calendar, instrument keys, search
  market.py       index quotes, option/future pricing, chain, candles
  broker.py       orders, fills, margin, positions, settlement
  auth.py         login / registration
  web.py          /auth/* and /api/t/* routes, order-matching scheduler
```

## Strategy Studio (v1): bring your own strategy

Open `/studio` after logging in. The platform supplies **tooling only**: data, option pricing, backtesting, model training and paper execution. Customers write their own strategies and train their own models. The platform never publishes buy/sell calls.

| Step | What the user does |
|------|--------------------|
| 1. Strategies | Write rules (`rsi_14 < 30 and close > sma_200`), choose option legs and risk limits. Optionally describe the strategy in plain English and Claude converts it into rules. Claude only translates; it does not invent strategies. |
| 2. AI Models | Pick features, a target (`direction`, `big_move`, `vol_expansion`) and a model (logistic regression / random forest / gradient boosting). Validation is walk-forward with an embargo gap, and results are reported against a naive baseline. The model output becomes `model_<name>`, which rules can read. |
| 3. Backtest | Event-driven simulator: decisions are made at the bar close and filled at the next open, with slippage, brokerage and STT. Option premiums use Black-Scholes on historical vol. |
| 4. Paper Trading | Deploy a strategy with virtual capital. The same simulator advances it on each new daily bar (automatically every `paper_tick_seconds`, or with "Update now"). |
| Integrations | A webhook (`POST /api/v2/webhook`, `X-API-Key`) for strategies that run on the customer's own infrastructure, FinBERT news sentiment (Hugging Face), and a Python plugin SDK for self-hosted operators. |

Compliance-oriented design: a consent gate, per-deployment "this is my strategy" confirmation, an append-only audit log of every strategy, model, deployment and webhook intent, no platform-authored signals, and paper trading only.

### Layout
```
engine/
  pricing.py        Black-Scholes price, greeks, IV
  data.py           Yahoo daily bars + disk cache, synthetic fallback
  indicators.py     name-based indicators (sma_N, rsi_N, hv_N, ...)
  rules.py          safe AST rule DSL (no code execution)
  strategy_base.py  SDK: Strategy, Leg, RiskLimits, Context
  simulator.py      shared backtest / paper engine + metrics
  ai/models.py      walk-forward model training (scikit-learn)
  ai/sentiment.py   FinBERT via transformers or the HF Inference API
  ai/assistant.py   plain-English -> rule spec (Claude)
  store.py          SQLite tables, audit log, API keys
  service.py        orchestration + paper scheduler
  web.py            /studio page and /api/v2 routes
plugins/            operator-installed Python strategies
tests/              python -m pytest tests
```

### Optional AI backends
- Strategy translator: `pip install anthropic` and set `ANTHROPIC_API_KEY`.
- Sentiment: `pip install transformers torch` (local CPU), or set `HF_TOKEN` to use the Hugging Face API.

### Known limits of v1
- Daily bars only. Option prices are modelled, not historical quotes.
- No live broker execution. Use `engine/simulator.py` fills as the seam for a broker adapter.
- Set `SECRET_KEY` in production.
