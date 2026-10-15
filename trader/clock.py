"""Exchange clock in IST (India has no daylight saving, a fixed offset is exact)."""
from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), 'IST')
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
MIS_SQUARE_OFF = time(15, 20)

# Tests patch this to freeze time.
_now_override = None


def now():
    return _now_override or datetime.now(IST)


def today():
    return now().date()


def is_trading_day(d):
    return d.weekday() < 5  # exchange holidays not modelled in v1


def is_market_open(at=None):
    at = at or now()
    return is_trading_day(at.date()) and MARKET_OPEN <= at.time() < MARKET_CLOSE


def market_status():
    at = now()
    if is_market_open(at):
        return 'open'
    if is_trading_day(at.date()) and at.time() < MARKET_OPEN:
        return 'pre-open'
    return 'closed'


def expiry_moment(d):
    """Contracts expire at the close of their expiry day."""
    return datetime.combine(d, MARKET_CLOSE, IST)


def next_square_off(after):
    """First weekday 15:20 IST strictly after `after` (MIS auto square-off)."""
    d = after.date()
    candidate = datetime.combine(d, MIS_SQUARE_OFF, IST)
    while candidate <= after or not is_trading_day(candidate.date()):
        d += timedelta(days=1)
        candidate = datetime.combine(d, MIS_SQUARE_OFF, IST)
    return candidate


def iso(dt=None):
    return (dt or now()).isoformat(timespec='seconds')


def parse(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)
