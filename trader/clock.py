"""Exchange clock in IST (India has no daylight saving, a fixed offset is exact)."""
from datetime import date, datetime, time, timedelta, timezone

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


# NSE equity-derivatives trading holidays (weekdays only). Override with
# `nse_holidays` in config.properties when NSE publishes a new calendar.
HOLIDAYS = {date.fromisoformat(d) for d in (
    '2026-01-15', '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31', '2026-04-03', '2026-04-14',
    '2026-05-01', '2026-05-28', '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20', '2026-11-10',
    '2026-11-24', '2026-12-25',
)}
SESSION_MINUTES = 375          # 09:15-15:30
YEAR_MINUTES = 252 * SESSION_MINUTES


def configure(config):
    raw = config.get('nse_holidays')
    if raw:
        HOLIDAYS.clear()
        HOLIDAYS.update(date.fromisoformat(x.strip()) for x in raw.split(',') if x.strip())


def is_trading_day(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def previous_trading_day(d):
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_minutes_between(start, end):
    """Exchange-session minutes in [start, end). Options decay over trading time."""
    if end <= start:
        return 0.0
    total, d = 0.0, start.date()
    while d <= end.date():
        if is_trading_day(d):
            open_ = datetime.combine(d, MARKET_OPEN, IST)
            close = datetime.combine(d, MARKET_CLOSE, IST)
            lo, hi = max(open_, start), min(close, end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60
        d += timedelta(days=1)
    return total


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
