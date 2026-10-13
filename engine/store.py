"""SQLite persistence for the strategy platform (same DB file as the legacy app)."""
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get('DB_PATH', 'trading.db')
DISCLAIMER_VERSION = '2026-10-v1'

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_strategies (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    spec TEXT NOT NULL, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, strategy_id INTEGER, strategy_name TEXT,
    settings TEXT, metrics TEXT, equity TEXT, trades TEXT, data_source TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ai_models (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, config TEXT,
    metrics TEXT, importance TEXT, data_source TEXT, created_at TEXT, UNIQUE(user_id, name));
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, strategy_id INTEGER NOT NULL, mode TEXT NOT NULL,
    status TEXT NOT NULL, settings TEXT, state TEXT, last_bar TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS api_keys (
    user_id INTEGER PRIMARY KEY, key_hash TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, version TEXT NOT NULL, accepted_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, user_id INTEGER, ts TEXT, event TEXT, detail TEXT);
"""


def now():
    return datetime.now().isoformat(timespec='seconds')


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)


def audit(user_id, event, detail=None):
    """Append-only trail: who authored/changed/ran what, and when."""
    with connect() as conn:
        conn.execute('INSERT INTO audit_log (user_id, ts, event, detail) VALUES (?, ?, ?, ?)',
                     (user_id, now(), event, json.dumps(detail or {}, default=str)[:4000]))


def _row(r, json_fields=()):
    if r is None:
        return None
    d = dict(r)
    for f in json_fields:
        if d.get(f):
            d[f] = json.loads(d[f])
    return d


# ---- consent ---------------------------------------------------------------
def has_consent(user_id):
    with connect() as conn:
        r = conn.execute('SELECT 1 FROM consents WHERE user_id=? AND version=?',
                         (user_id, DISCLAIMER_VERSION)).fetchone()
    return r is not None


def record_consent(user_id):
    with connect() as conn:
        conn.execute('INSERT INTO consents (user_id, version, accepted_at) VALUES (?, ?, ?)',
                     (user_id, DISCLAIMER_VERSION, now()))
    audit(user_id, 'consent.accepted', {'version': DISCLAIMER_VERSION})


# ---- strategies --------------------------------------------------------------
def list_strategies(user_id):
    with connect() as conn:
        rows = conn.execute('SELECT * FROM user_strategies WHERE user_id=? ORDER BY updated_at DESC',
                            (user_id,)).fetchall()
    return [_row(r, ('spec',)) for r in rows]


def get_strategy(user_id, sid):
    with connect() as conn:
        r = conn.execute('SELECT * FROM user_strategies WHERE id=? AND user_id=?', (sid, user_id)).fetchone()
    return _row(r, ('spec',))


def save_strategy(user_id, name, kind, spec, sid=None):
    with connect() as conn:
        if sid:
            cur = conn.execute('UPDATE user_strategies SET name=?, kind=?, spec=?, updated_at=? WHERE id=? AND user_id=?',
                               (name, kind, json.dumps(spec), now(), sid, user_id))
            if cur.rowcount == 0:
                return None
        else:
            cur = conn.execute('INSERT INTO user_strategies (user_id, name, kind, spec, created_at, updated_at) '
                               'VALUES (?, ?, ?, ?, ?, ?)', (user_id, name, kind, json.dumps(spec), now(), now()))
            sid = cur.lastrowid
    audit(user_id, 'strategy.saved', {'id': sid, 'name': name, 'kind': kind, 'spec': spec})
    return sid


def delete_strategy(user_id, sid):
    with connect() as conn:
        conn.execute('DELETE FROM deployments WHERE strategy_id=? AND user_id=?', (sid, user_id))
        conn.execute('DELETE FROM user_strategies WHERE id=? AND user_id=?', (sid, user_id))
    audit(user_id, 'strategy.deleted', {'id': sid})


# ---- backtests ----------------------------------------------------------------
def save_backtest(user_id, strategy_id, strategy_name, settings, metrics, equity, trades, source):
    with connect() as conn:
        cur = conn.execute('INSERT INTO backtests (user_id, strategy_id, strategy_name, settings, metrics, equity, '
                           'trades, data_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (user_id, strategy_id, strategy_name, json.dumps(settings), json.dumps(metrics),
                            json.dumps(equity), json.dumps(trades), source, now()))
    return cur.lastrowid


def list_backtests(user_id, limit=20):
    with connect() as conn:
        rows = conn.execute('SELECT id, strategy_id, strategy_name, settings, metrics, data_source, created_at '
                            'FROM backtests WHERE user_id=? ORDER BY id DESC LIMIT ?', (user_id, limit)).fetchall()
    return [_row(r, ('settings', 'metrics')) for r in rows]


def get_backtest(user_id, bid):
    with connect() as conn:
        r = conn.execute('SELECT * FROM backtests WHERE id=? AND user_id=?', (bid, user_id)).fetchone()
    return _row(r, ('settings', 'metrics', 'equity', 'trades'))


# ---- models -------------------------------------------------------------------
def save_model(user_id, cfg, metrics, importance, source):
    with connect() as conn:
        conn.execute('INSERT INTO ai_models (user_id, name, config, metrics, importance, data_source, created_at) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, name) DO UPDATE SET config=excluded.config, '
                     'metrics=excluded.metrics, importance=excluded.importance, data_source=excluded.data_source, '
                     'created_at=excluded.created_at',
                     (user_id, cfg['name'], json.dumps(cfg), json.dumps(metrics), json.dumps(importance), source, now()))
    audit(user_id, 'model.trained', {'name': cfg['name'], 'config': cfg, 'metrics': metrics})


def list_models(user_id):
    with connect() as conn:
        rows = conn.execute('SELECT * FROM ai_models WHERE user_id=? ORDER BY created_at DESC', (user_id,)).fetchall()
    return [_row(r, ('config', 'metrics', 'importance')) for r in rows]


def model_names(user_id):
    return {m['name'] for m in list_models(user_id)}


def delete_model(user_id, name):
    with connect() as conn:
        conn.execute('DELETE FROM ai_models WHERE user_id=? AND name=?', (user_id, name))
    audit(user_id, 'model.deleted', {'name': name})


# ---- deployments ---------------------------------------------------------------
def create_deployment(user_id, strategy_id, settings):
    with connect() as conn:
        cur = conn.execute('INSERT INTO deployments (user_id, strategy_id, mode, status, settings, state, created_at, '
                           'updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (user_id, strategy_id, 'paper', 'active', json.dumps(settings), None, now(), now()))
    audit(user_id, 'deployment.created', {'id': cur.lastrowid, 'strategy_id': strategy_id, 'settings': settings})
    return cur.lastrowid


def list_deployments(user_id):
    with connect() as conn:
        rows = conn.execute('SELECT d.*, s.name AS strategy_name FROM deployments d JOIN user_strategies s '
                            'ON s.id = d.strategy_id WHERE d.user_id=? ORDER BY d.id DESC', (user_id,)).fetchall()
    return [_row(r, ('settings', 'state')) for r in rows]


def get_deployment(user_id, did):
    with connect() as conn:
        r = conn.execute('SELECT * FROM deployments WHERE id=? AND user_id=?', (did, user_id)).fetchone()
    return _row(r, ('settings', 'state'))


def active_deployments():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM deployments WHERE status='active'").fetchall()
    return [_row(r, ('settings', 'state')) for r in rows]


def update_deployment(did, **fields):
    fields['updated_at'] = now()
    for k in ('settings', 'state'):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    cols = ', '.join(f'{k}=?' for k in fields)
    with connect() as conn:
        conn.execute(f'UPDATE deployments SET {cols} WHERE id=?', (*fields.values(), did))


# ---- API keys (for external strategy webhooks) ---------------------------------
def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def rotate_api_key(user_id):
    key = 'sk_strat_' + secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute('INSERT INTO api_keys (user_id, key_hash, created_at) VALUES (?, ?, ?) '
                     'ON CONFLICT(user_id) DO UPDATE SET key_hash=excluded.key_hash, created_at=excluded.created_at',
                     (user_id, _hash(key), now()))
    audit(user_id, 'api_key.rotated')
    return key


def has_api_key(user_id):
    with connect() as conn:
        return conn.execute('SELECT 1 FROM api_keys WHERE user_id=?', (user_id,)).fetchone() is not None


def user_for_key(key):
    if not key:
        return None
    with connect() as conn:
        r = conn.execute('SELECT user_id FROM api_keys WHERE key_hash=?', (_hash(key),)).fetchone()
    return r['user_id'] if r else None


def list_audit(user_id, limit=50):
    with connect() as conn:
        rows = conn.execute('SELECT ts, event, detail FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT ?',
                            (user_id, limit)).fetchall()
    return [_row(r, ('detail',)) for r in rows]
