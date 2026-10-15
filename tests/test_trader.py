import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import store  # noqa: E402
from trader import auth, broker, clock, instruments, market  # noqa: E402
from trader.instruments import Instrument  # noqa: E402

SPOT = {'NIFTY': 24000.0, 'BANKNIFTY': 52000.0}


def at(y, m, d, hh=11, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=clock.IST)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DB_PATH', str(tmp_path / 't.db'))
    with store.connect() as conn:
        conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, balance REAL)')
    store.init()
    broker.init()
    broker.CONFIG.update({'virtual_capital': 1000000.0, 'enforce_market_hours': False, 'short_margin_pct': 12.0})
    monkeypatch.setattr(market, '_daily', lambda und: (0.15, SPOT[und], SPOT[und], 'test'))
    monkeypatch.setattr(market, 'spot', lambda und: {
        'underlying': und, 'name': und, 'ltp': SPOT[und], 'prev_close': 24000.0 if und == 'NIFTY' else 52000.0,
        'change': 0, 'change_pct': 0, 'source': 'test', 'as_of': None, 'hv20': 0.15})
    monkeypatch.setattr(market, 'lot_size', lambda und: 65 if und == 'NIFTY' else 30)
    clock._now_override = at(2026, 10, 14)  # Wednesday
    SPOT.update({'NIFTY': 24000.0, 'BANKNIFTY': 52000.0})
    yield
    clock._now_override = None


def nifty_opt(kind='CE', strike=24000):
    exp = date.fromisoformat(instruments.option_expiries('NIFTY')[0]['date'])
    return Instrument('NIFTY', exp, kind, float(strike))


def cash(uid):
    with store.connect() as conn:
        return conn.execute('SELECT cash FROM tr_accounts WHERE user_id=?', (uid,)).fetchone()['cash']


def order(uid, inst, side, lots=1, **kw):
    return broker.place_order(uid, {'key': inst.key, 'side': side, 'lots': lots, 'product': kw.pop('product', 'NRML'),
                                    'order_type': kw.pop('order_type', 'MARKET'), **kw})


# ---- calendar ------------------------------------------------------------------
def test_expiry_calendar():
    nifty = instruments.option_expiries('NIFTY')
    dates = [date.fromisoformat(e['date']) for e in nifty]
    assert all(d.weekday() == 1 for d in dates)              # Tuesdays
    assert dates[0] == date(2026, 10, 20)                     # next Tuesday
    assert any(e['monthly'] and e['date'] == '2026-10-27' for e in nifty)  # last Tuesday of Oct
    bank = instruments.option_expiries('BANKNIFTY')
    assert all(e['monthly'] for e in bank) and len(bank) == 3


def test_expiry_day_rollover():
    clock._now_override = at(2026, 10, 20, 15, 31)            # Tuesday after close
    assert instruments.option_expiries('NIFTY')[0]['date'] == '2026-10-27'


def test_key_validation():
    with pytest.raises(instruments.InstrumentError):
        instruments.parse_key('NIFTY:2026-10-20:24010:CE')    # off strike grid
    with pytest.raises(instruments.InstrumentError):
        instruments.validate_tradable(instruments.parse_key('NIFTY:2026-10-21:24000:CE'))  # not an expiry


def test_search():
    names = [i.name for i in instruments.search('nifty 24500 ce', lambda u: SPOT[u])]
    assert names and all('24500 CE' in n and n.startswith('NIFTY') for n in names)
    assert all(i.kind == 'FUT' for i in instruments.search('bank fut', lambda u: SPOT[u]))


# ---- orders & accounting ----------------------------------------------------------
def test_buy_then_sell_option_accounting():
    broker.ensure_account(1)
    inst = nifty_opt()
    o = order(1, inst, 'BUY')
    assert o['status'] == 'COMPLETE'
    q = market.quote(inst)
    assert cash(1) == pytest.approx(1000000 - q['ask'] * 65 - 20)
    SPOT['NIFTY'] = 24200.0
    o2 = order(1, inst, 'SELL')
    pos = broker.positions(1)[0]
    assert pos['qty'] == 0
    assert pos['realized'] == pytest.approx((o2['avg_price'] - o['avg_price']) * 65, abs=0.01)
    stt = o2['avg_price'] * 65 * 0.001
    assert cash(1) == pytest.approx(1000000 + pos['realized'] - 40 - stt, abs=0.01)


def test_short_option_needs_margin_and_exit_always_allowed():
    broker.ensure_account(1)
    inst = nifty_opt('PE', 23500)
    o = order(1, inst, 'SELL', lots=20)
    assert o['status'] == 'REJECTED' and 'Insufficient funds' in o['reason']   # 20*65*24000*12% > 10L
    assert order(1, inst, 'SELL', lots=2)['status'] == 'COMPLETE'
    f = broker.funds(1)
    assert f['margin_used'] == pytest.approx(0.12 * 130 * 24000)
    with store.connect() as conn:  # drain cash: exits must still go through
        conn.execute('UPDATE tr_accounts SET cash = 0 WHERE user_id=1')
    broker.exit_position(1, inst.key, 'NRML')
    assert broker.positions(1)[0]['qty'] == 0


def test_future_pnl_realised_into_cash():
    broker.ensure_account(1)
    fut = Instrument('NIFTY', instruments.future_expiries('NIFTY')[0], 'FUT')
    o = order(1, fut, 'BUY')
    assert cash(1) == pytest.approx(1000000 - 20)              # no premium on futures
    SPOT['NIFTY'] = 24100.0
    o2 = order(1, fut, 'SELL')
    assert cash(1) == pytest.approx(1000000 - 40 + (o2['avg_price'] - o['avg_price']) * 65, abs=0.01)


def test_limit_order_rests_then_fills():
    broker.ensure_account(1)
    inst = nifty_opt()
    ltp = market.quote(inst)['ltp']
    o = order(1, inst, 'BUY', order_type='LIMIT', price=round(ltp * 0.7, 1))
    assert o['status'] == 'OPEN'
    broker.process(1)
    assert broker.orders(1)[0]['status'] == 'OPEN'
    SPOT['NIFTY'] = 23700.0                                    # call gets cheaper
    broker.process(1)
    assert broker.orders(1)[0]['status'] == 'COMPLETE'


def test_stop_order_triggers():
    broker.ensure_account(1)
    inst = nifty_opt('PE')
    ltp = market.quote(inst)['ltp']
    o = order(1, inst, 'BUY', order_type='SL-M', trigger_price=round(ltp + 20, 1))
    assert o['status'] == 'OPEN'
    SPOT['NIFTY'] = 23900.0                                    # put rises above trigger
    broker.process(1)
    assert broker.orders(1)[0]['status'] == 'COMPLETE'


def test_invalid_stop_rejected():
    broker.ensure_account(1)
    inst = nifty_opt()
    with pytest.raises(broker.OrderError):
        order(1, inst, 'BUY', order_type='SL-M', trigger_price=1)


def test_mis_auto_square_off():
    broker.ensure_account(1)
    inst = nifty_opt()
    order(1, inst, 'BUY', product='MIS')
    clock._now_override = at(2026, 10, 14, 15, 19)
    broker.process(1)
    assert broker.positions(1)[0]['qty'] == 65
    clock._now_override = at(2026, 10, 14, 15, 21)
    broker.process(1)
    assert broker.positions(1)[0]['qty'] == 0
    assert broker.orders(1)[0]['tag'] == 'AUTO SQ-OFF'


def test_expiry_settles_at_intrinsic():
    broker.ensure_account(1)
    inst = nifty_opt('CE', 24000)
    order(1, inst, 'BUY')
    c_before = cash(1)
    SPOT['NIFTY'] = 24300.0
    clock._now_override = at(2026, 10, 20, 15, 31)
    broker.process(1)
    assert broker.positions(1)[0]['qty'] == 0
    assert cash(1) == pytest.approx(c_before + 300 * 65)      # no charges on settlement


def test_market_hours_enforced_when_enabled():
    broker.ensure_account(1)
    broker.CONFIG['enforce_market_hours'] = True
    clock._now_override = at(2026, 10, 17)                     # Saturday
    with pytest.raises(broker.OrderError):
        order(1, Instrument('NIFTY', date(2026, 10, 20), 'CE', 24000.0), 'BUY')


def test_preview_does_not_place():
    broker.ensure_account(1)
    p = broker.preview(1, {'key': nifty_opt().key, 'side': 'BUY', 'lots': 1})
    assert p['ok'] and p['required'] > 0
    assert broker.orders(1) == [] and cash(1) == 1000000


# ---- auth ------------------------------------------------------------------------
def test_register_login_and_legacy_upgrade():
    uid, _ = auth.register('alice', 'longpassword', '1.1.1.1')
    assert auth.login('alice', 'longpassword', '1.1.1.1')[0] == uid
    with pytest.raises(auth.AuthError):
        auth.login('alice', 'wrong', '1.1.1.1')
    import hashlib
    with store.connect() as conn:
        conn.execute('INSERT INTO users (username, password) VALUES (?, ?)', ('old', hashlib.md5(b'pw').hexdigest()))
    auth.login('old', 'pw', '2.2.2.2')
    with store.connect() as conn:
        assert conn.execute("SELECT password FROM users WHERE username='old'").fetchone()[0].startswith(('scrypt', 'pbkdf2'))


def test_register_rules_and_throttle():
    with pytest.raises(auth.AuthError):
        auth.register('bob', 'short', '3.3.3.3')
    auth._failures.clear()
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.login('nobody', 'x', '9.9.9.9')
    with pytest.raises(auth.AuthError, match='Too many'):
        auth.login('nobody', 'x', '9.9.9.9')
