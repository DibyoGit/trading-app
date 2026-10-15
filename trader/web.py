"""Flask blueprint: auth endpoints and the /api/t trading API."""
import threading
import time
from datetime import date
from functools import wraps

from flask import Blueprint, jsonify, redirect, request, session

from engine import store

from . import auth, broker, clock, instruments, market
from .instruments import InstrumentError

bp = Blueprint('trader', __name__)

USERS_SCHEMA = '''CREATE TABLE IF NOT EXISTS users
    (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, balance REAL DEFAULT 100000)'''


def init_app(app, config):
    market.configure(config)
    broker.configure(config)
    with store.connect() as conn:
        conn.execute(USERS_SCHEMA)
    broker.init()
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
    app.config.setdefault('SESSION_COOKIE_HTTPONLY', True)
    app.register_blueprint(bp)


def _uid():
    return session.get('user_id')


def _err(msg, code=400):
    return jsonify({'success': False, 'error': str(msg)}), code


def _body():
    return request.get_json(silent=True) or {}


def _ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[-1].strip()  # last hop is added by our proxy, not the client


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _uid():
            return _err('Not logged in', 401)
        return fn(*a, **kw)
    return wrapper


def _und(name):
    und = str(name or 'NIFTY').upper()
    if und not in instruments.UNDERLYINGS:
        raise InstrumentError('unknown underlying')
    return und


# ---- auth -----------------------------------------------------------------------
def _start_session(uid, username):
    session.clear()
    session['user_id'], session['username'] = uid, username
    broker.ensure_account(uid)


@bp.route('/auth/login', methods=['POST'])
def do_login():
    b = _body()
    try:
        _start_session(*auth.login(str(b.get('username', '')).strip(), str(b.get('password', '')), _ip()))
    except auth.AuthError as exc:
        return _err(exc, 401)
    return jsonify({'success': True})


@bp.route('/auth/register', methods=['POST'])
def do_register():
    b = _body()
    try:
        _start_session(*auth.register(str(b.get('username', '')).strip(), str(b.get('password', '')), _ip()))
    except auth.AuthError as exc:
        return _err(exc)
    return jsonify({'success': True})


@bp.route('/auth/logout')
def do_logout():
    session.clear()
    return redirect('/')


# ---- market data ----------------------------------------------------------------
@bp.route('/api/t/status')
@login_required
def status():
    return jsonify({'market': clock.market_status(), 'time': clock.iso(), 'username': session.get('username'),
                    'enforce_hours': broker.CONFIG['enforce_market_hours'],
                    'lots': {u: market.lot_size(u) for u in instruments.UNDERLYINGS}})


@bp.route('/api/t/indices')
@login_required
def indices():
    return jsonify([market.spot(u) for u in instruments.UNDERLYINGS])


@bp.route('/api/t/quotes')
@login_required
def quotes():
    out = {}
    for key in [k for k in request.args.get('keys', '').split(',') if k][:60]:
        try:
            out[key] = market.quote_any(key)
        except (InstrumentError, ValueError):
            continue
    return jsonify(out)


@bp.route('/api/t/expiries')
@login_required
def expiries():
    try:
        und = _und(request.args.get('underlying'))
    except InstrumentError as exc:
        return _err(exc)
    return jsonify({'options': instruments.option_expiries(und),
                    'futures': [d.isoformat() for d in instruments.future_expiries(und)]})


@bp.route('/api/t/chain')
@login_required
def chain():
    try:
        und = _und(request.args.get('underlying'))
        exp = date.fromisoformat(request.args.get('expiry', ''))
        if exp.isoformat() not in {e['date'] for e in instruments.option_expiries(und)}:
            raise InstrumentError('expiry not listed')
        width = min(25, max(5, int(request.args.get('width', 12))))
    except (InstrumentError, ValueError) as exc:
        return _err(exc)
    return jsonify(market.chain(und, exp, width))


@bp.route('/api/t/candles')
@login_required
def candles():
    try:
        return jsonify(market.candles(_und(request.args.get('underlying')), request.args.get('interval', '5m')))
    except (InstrumentError, ValueError) as exc:
        return _err(exc)


@bp.route('/api/t/search')
@login_required
def search():
    q = request.args.get('q', '')
    if len(q.strip()) < 2:
        return jsonify([])
    res = instruments.search(q, lambda u: market.spot(u)['ltp'])
    return jsonify([{'key': i.key, 'name': i.name, 'kind': i.kind} for i in res])


# ---- watchlist ------------------------------------------------------------------
@bp.route('/api/t/watchlist', methods=['GET'])
@login_required
def get_watchlist():
    return jsonify(broker.watchlist(_uid()))


@bp.route('/api/t/watchlist', methods=['POST'])
@login_required
def add_watch():
    try:
        broker.watch_add(_uid(), str(_body().get('key', '')))
    except broker.OrderError as exc:
        return _err(exc)
    return jsonify({'success': True})


@bp.route('/api/t/watchlist', methods=['DELETE'])
@login_required
def remove_watch():
    broker.watch_remove(_uid(), str(_body().get('key', '')))
    return jsonify({'success': True})


# ---- trading --------------------------------------------------------------------
@bp.route('/api/t/orders', methods=['POST'])
@login_required
def place():
    try:
        order = broker.place_order(_uid(), _body())
    except broker.OrderError as exc:
        return _err(exc)
    ok = order['status'] != 'REJECTED'
    return jsonify({'success': ok, 'order': order, 'error': None if ok else order['reason']}), 200 if ok else 400


@bp.route('/api/t/orders/preview', methods=['POST'])
@login_required
def preview():
    try:
        return jsonify({'success': True, **broker.preview(_uid(), _body())})
    except broker.OrderError as exc:
        return _err(exc)


@bp.route('/api/t/orders', methods=['GET'])
@login_required
def list_orders():
    broker.process(_uid())
    return jsonify(broker.orders(_uid()))


@bp.route('/api/t/orders/<int:oid>', methods=['DELETE'])
@login_required
def cancel(oid):
    try:
        broker.cancel_order(_uid(), oid)
    except broker.OrderError as exc:
        return _err(exc)
    return jsonify({'success': True})


@bp.route('/api/t/positions', methods=['GET'])
@login_required
def list_positions():
    broker.process(_uid())
    return jsonify(broker.positions(_uid()))


@bp.route('/api/t/positions/exit', methods=['POST'])
@login_required
def exit_pos():
    b = _body()
    try:
        broker.exit_position(_uid(), str(b.get('key', '')), str(b.get('product', '')))
    except (broker.OrderError, InstrumentError) as exc:
        return _err(exc)
    return jsonify({'success': True})


@bp.route('/api/t/trades')
@login_required
def list_trades():
    return jsonify(broker.trades(_uid()))


@bp.route('/api/t/funds')
@login_required
def get_funds():
    broker.process(_uid())
    return jsonify(broker.funds(_uid()))


@bp.route('/api/t/funds/reset', methods=['POST'])
@login_required
def reset_funds():
    if not _body().get('confirm'):
        return _err('confirm required')
    broker.reset_account(_uid())
    return jsonify({'success': True})


def start_scheduler(interval_seconds=20):
    """Match resting orders, settle expiries and square off MIS while the server is up."""
    def loop():
        while True:
            time.sleep(interval_seconds)
            try:
                if broker.active_users():
                    broker.process()
            except Exception as exc:
                print(f'[trader] processing failed: {exc}')
    threading.Thread(target=loop, name='trader-scheduler', daemon=True).start()
