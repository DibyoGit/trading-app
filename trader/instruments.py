"""Contract calendar, instrument keys and search.

Key format:  NIFTY:2026-10-20:24500:CE   (option)
             NIFTY:2026-10-27:FUT        (future)
Expiry rules (NSE, from Sept 2025): NIFTY weekly options every Tuesday plus
monthly on the last Tuesday; BANKNIFTY options and all index futures monthly on
the last Tuesday. When that day is an exchange holiday, expiry moves to the
previous trading day.
"""
import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from . import clock

UNDERLYINGS = {
    'NIFTY': {'name': 'NIFTY 50', 'step': 50, 'weekly': True},
    'BANKNIFTY': {'name': 'NIFTY BANK', 'step': 100, 'weekly': False},
}
EXPIRY_WEEKDAY = 1  # Tuesday
MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']


class InstrumentError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    underlying: str
    expiry: date
    kind: str            # CE / PE / FUT
    strike: float = None

    @property
    def key(self):
        if self.kind == 'FUT':
            return f'{self.underlying}:{self.expiry.isoformat()}:FUT'
        return f'{self.underlying}:{self.expiry.isoformat()}:{int(self.strike)}:{self.kind}'

    @property
    def is_option(self):
        return self.kind in ('CE', 'PE')

    @property
    def name(self):
        mon = MONTHS[self.expiry.month - 1]
        if self.kind == 'FUT':
            return f'{self.underlying} {mon} FUT'
        tag = f'{mon}' if self.expiry in monthly_expiries(self.underlying, 6, self.expiry) else \
            f'{self.expiry.day:02d} {mon}'
        return f'{self.underlying} {tag} {int(self.strike)} {self.kind}'

    def is_expired(self, at=None):
        return (at or clock.now()) >= clock.expiry_moment(self.expiry)


def _last_weekday(year, month, weekday):
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _monthly_for(year, month):
    return clock.previous_trading_day(_last_weekday(year, month, EXPIRY_WEEKDAY))


def is_monthly(d):
    return d == _monthly_for(d.year, d.month)


def _alive(d, at=None):
    return (at or clock.now()) < clock.expiry_moment(d)


def monthly_expiries(underlying, count=3, start=None):
    start = start or clock.today()
    out, y, m = [], start.year, start.month
    while len(out) < count:
        d = _monthly_for(y, m)
        if d >= start and (start != clock.today() or _alive(d)):
            out.append(d)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def option_expiries(underlying):
    underlying = underlying.upper()
    monthly = monthly_expiries(underlying, 3)
    if not UNDERLYINGS[underlying]['weekly']:
        return [{'date': d.isoformat(), 'monthly': True} for d in monthly]
    weekly, d = [], clock.today()
    d += timedelta(days=(EXPIRY_WEEKDAY - d.weekday()) % 7)
    while len(weekly) < 4:
        actual = clock.previous_trading_day(d)
        if actual >= clock.today() and _alive(actual):
            weekly.append(actual)
        d += timedelta(days=7)
    dates = sorted(set(weekly) | set(monthly))
    return [{'date': x.isoformat(), 'monthly': x in monthly} for x in dates]


def future_expiries(underlying):
    return monthly_expiries(underlying, 3)


def parse_key(key):
    parts = str(key).upper().split(':')
    try:
        und = parts[0]
        if und not in UNDERLYINGS:
            raise InstrumentError(f'unknown underlying {und}')
        exp = date.fromisoformat(parts[1])
        if len(parts) == 3 and parts[2] == 'FUT':
            return Instrument(und, exp, 'FUT')
        if len(parts) == 4 and parts[3] in ('CE', 'PE'):
            strike = float(parts[2])
            if strike <= 0 or strike % UNDERLYINGS[und]['step']:
                raise InstrumentError('strike not on the exchange strike grid')
            return Instrument(und, exp, parts[3], strike)
    except (IndexError, ValueError) as exc:
        if isinstance(exc, InstrumentError):
            raise
    raise InstrumentError(f'invalid instrument "{key}"')


def validate_tradable(inst):
    """Only listed contracts: real expiry dates, strikes within a sane band."""
    valid = {date.fromisoformat(e['date']) for e in option_expiries(inst.underlying)} if inst.is_option \
        else set(future_expiries(inst.underlying))
    if inst.expiry not in valid:
        raise InstrumentError('contract is not listed (check expiry date)')
    if inst.is_expired():
        raise InstrumentError('contract has expired')


def search(query, spot_of, limit=20):
    """Free-text search, e.g. 'nifty 24500 ce', 'bank fut', 'nifty oct pe'."""
    q = str(query or '').upper()
    tokens = re.findall(r'[A-Z]+|\d+', q)
    unds = [u for u in UNDERLYINGS if any(u.startswith(t) and len(t) >= 3 for t in tokens)] or \
        (['BANKNIFTY'] if any(t.startswith('BANK') for t in tokens) else [])
    if any(t.startswith('BANK') for t in tokens):
        unds = ['BANKNIFTY']
    unds = unds or list(UNDERLYINGS)
    kinds = [k for k in ('CE', 'PE', 'FUT') if k in tokens] or ['FUT', 'CE', 'PE']
    month = next((MONTHS.index(t[:3]) + 1 for t in tokens if t[:3] in MONTHS and len(t) >= 3), None)
    strikes = [int(t) for t in tokens if t.isdigit() and int(t) >= 1000]

    results = []
    for und in unds:
        step = UNDERLYINGS[und]['step']
        spot = spot_of(und)
        if 'FUT' in kinds and not strikes:
            for d in future_expiries(und):
                if month is None or d.month == month:
                    results.append(Instrument(und, d, 'FUT'))
        opt_kinds = [k for k in kinds if k != 'FUT']
        if not opt_kinds:
            continue
        if strikes:
            ks = [round(s / step) * step for s in strikes]
        else:
            atm = round(spot / step) * step
            ks = [atm + i * step for i in (0, -1, 1, -2, 2)]
        for e in option_expiries(und):
            d = date.fromisoformat(e['date'])
            if month is not None and d.month != month:
                continue
            for k in ks:
                for kind in opt_kinds:
                    results.append(Instrument(und, d, kind, float(k)))
    return results[:limit]
