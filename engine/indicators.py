"""Indicator library with name-based lookup (e.g. 'sma_20', 'rsi_14').

Every value at row i uses only data up to and including bar i, so strategies
evaluated on bar close cannot peek into the future.
"""
import re

import numpy as np
import pandas as pd

BASE_COLUMNS = ('open', 'high', 'low', 'close', 'volume')


def sma(s, n):
    return s.rolling(n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n):
    prev = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - prev).abs(), (df['low'] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def hv(s, n):
    """Annualised historical volatility (decimal) from log returns."""
    return np.log(s / s.shift(1)).rolling(n).std() * np.sqrt(252)


def ret(s, n):
    """n-bar percentage return."""
    return s.pct_change(n) * 100


def zscore(s, n):
    return (s - s.rolling(n).mean()) / s.rolling(n).std()


def bb_width(s, n):
    m = s.rolling(n).mean()
    return 4 * s.rolling(n).std() / m * 100


SERIES_FUNCS = {'sma': sma, 'ema': ema, 'rsi': rsi, 'hv': hv, 'ret': ret, 'zscore': zscore, 'bbw': bb_width}
FRAME_FUNCS = {'atr': atr}
NAME_RE = re.compile(r'^([a-z]+)_(\d+)$')

CATALOG = [
    {'name': 'open / high / low / close / volume', 'desc': 'Raw daily bar values'},
    {'name': 'sma_N', 'desc': 'Simple moving average of close over N bars'},
    {'name': 'ema_N', 'desc': 'Exponential moving average of close'},
    {'name': 'rsi_N', 'desc': 'Relative Strength Index (0-100)'},
    {'name': 'atr_N', 'desc': 'Average True Range (points)'},
    {'name': 'hv_N', 'desc': 'Annualised historical volatility (0.15 = 15%)'},
    {'name': 'ret_N', 'desc': 'N-bar % return'},
    {'name': 'zscore_N', 'desc': 'Z-score of close vs N-bar mean'},
    {'name': 'bbw_N', 'desc': 'Bollinger band width (% of price)'},
    {'name': 'dow', 'desc': 'Day of week (0=Mon .. 4=Fri)'},
    {'name': 'model_<name>', 'desc': 'Out-of-sample probability from your trained AI model'},
]


def compute(df, name):
    """Return a Series for an indicator name, or raise KeyError."""
    if name in BASE_COLUMNS:
        return df[name]
    if name == 'dow':
        return pd.Series(df.index.dayofweek, index=df.index)
    m = NAME_RE.match(name)
    if not m:
        raise KeyError(name)
    fn, n = m.group(1), int(m.group(2))
    if n < 1 or n > 500:
        raise KeyError(name)
    if fn in SERIES_FUNCS:
        return SERIES_FUNCS[fn](df['close'], n)
    if fn in FRAME_FUNCS:
        return FRAME_FUNCS[fn](df, n)
    raise KeyError(name)


def enrich(df, names):
    """Add requested indicator columns to a copy of df."""
    out = df.copy()
    for name in names:
        if name not in out.columns and not name.startswith('model_'):
            out[name] = compute(df, name)
    return out
