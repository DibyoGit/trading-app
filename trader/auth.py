"""Login and registration.

Passwords are stored with Werkzeug's salted scrypt hash. Accounts created by
the legacy app (unsalted MD5) are verified once and upgraded on next login.
"""
import hashlib
import hmac
import re
import time
from collections import defaultdict, deque

from werkzeug.security import check_password_hash, generate_password_hash

from engine import store

USERNAME_RE = re.compile(r'^[A-Za-z0-9_.]{3,30}$')
MIN_PASSWORD = 8
MAX_FAILURES, WINDOW = 10, 600  # per client IP

_failures = defaultdict(deque)


class AuthError(ValueError):
    pass


def _is_legacy(h):
    return bool(re.fullmatch(r'[0-9a-f]{32}', h or ''))


def _throttle(ip):
    q = _failures[ip]
    while q and time.time() - q[0] > WINDOW:
        q.popleft()
    if len(q) >= MAX_FAILURES:
        raise AuthError('Too many failed attempts. Try again in a few minutes.')


def login(username, password, ip):
    _throttle(ip)
    with store.connect() as conn:
        row = conn.execute('SELECT id, username, password FROM users WHERE username=?', (username,)).fetchone()
        ok = False
        if row:
            stored = row['password'] or ''
            if _is_legacy(stored):
                ok = hmac.compare_digest(stored, hashlib.md5(password.encode()).hexdigest())
                if ok:
                    conn.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash(password), row['id']))
            else:
                ok = check_password_hash(stored, password)
    if not ok:
        _failures[ip].append(time.time())
        raise AuthError('Invalid username or password')
    store.audit(row['id'], 'auth.login', {'ip': ip})
    return row['id'], row['username']


def register(username, password, ip):
    _throttle(ip)
    if not USERNAME_RE.match(username or ''):
        raise AuthError('Username: 3-30 letters, digits, dot or underscore')
    if len(password or '') < MIN_PASSWORD:
        raise AuthError(f'Password must be at least {MIN_PASSWORD} characters')
    with store.connect() as conn:
        if conn.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            raise AuthError('Username already taken')
        cur = conn.execute('INSERT INTO users (username, password) VALUES (?, ?)',
                           (username, generate_password_hash(password)))
        uid = cur.lastrowid
    store.audit(uid, 'auth.register', {'ip': ip})
    return uid, username
