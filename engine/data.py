"""Market data: Yahoo Finance daily bars with on-disk cache, synthetic fallback.

Caching keeps repeat backtests/training runs off the network (cheaper, faster,
and kinder to the data source).
"""
import os
import time

import numpy as np
import pandas as pd

UNDERLYINGS = {
    'NIFTY': {'ticker': '^NSEI', 'strike_step': 50, 'lot_size_key': 'nifty_lot_size', 'default_lot': 65},
    'BANKNIFTY': {'ticker': '^NSEBANK', 'strike_step': 100, 'lot_size_key': 'banknifty_lot_size', 'default_lot': 30},
}

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'cache')
CACHE_TTL_SECONDS = 6 * 3600


def underlying_meta(name, config=None):
    meta = dict(UNDERLYINGS.get(name.upper(), UNDERLYINGS['NIFTY']))
    config = config or {}
    meta['lot_size'] = int(config.get(meta['lot_size_key'], meta['default_lot']))
    return meta


def _cache_path(name, period):
    return os.path.join(CACHE_DIR, f'{name.upper()}_{period}_1d.csv')


def _synthetic(name, periods=1000, seed=7):
    """Deterministic GBM series for offline use. Clearly flagged as synthetic."""
    rng = np.random.default_rng(seed)
    start = 24000.0 if name.upper() == 'NIFTY' else 52000.0
    rets = rng.normal(0.0004, 0.011, periods)
    close = start * np.exp(np.cumsum(rets))
    open_ = close * (1 + rng.normal(0, 0.003, periods))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, periods)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, periods)))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=periods)
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close,
                         'volume': rng.integers(1e5, 5e5, periods)}, index=idx)


def load_bars(name='NIFTY', period='5y', allow_synthetic=True):
    """Return (DataFrame[open, high, low, close, volume], source)."""
    name = name.upper()
    path = _cache_path(name, period)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL_SECONDS:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df, 'yahoo (cached)'

    try:
        import yfinance as yf
        raw = yf.download(UNDERLYINGS[name]['ticker'], period=period, interval='1d',
                          auto_adjust=False, progress=False)
        if raw is not None and len(raw) > 100:
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            df = raw.rename(columns=str.lower)[['open', 'high', 'low', 'close', 'volume']].dropna()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            os.makedirs(CACHE_DIR, exist_ok=True)
            df.to_csv(path)
            return df, 'yahoo'
    except Exception as exc:  # network down, package missing, ticker change
        print(f'[data] yahoo fetch failed for {name}: {exc}')

    if os.path.exists(path):
        return pd.read_csv(path, index_col=0, parse_dates=True), 'yahoo (stale cache)'
    if not allow_synthetic:
        raise RuntimeError(f'No market data available for {name}')
    return _synthetic(name), 'synthetic'
