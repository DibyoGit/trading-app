"""Black-Scholes pricing and greeks for European index options (NIFTY style)."""
import math

SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _d1_d2(spot, strike, t, r, sigma):
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return d1, d1 - sigma * math.sqrt(t)


def bs_price(spot, strike, t, r, sigma, opt_type):
    """Option premium. t in years. opt_type 'CE' or 'PE'."""
    if t <= 0 or sigma <= 0:
        intrinsic = spot - strike if opt_type == 'CE' else strike - spot
        return max(0.0, intrinsic)
    d1, d2 = _d1_d2(spot, strike, t, r, sigma)
    if opt_type == 'CE':
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_greeks(spot, strike, t, r, sigma, opt_type):
    """Delta, gamma, theta (per day), vega (per 1 vol point)."""
    if t <= 0 or sigma <= 0:
        itm = (spot > strike) if opt_type == 'CE' else (spot < strike)
        delta = (1.0 if itm else 0.0) * (1 if opt_type == 'CE' else -1)
        return {'delta': delta, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}
    d1, d2 = _d1_d2(spot, strike, t, r, sigma)
    pdf = _norm_pdf(d1)
    gamma = pdf / (spot * sigma * math.sqrt(t))
    vega = spot * pdf * math.sqrt(t) / 100
    if opt_type == 'CE':
        delta = _norm_cdf(d1)
        theta = (-spot * pdf * sigma / (2 * math.sqrt(t)) - r * strike * math.exp(-r * t) * _norm_cdf(d2)) / 365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-spot * pdf * sigma / (2 * math.sqrt(t)) + r * strike * math.exp(-r * t) * _norm_cdf(-d2)) / 365
    return {'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega}


def implied_vol(price, spot, strike, t, r, opt_type, lo=0.01, hi=3.0, tol=1e-4):
    """Bisection IV solver. Returns None if price outside model bounds."""
    if t <= 0 or price <= 0:
        return None
    if not (bs_price(spot, strike, t, r, lo, opt_type) <= price <= bs_price(spot, strike, t, r, hi, opt_type)):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t, r, mid, opt_type) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


def resolve_strike(spec, spot, step):
    """Resolve 'ATM', 'ATM+2', 'ATM-1' (strike steps) or a number to a strike."""
    atm = round(spot / step) * step
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec).strip().upper().replace(' ', '')
    if s == 'ATM':
        return float(atm)
    if s.startswith('ATM'):
        return float(atm + int(s[3:]) * step)
    return float(s)
