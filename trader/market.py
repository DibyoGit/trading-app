"""Quotes for the terminal.

* Index level: Yahoo Finance 1-minute bars (delayed), cached briefly; falls
  back to the last daily close when the feed is unreachable.
* Options: Black-Scholes on the index with a volatility smile built from
  20-day historical volatility. Futures: cost-of-carry fair value.
* Bid/ask: a synthetic spread around the model price, rounded to the 0.05 tick.

Every quote carries a `source` so the UI can say exactly what it shows.
"""
import math
import threading
import time
from datetime import timedelta

from engine import data as eng_data
from engine.indicators import hv
from engine.pricing import bs_greeks, bs_price

from . import clock
from .instruments import UNDERLYINGS, Instrument, parse_key

TICK = 0.05
SPOT_TTL = 15          # seconds between index refreshes
FAIL_BACKOFF = 60      # seconds to wait after a failed fetch
VOL_TTL = 3600

_lock = threading.Lock()
_spot_cache = {}       # und -> (fetched_at, payload)
_fail_until = {}
_vol_cache = {}        # und -> (fetched_at, hv20, prev_close, last_close)
_candle_cache = {}

CONFIG = {'risk_free_rate': 0.065, 'iv_multiplier': 1.10}


def configure(config):
    for k in ('risk_free_rate', 'iv_multiplier'):
        if k in config:
            CONFIG[k] = float(config[k])
    CONFIG['lot'] = {u: eng_data.underlying_meta(u, config)['lot_size'] for u in UNDERLYINGS}


def lot_size(und):
    return CONFIG.get('lot', {}).get(und) or eng_data.underlying_meta(und)['lot_size']


def round_tick(x):
    return max(TICK, round(round(x / TICK) * TICK, 2))


def _daily(und):
    """hv20, previous close and last close from cached daily bars."""
    hit = _vol_cache.get(und)
    if hit and time.time() - hit[0] < VOL_TTL:
        return hit[1:]
    df, source = eng_data.load_bars(und, '1y')
    vol = float(hv(df['close'], 20).iloc[-1])
    if not math.isfinite(vol) or vol <= 0:
        vol = 0.14
    last_date = df.index[-1].date()
    closes = df['close']
    # if today's daily bar already exists, "previous close" is the bar before it
    prev = float(closes.iloc[-2]) if last_date >= clock.today() and len(closes) > 1 else float(closes.iloc[-1])
    _vol_cache[und] = (time.time(), vol, prev, float(closes.iloc[-1]), source)
    return vol, prev, float(closes.iloc[-1]), source


def _fetch_intraday(und):
    import yfinance as yf
    hist = yf.Ticker(eng_data.UNDERLYINGS[und]['ticker']).history(period='1d', interval='1m')
    if hist is None or hist.empty:
        return None
    return float(hist['Close'].iloc[-1]), hist.index[-1].to_pydatetime()


def spot(und):
    und = und.upper()
    with _lock:
        hit = _spot_cache.get(und)
        if hit and time.time() - hit[0] < SPOT_TTL:
            return hit[1]
        vol, prev, last_close, dsource = _daily(und)
        ltp, source, as_of = None, None, None
        if time.time() >= _fail_until.get(und, 0):
            try:
                got = _fetch_intraday(und)
                if got:
                    ltp, as_of = got
                    source = 'Yahoo (delayed)'
            except Exception as exc:
                print(f'[market] intraday fetch failed for {und}: {exc}')
            if ltp is None:
                _fail_until[und] = time.time() + FAIL_BACKOFF
        if ltp is None:
            ltp, source = last_close, f'last close ({dsource})'
        payload = {
            'underlying': und, 'name': UNDERLYINGS[und]['name'], 'ltp': round(ltp, 2),
            'prev_close': round(prev, 2), 'change': round(ltp - prev, 2),
            'change_pct': round((ltp / prev - 1) * 100, 2) if prev else 0.0,
            'source': source, 'as_of': as_of.isoformat() if as_of else None, 'hv20': round(vol, 4),
        }
        _spot_cache[und] = (time.time(), payload)
        return payload


def _years_to(expiry, at=None):
    secs = (clock.expiry_moment(expiry) - (at or clock.now())).total_seconds()
    return max(secs, 60) / (365 * 24 * 3600)


def implied_vol(und, strike, s):
    """Smile: OTM puts richer than OTM calls, wings richer than ATM."""
    base = _daily(und)[0] * CONFIG['iv_multiplier']
    m = math.log(strike / s)
    iv = base * (1 + 4 * m * m) * (1 - 0.8 * m)
    return min(1.5, max(0.06, iv))


def model_price(inst, s, at=None):
    r = CONFIG['risk_free_rate']
    t = _years_to(inst.expiry, at)
    if inst.kind == 'FUT':
        return s * math.exp(r * t), None, None
    iv = implied_vol(inst.underlying, inst.strike, s)
    return bs_price(s, inst.strike, t, r, iv, inst.kind), iv, t


def spread(price, is_option=True):
    """Synthetic bid/ask width: ~0.4% for options, ~1 point for index futures."""
    return max(TICK, round_tick(price * (0.004 if is_option else 0.00004)))


def quote(inst):
    if isinstance(inst, str):
        inst = parse_key(inst)
    sp = spot(inst.underlying)
    s = sp['ltp']
    if inst.is_expired():
        ltp = max(0.0, s - inst.strike) if inst.kind == 'CE' else max(0.0, inst.strike - s) if inst.kind == 'PE' else s
        return {'key': inst.key, 'name': inst.name, 'ltp': round(ltp, 2), 'bid': round(ltp, 2), 'ask': round(ltp, 2),
                'change': 0.0, 'change_pct': 0.0, 'expired': True, 'lot_size': lot_size(inst.underlying),
                'source': 'settlement'}
    price, iv, t = model_price(inst, s)
    prev_price, _, _ = model_price(inst, sp['prev_close'], clock.now() - timedelta(days=1))
    ltp = round_tick(price)
    half = spread(ltp, inst.is_option) / 2
    q = {
        'key': inst.key, 'name': inst.name, 'underlying': inst.underlying, 'kind': inst.kind,
        'expiry': inst.expiry.isoformat(), 'strike': inst.strike, 'ltp': ltp,
        'bid': round_tick(ltp - half), 'ask': round_tick(ltp + half),
        'change': round(ltp - prev_price, 2), 'change_pct': round((ltp / prev_price - 1) * 100, 2) if prev_price > 0.05 else 0.0,
        'lot_size': lot_size(inst.underlying), 'spot': s, 'expired': False,
        'source': 'model' if inst.is_option else 'model (cost of carry)',
    }
    if inst.is_option:
        g = bs_greeks(s, inst.strike, t, CONFIG['risk_free_rate'], iv, inst.kind)
        q.update({'iv': round(iv * 100, 2), 'delta': round(g['delta'], 3), 'theta': round(g['theta'], 2)})
    return q


def index_quote_as_instrument(und):
    sp = spot(und)
    return {'key': f'{und}:INDEX', 'name': sp['name'], 'ltp': sp['ltp'], 'change': sp['change'],
            'change_pct': sp['change_pct'], 'index': True, 'source': sp['source']}


def quote_any(key):
    if key.upper().endswith(':INDEX'):
        und = key.split(':')[0].upper()
        if und not in UNDERLYINGS:
            raise ValueError('unknown index')
        return index_quote_as_instrument(und)
    return quote(key)


def chain(und, expiry_date, width=12):
    sp = spot(und)
    step = UNDERLYINGS[und]['step']
    atm = round(sp['ltp'] / step) * step
    rows = []
    for i in range(-width, width + 1):
        k = float(atm + i * step)
        ce = quote(Instrument(und, expiry_date, 'CE', k))
        pe = quote(Instrument(und, expiry_date, 'PE', k))
        rows.append({'strike': k, 'atm': i == 0, 'ce': ce, 'pe': pe})
    return {'underlying': und, 'expiry': expiry_date.isoformat(), 'spot': sp, 'atm': atm, 'rows': rows,
            'lot_size': lot_size(und)}


def candles(und, interval='5m'):
    period = {'1m': '1d', '5m': '5d', '15m': '1mo', '1d': '1y'}.get(interval)
    if period is None:
        raise ValueError('interval must be 1m, 5m, 15m or 1d')
    ck = (und, interval)
    hit = _candle_cache.get(ck)
    ttl = 60 if interval != '1d' else 3600
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    out = {'underlying': und, 'interval': interval, 'candles': [], 'source': None}
    try:
        if interval == '1d':
            df, src = eng_data.load_bars(und, '1y')
            df = df.rename(columns=str.capitalize)
        else:
            if time.time() < _fail_until.get(und, 0):
                raise RuntimeError('feed backing off')
            import yfinance as yf
            df = yf.Ticker(eng_data.UNDERLYINGS[und]['ticker']).history(period=period, interval=interval)
            src = 'Yahoo (delayed)'
        for ts, row in df.iterrows():
            stamp = int(ts.timestamp()) + (19800 if interval != '1d' else 0)  # chart axis shows IST
            out['candles'].append({'time': stamp, 'open': round(float(row['Open']), 2), 'high': round(float(row['High']), 2),
                                   'low': round(float(row['Low']), 2), 'close': round(float(row['Close']), 2)})
        out['source'] = src
    except Exception as exc:
        print(f'[market] candles failed for {und} {interval}: {exc}')
        out['source'] = 'unavailable'
    _candle_cache[ck] = (time.time(), out)
    return out
