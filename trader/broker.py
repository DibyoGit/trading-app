"""Virtual broker: accounts, orders, fills, positions, margin, settlement.

Accounting model
  * cash: option premiums move cash at each fill (buy pays, sell receives);
    futures move cash only when P&L is realised; charges always reduce cash
  * margin: short options and futures block `short_margin_pct` of notional
  * available = cash + unrealised futures P&L - margin used
  * orders that only reduce a position are never blocked for funds
"""
import threading

from engine import store

from . import clock, market
from .instruments import InstrumentError, future_expiries, parse_key, validate_tradable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tr_accounts (
    user_id INTEGER PRIMARY KEY, cash REAL NOT NULL, opening REAL NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS tr_orders (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, key TEXT NOT NULL, side TEXT NOT NULL,
    qty INTEGER NOT NULL, product TEXT NOT NULL, order_type TEXT NOT NULL, price REAL, trigger_price REAL,
    status TEXT NOT NULL, avg_price REAL, reason TEXT, tag TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS tr_trades (
    id INTEGER PRIMARY KEY, order_id INTEGER, user_id INTEGER NOT NULL, key TEXT NOT NULL, side TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL NOT NULL, charges REAL NOT NULL, product TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS tr_positions (
    user_id INTEGER NOT NULL, key TEXT NOT NULL, product TEXT NOT NULL, qty INTEGER NOT NULL,
    avg_price REAL NOT NULL, realized REAL NOT NULL DEFAULT 0, opened_at TEXT, updated_at TEXT,
    PRIMARY KEY (user_id, key, product));
CREATE TABLE IF NOT EXISTS tr_watchlist (
    user_id INTEGER NOT NULL, key TEXT NOT NULL, pos INTEGER NOT NULL, PRIMARY KEY (user_id, key));
CREATE INDEX IF NOT EXISTS ix_tr_orders_open ON tr_orders(status);
"""

CONFIG = {
    'virtual_capital': 1000000.0, 'max_lots_per_order': 20, 'cost_per_order': 20.0,
    'sell_stt_pct': 0.1, 'short_margin_pct': 12.0, 'enforce_market_hours': False,
}
SIDES, PRODUCTS, TYPES = ('BUY', 'SELL'), ('MIS', 'NRML'), ('MARKET', 'LIMIT', 'SL-M')
WATCHLIST_MAX = 50

_lock = threading.RLock()


class OrderError(ValueError):
    pass


def configure(config):
    for k in ('virtual_capital', 'cost_per_order', 'sell_stt_pct', 'short_margin_pct'):
        if k in config:
            CONFIG[k] = float(config[k])
    if 'max_lots_per_order' in config:
        CONFIG['max_lots_per_order'] = int(config['max_lots_per_order'])
    if 'enforce_market_hours' in config:
        CONFIG['enforce_market_hours'] = str(config['enforce_market_hours']).lower() in ('1', 'true', 'yes')


def init():
    with store.connect() as conn:
        conn.executescript(SCHEMA)


def _sign(x):
    return (x > 0) - (x < 0)


# ---- accounts -------------------------------------------------------------------
def ensure_account(uid):
    with store.connect() as conn:
        row = conn.execute('SELECT * FROM tr_accounts WHERE user_id=?', (uid,)).fetchone()
        if row:
            return dict(row)
        cap = CONFIG['virtual_capital']
        conn.execute('INSERT INTO tr_accounts (user_id, cash, opening, created_at) VALUES (?, ?, ?, ?)',
                     (uid, cap, cap, clock.iso()))
        pos = 0
        for key in ('NIFTY:INDEX', 'BANKNIFTY:INDEX', *(f'{u}:{future_expiries(u)[0].isoformat()}:FUT'
                                                          for u in ('NIFTY', 'BANKNIFTY'))):
            conn.execute('INSERT OR IGNORE INTO tr_watchlist (user_id, key, pos) VALUES (?, ?, ?)', (uid, key, pos))
            pos += 1
    return {'user_id': uid, 'cash': cap, 'opening': cap}


def reset_account(uid):
    with _lock, store.connect() as conn:
        for t in ('tr_orders', 'tr_trades', 'tr_positions'):
            conn.execute(f'DELETE FROM {t} WHERE user_id=?', (uid,))
        conn.execute('UPDATE tr_accounts SET cash=opening WHERE user_id=?', (uid,))
    store.audit(uid, 'trader.reset')


# ---- margin ---------------------------------------------------------------------
def _exposure(inst, qty, q):
    """Margin blocked for a signed position."""
    if qty == 0:
        return 0.0
    pct = CONFIG['short_margin_pct'] / 100
    if inst.kind == 'FUT':
        return pct * abs(qty) * q['ltp']
    if qty < 0:
        return pct * abs(qty) * q['spot']
    return 0.0


def _book(conn, uid, override=None):
    """Margin used and unrealised futures P&L, optionally with one position replaced."""
    rows = {(r['key'], r['product']): (r['qty'], r['avg_price'])
            for r in conn.execute('SELECT key, product, qty, avg_price FROM tr_positions WHERE user_id=?', (uid,))}
    if override:
        rows[override[0]] = override[1]
    margin, fut_unreal = 0.0, 0.0
    for (key, _), (qty, avg) in rows.items():
        if qty == 0:
            continue
        inst = parse_key(key)
        q = market.quote(inst)
        margin += _exposure(inst, qty, q)
        if inst.kind == 'FUT':
            fut_unreal += (q['ltp'] - avg) * qty
    return margin, fut_unreal


# ---- execution ------------------------------------------------------------------
def _charges(inst, side, qty, price):
    c = CONFIG['cost_per_order']
    if side == 'SELL' and inst.is_option:
        c += price * qty * CONFIG['sell_stt_pct'] / 100
    return round(c, 2)


def _execute(conn, order, price, charges=True):
    """Fill `order` completely at `price`. Raises OrderError if funds are short."""
    uid, inst, side, qty = order['user_id'], parse_key(order['key']), order['side'], order['qty']
    signed = qty if side == 'BUY' else -qty
    pk = (order['key'], order['product'])
    row = conn.execute('SELECT * FROM tr_positions WHERE user_id=? AND key=? AND product=?', (uid, *pk)).fetchone()
    cur, avg, realized, opened = (row['qty'], row['avg_price'], row['realized'], row['opened_at']) if row \
        else (0, 0.0, 0.0, None)

    new = cur + signed
    realized_delta = 0.0
    if cur == 0 or _sign(cur) == _sign(signed):
        new_avg = (abs(cur) * avg + qty * price) / abs(new)
        opened = opened if cur else clock.iso()
    else:
        closed = min(abs(cur), qty)
        realized_delta = closed * (price - avg) * _sign(cur)
        if new == 0:
            new_avg = 0.0
        elif _sign(new) == _sign(cur):
            new_avg = avg
        else:
            new_avg, opened = price, clock.iso()

    fee = _charges(inst, side, qty, price) if charges else 0.0
    cash_delta = (-signed * price if inst.is_option else realized_delta) - fee
    acct = conn.execute('SELECT cash FROM tr_accounts WHERE user_id=?', (uid,)).fetchone()
    reduce_only = cur != 0 and abs(new) < abs(cur) and (new == 0 or _sign(new) == _sign(cur))
    if not reduce_only:
        margin_after, fut_unreal = _book(conn, uid, override=(pk, (new, new_avg)))
        free_after = acct['cash'] + cash_delta + fut_unreal - margin_after
        if free_after < 0:
            raise OrderError(f'Insufficient funds: short by ₹{-free_after:,.2f}')

    now = clock.iso()
    conn.execute('UPDATE tr_accounts SET cash = cash + ? WHERE user_id=?', (cash_delta, uid))
    conn.execute('INSERT INTO tr_positions (user_id, key, product, qty, avg_price, realized, opened_at, updated_at) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, key, product) DO UPDATE SET qty=excluded.qty, '
                 'avg_price=excluded.avg_price, realized=excluded.realized, opened_at=excluded.opened_at, '
                 'updated_at=excluded.updated_at',
                 (uid, *pk, new, round(new_avg, 4), round(realized + realized_delta, 2), opened, now))
    conn.execute('INSERT INTO tr_trades (order_id, user_id, key, side, qty, price, charges, product, ts) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (order['id'], uid, order['key'], side, qty, price, fee,
                                                        order['product'], now))
    conn.execute("UPDATE tr_orders SET status='COMPLETE', avg_price=?, updated_at=? WHERE id=?", (price, now, order['id']))


def _insert_order(conn, uid, key, side, qty, product, otype, price=None, trigger=None, tag=None):
    now = clock.iso()
    cur = conn.execute('INSERT INTO tr_orders (user_id, key, side, qty, product, order_type, price, trigger_price, '
                       "status, tag, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)",
                       (uid, key, side, qty, product, otype, price, trigger, tag, now, now))
    return dict(conn.execute('SELECT * FROM tr_orders WHERE id=?', (cur.lastrowid,)).fetchone())


def _reject(conn, order_id, reason):
    conn.execute("UPDATE tr_orders SET status='REJECTED', reason=?, updated_at=? WHERE id=?",
                 (reason, clock.iso(), order_id))


def _validate(uid, p):
    try:
        inst = parse_key(p.get('key', ''))
        validate_tradable(inst)
    except InstrumentError as exc:
        raise OrderError(str(exc)) from None
    side, product, otype = str(p.get('side', '')).upper(), str(p.get('product', 'NRML')).upper(), \
        str(p.get('order_type', 'MARKET')).upper()
    if side not in SIDES:
        raise OrderError('side must be BUY or SELL')
    if product not in PRODUCTS:
        raise OrderError('product must be MIS or NRML')
    if otype not in TYPES:
        raise OrderError('order type must be MARKET, LIMIT or SL-M')
    try:
        lots = int(p.get('lots', 0))
    except (TypeError, ValueError):
        raise OrderError('lots must be a whole number') from None
    if not 1 <= lots <= CONFIG['max_lots_per_order']:
        raise OrderError(f"lots must be 1-{CONFIG['max_lots_per_order']} per order")
    if CONFIG['enforce_market_hours'] and not clock.is_market_open():
        raise OrderError('Market is closed (09:15-15:30 IST, Mon-Fri)')
    q = market.quote(inst)
    price = trigger = None
    if otype == 'LIMIT':
        price = market.round_tick(float(p.get('price') or 0))
        if not p.get('price') or float(p['price']) <= 0:
            raise OrderError('enter a limit price')
    if otype == 'SL-M':
        trigger = market.round_tick(float(p.get('trigger_price') or 0))
        if side == 'BUY' and trigger <= q['ltp']:
            raise OrderError('for a BUY stop, trigger must be above LTP')
        if side == 'SELL' and trigger >= q['ltp']:
            raise OrderError('for a SELL stop, trigger must be below LTP')
    return inst, side, product, otype, lots * q['lot_size'], price, trigger, q


def place_order(uid, payload):
    inst, side, product, otype, qty, price, trigger, q = _validate(uid, payload)
    ensure_account(uid)
    with _lock, store.connect() as conn:
        order = _insert_order(conn, uid, inst.key, side, qty, product, otype, price, trigger,
                              str(payload.get('tag') or '')[:40] or None)
        fill = None
        if otype == 'MARKET':
            fill = q['ask'] if side == 'BUY' else q['bid']
        elif otype == 'LIMIT' and ((side == 'BUY' and q['ask'] <= price) or (side == 'SELL' and q['bid'] >= price)):
            fill = q['ask'] if side == 'BUY' else q['bid']
        try:
            if fill is not None:
                _execute(conn, order, fill)
            else:
                _precheck(conn, order, price or q['ltp'])
        except OrderError as exc:
            _reject(conn, order['id'], str(exc))
        result = dict(conn.execute('SELECT * FROM tr_orders WHERE id=?', (order['id'],)).fetchone())
    store.audit(uid, 'trader.order', {'order': result})
    return _order_view(result)


def _precheck(conn, order, price):
    """Reject a resting order now if it could not be funded at its own price."""
    conn.execute('SAVEPOINT pre')
    try:
        _execute(conn, order, price)
    finally:
        conn.execute('ROLLBACK TO pre')
        conn.execute('RELEASE pre')


def cancel_order(uid, oid):
    with _lock, store.connect() as conn:
        cur = conn.execute("UPDATE tr_orders SET status='CANCELLED', reason='cancelled by you', updated_at=? "
                           "WHERE id=? AND user_id=? AND status='OPEN'", (clock.iso(), oid, uid))
    if cur.rowcount == 0:
        raise OrderError('order is not open')


def exit_position(uid, key, product):
    with _lock, store.connect() as conn:
        row = conn.execute('SELECT qty FROM tr_positions WHERE user_id=? AND key=? AND product=?',
                           (uid, key, product)).fetchone()
        if not row or row['qty'] == 0:
            raise OrderError('no open position')
        q = market.quote(key)
        side = 'SELL' if row['qty'] > 0 else 'BUY'
        order = _insert_order(conn, uid, key, side, abs(row['qty']), product, 'MARKET', tag='EXIT')
        _execute(conn, order, q['bid'] if side == 'SELL' else q['ask'])
    store.audit(uid, 'trader.exit', {'key': key, 'product': product})


# ---- background processing ------------------------------------------------------
def process(uid=None):
    """Match resting orders, settle expiries, auto square-off MIS. Idempotent."""
    events = []
    with _lock, store.connect() as conn:
        where, args = ("AND user_id=?", (uid,)) if uid else ('', ())
        for o in conn.execute(f"SELECT * FROM tr_orders WHERE status='OPEN' {where}", args).fetchall():
            o = dict(o)
            inst = parse_key(o['key'])
            if inst.is_expired():
                conn.execute("UPDATE tr_orders SET status='CANCELLED', reason='contract expired', updated_at=? "
                             "WHERE id=?", (clock.iso(), o['id']))
                continue
            q = market.quote(inst)
            fill = None
            if o['order_type'] == 'LIMIT':
                if o['side'] == 'BUY' and q['ask'] <= o['price']:
                    fill = q['ask']
                elif o['side'] == 'SELL' and q['bid'] >= o['price']:
                    fill = q['bid']
            elif o['order_type'] == 'SL-M':
                if o['side'] == 'BUY' and q['ltp'] >= o['trigger_price']:
                    fill = q['ask']
                elif o['side'] == 'SELL' and q['ltp'] <= o['trigger_price']:
                    fill = q['bid']
            if fill is None:
                continue
            try:
                _execute(conn, o, fill)
                events.append(('filled', o['id']))
            except OrderError as exc:
                _reject(conn, o['id'], str(exc))

        now = clock.now()
        for p in conn.execute(f"SELECT * FROM tr_positions WHERE qty != 0 {where}", args).fetchall():
            inst = parse_key(p['key'])
            side = 'SELL' if p['qty'] > 0 else 'BUY'
            if inst.is_expired(now):
                q = market.quote(inst)  # expired quote = settlement value
                order = _insert_order(conn, p['user_id'], p['key'], side, abs(p['qty']), p['product'], 'MARKET',
                                      tag='EXPIRY')
                _execute(conn, order, q['ltp'], charges=False)
                events.append(('settled', p['key']))
            elif p['product'] == 'MIS' and p['opened_at'] and now >= clock.next_square_off(clock.parse(p['opened_at'])):
                q = market.quote(inst)
                order = _insert_order(conn, p['user_id'], p['key'], side, abs(p['qty']), p['product'], 'MARKET',
                                      tag='AUTO SQ-OFF')
                _execute(conn, order, q['bid'] if side == 'SELL' else q['ask'])
                events.append(('squared_off', p['key']))
    return events


# ---- views ----------------------------------------------------------------------
def _order_view(o):
    inst = parse_key(o['key'])
    lot = market.lot_size(inst.underlying)
    return {**o, 'name': inst.name, 'lots': o['qty'] // lot if lot else None}


def orders(uid, limit=200):
    with store.connect() as conn:
        rows = conn.execute('SELECT * FROM tr_orders WHERE user_id=? ORDER BY id DESC LIMIT ?', (uid, limit)).fetchall()
    return [_order_view(dict(r)) for r in rows]


def trades(uid, limit=200):
    with store.connect() as conn:
        rows = conn.execute('SELECT * FROM tr_trades WHERE user_id=? ORDER BY id DESC LIMIT ?', (uid, limit)).fetchall()
    return [{**dict(r), 'name': parse_key(r['key']).name} for r in rows]


def positions(uid):
    with store.connect() as conn:
        rows = conn.execute('SELECT * FROM tr_positions WHERE user_id=? ORDER BY updated_at DESC', (uid,)).fetchall()
    out = []
    for r in rows:
        q = market.quote(r['key'])
        unreal = (q['ltp'] - r['avg_price']) * r['qty']
        out.append({'key': r['key'], 'name': q['name'], 'product': r['product'], 'qty': r['qty'],
                    'lots': r['qty'] // q['lot_size'], 'avg_price': round(r['avg_price'], 2), 'ltp': q['ltp'],
                    'unrealized': round(unreal, 2), 'realized': r['realized'], 'pnl': round(unreal + r['realized'], 2),
                    'change_pct': q['change_pct'], 'opened_at': r['opened_at']})
    return out


def funds(uid):
    acct = ensure_account(uid)
    with store.connect() as conn:
        acct = dict(conn.execute('SELECT * FROM tr_accounts WHERE user_id=?', (uid,)).fetchone())
        margin, fut_unreal = _book(conn, uid)
        charges = conn.execute('SELECT COALESCE(SUM(charges), 0) AS c FROM tr_trades WHERE user_id=?',
                               (uid,)).fetchone()['c']
        opt_value = 0.0
        for r in conn.execute('SELECT key, qty FROM tr_positions WHERE user_id=? AND qty != 0', (uid,)).fetchall():
            inst = parse_key(r['key'])
            if inst.is_option:
                opt_value += market.quote(inst)['ltp'] * r['qty']
    value = acct['cash'] + opt_value + fut_unreal
    return {'opening': acct['opening'], 'cash': round(acct['cash'], 2), 'margin_used': round(margin, 2),
            'available': round(acct['cash'] + fut_unreal - margin, 2), 'option_value': round(opt_value, 2),
            'futures_mtm': round(fut_unreal, 2), 'account_value': round(value, 2),
            'total_pnl': round(value - acct['opening'], 2), 'charges': round(charges, 2)}


def preview(uid, payload):
    """Funds needed for an order, without placing it."""
    inst, side, product, otype, qty, price, trigger, q = _validate(uid, payload)
    ensure_account(uid)
    px = price or (q['ask'] if side == 'BUY' else q['bid'])
    with _lock, store.connect() as conn:
        before = funds(uid)['available']
        conn.execute('SAVEPOINT pv')
        try:
            order = _insert_order(conn, uid, inst.key, side, qty, product, otype, price, trigger)
            ok, reason = True, None
            try:
                _execute(conn, order, px)
            except OrderError as exc:
                ok, reason = False, str(exc)
            after = None
            if ok:
                margin, fut_unreal = _book(conn, uid)
                cash = conn.execute('SELECT cash FROM tr_accounts WHERE user_id=?', (uid,)).fetchone()['cash']
                after = cash + fut_unreal - margin
        finally:
            conn.execute('ROLLBACK TO pv')
            conn.execute('RELEASE pv')
    return {'qty': qty, 'price': px, 'required': round(before - after, 2) if ok else None,
            'available': before, 'ok': ok, 'reason': reason, 'charges': _charges(inst, side, qty, px)}


# ---- watchlist ------------------------------------------------------------------
def watchlist(uid):
    ensure_account(uid)
    with store.connect() as conn:
        keys = [r['key'] for r in conn.execute('SELECT key FROM tr_watchlist WHERE user_id=? ORDER BY pos', (uid,))]
    out = []
    for k in keys:
        try:
            out.append(market.quote_any(k))
        except (InstrumentError, ValueError):
            continue
    return out


def watch_add(uid, key):
    if not key.upper().endswith(':INDEX'):
        try:
            inst = parse_key(key)
            validate_tradable(inst)
            key = inst.key
        except InstrumentError as exc:
            raise OrderError(str(exc)) from None
    with store.connect() as conn:
        n = conn.execute('SELECT COUNT(*) AS n, COALESCE(MAX(pos), -1) AS m FROM tr_watchlist WHERE user_id=?',
                         (uid,)).fetchone()
        if n['n'] >= WATCHLIST_MAX:
            raise OrderError(f'watchlist is full ({WATCHLIST_MAX})')
        conn.execute('INSERT OR IGNORE INTO tr_watchlist (user_id, key, pos) VALUES (?, ?, ?)', (uid, key, n['m'] + 1))


def watch_remove(uid, key):
    with store.connect() as conn:
        conn.execute('DELETE FROM tr_watchlist WHERE user_id=? AND key=?', (uid, key))


def active_users():
    with store.connect() as conn:
        rows = conn.execute("SELECT DISTINCT user_id FROM tr_orders WHERE status='OPEN' UNION "
                            "SELECT DISTINCT user_id FROM tr_positions WHERE qty != 0").fetchall()
    return [r['user_id'] for r in rows]
