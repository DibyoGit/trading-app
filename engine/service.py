"""Orchestration used by the HTTP layer: build strategies, run backtests, train
models, advance paper deployments, accept external intents."""
import threading
import time

from . import data, plugins, store
from .ai import models as ai_models
from .indicators import enrich
from .rules import RuleError, RuleStrategy, validate_spec
from .simulator import DEFAULTS, Simulator, metrics
from .strategy_base import Leg, RiskLimits, Strategy

SETTING_LIMITS = {
    'capital': (10000, 1e8), 'iv_multiplier': (0.5, 3.0), 'slippage_pct': (0, 10),
    'cost_per_order': (0, 500), 'short_margin_pct': (5, 50), 'risk_free_rate': (0, 0.2),
}
PERIODS = ('1y', '2y', '3y', '5y', '10y', 'max')


class ExternalStrategy(Strategy):
    """Placeholder for strategies that run on the user's own infrastructure and
    send intents through the webhook. The simulator only fills what arrives."""
    name = 'External strategy'

    def __init__(self, spec, params=None):
        super().__init__(params)
        self.name = spec.get('name', self.name)
        self.risk = RiskLimits.from_dict(spec.get('risk'))

    def on_bar(self, ctx):
        pass


def normalise_strategy(kind, spec):
    """Validate a strategy definition before it is saved."""
    if kind == 'rules':
        return validate_spec(spec)
    if kind == 'plugin':
        key = spec.get('plugin')
        cls = plugins.discover().get(key)
        if cls is None:
            raise RuleError(f'unknown plugin "{key}"')
        return {'name': spec.get('name') or cls.name, 'plugin': key, 'params': spec.get('params') or {},
                'underlying': str(spec.get('underlying', 'NIFTY')).upper(),
                'risk': RiskLimits.from_dict(spec.get('risk')).to_dict()}
    if kind == 'external':
        name = str(spec.get('name', '')).strip()
        if not name:
            raise RuleError('name is required')
        return {'name': name, 'underlying': str(spec.get('underlying', 'NIFTY')).upper(),
                'risk': RiskLimits.from_dict(spec.get('risk')).to_dict(),
                'description': str(spec.get('description', ''))[:1000]}
    raise RuleError(f'unknown strategy kind {kind}')


def build_strategy(row):
    kind, spec = row['kind'], row['spec']
    if kind == 'rules':
        return RuleStrategy(spec)
    if kind == 'plugin':
        cls = plugins.discover().get(spec['plugin'])
        if cls is None:
            raise RuleError(f"plugin {spec['plugin']} is no longer installed")
        strat = cls(spec.get('params'))
        strat.risk = RiskLimits.from_dict(spec.get('risk')) if spec.get('risk') else (cls.risk or RiskLimits())
        return strat
    return ExternalStrategy(spec)


def clean_settings(raw, config):
    s = dict(DEFAULTS)
    for key in ('risk_free_rate', 'cost_per_order', 'slippage_pct', 'short_margin_pct', 'iv_multiplier'):
        if key in config:
            s[key] = float(config[key])
    for key, (lo, hi) in SETTING_LIMITS.items():
        if raw and raw.get(key) not in (None, ''):
            s[key] = min(hi, max(lo, float(raw[key])))
    return s


def prepare_frame(strategy, user_id, underlying, period='5y', live=False):
    df, source = data.load_bars(underlying, period)
    df = enrich(df, list(strategy.indicators))
    missing = set(strategy.models) - store.model_names(user_id)
    if missing:
        raise RuleError(f'strategy reads model(s) you have not trained: {", ".join(sorted(missing))}')
    df = ai_models.attach_model_columns(df, user_id, strategy.models, live=live)
    return df, source


def run_backtest(user_id, row, raw_settings, config):
    period = raw_settings.get('period', '5y') if raw_settings else '5y'
    if period not in PERIODS:
        period = '5y'
    strategy = build_strategy(row)
    underlying = row['spec'].get('underlying', 'NIFTY')
    meta = data.underlying_meta(underlying, config)
    settings = clean_settings(raw_settings, config)
    df, source = prepare_frame(strategy, user_id, underlying, period)
    sim = Simulator(strategy, df, meta['lot_size'], meta['strike_step'], settings)
    t0 = time.perf_counter()
    state = sim.run(start=1)
    m = metrics(state, settings['capital'])
    m['compute_seconds'] = round(time.perf_counter() - t0, 2)
    m['bars'] = len(df)
    m['first_date'], m['last_date'] = df.index[0].strftime('%Y-%m-%d'), df.index[-1].strftime('%Y-%m-%d')
    settings.update({'period': period, 'underlying': underlying, 'lot_size': meta['lot_size']})
    bid = store.save_backtest(user_id, row['id'], row['name'], settings, m, state['equity'], state['trades'], source)
    store.audit(user_id, 'backtest.run', {'backtest_id': bid, 'strategy_id': row['id']})
    return {'id': bid, 'metrics': m, 'equity': state['equity'], 'trades': state['trades'][-200:],
            'data_source': source, 'settings': settings}


def train_model(user_id, raw_cfg):
    cfg = ai_models.validate_config(raw_cfg)
    df, source = data.load_bars(cfg['underlying'], raw_cfg.get('period', '10y') if raw_cfg.get('period') in PERIODS else '10y')
    oos, m, model = ai_models.walk_forward(df, cfg)
    importance = m.pop('importance')
    ai_models.save(user_id, cfg, model, oos, m)
    store.save_model(user_id, cfg, m, importance, source)
    return {'config': cfg, 'metrics': m, 'importance': importance, 'data_source': source}


# ---- paper deployments ------------------------------------------------------------
_tick_lock = threading.Lock()


def _sim_for(dep, row, config):
    strategy = build_strategy(row)
    underlying = row['spec'].get('underlying', 'NIFTY')
    meta = data.underlying_meta(underlying, config)
    settings = dep['settings']
    df, _ = prepare_frame(strategy, dep['user_id'], underlying, '1y', live=True)
    return Simulator(strategy, df, meta['lot_size'], meta['strike_step'], settings, state=dep['state'])


def tick_deployment(dep, config):
    """Process every bar newer than the last one this deployment has seen."""
    if dep['status'] != 'active':
        return {'processed': 0}
    row = store.get_strategy(dep['user_id'], dep['strategy_id'])
    if row is None:
        store.update_deployment(dep['id'], status='orphaned')
        return {'processed': 0}
    with _tick_lock:
        sim = _sim_for(dep, row, config)
        dates = [d.strftime('%Y-%m-%d') for d in sim.dates]
        if dep['last_bar']:
            todo = [i for i, d in enumerate(dates) if d > dep['last_bar']]
        else:
            todo = [len(dates) - 1]  # start live: decide on latest closed bar, fill next open
        for i in todo:
            sim.step(i)
        if todo:
            store.update_deployment(dep['id'], state=sim.state, last_bar=dates[todo[-1]])
    return {'processed': len(todo), 'last_bar': dates[todo[-1]] if todo else dep['last_bar']}


def deploy(user_id, row, raw_settings, config):
    settings = clean_settings(raw_settings, config)
    did = store.create_deployment(user_id, row['id'], settings)
    tick_deployment(store.get_deployment(user_id, did), config)
    return did


def submit_external_intent(user_id, payload):
    """Accept an intent from the user's own externally-hosted strategy."""
    did = int(payload.get('deployment_id', 0))
    dep = store.get_deployment(user_id, did)
    if not dep or dep['status'] != 'active':
        raise RuleError('active deployment not found')
    intent = str(payload.get('intent', '')).lower()
    state = dep['state']
    if state is None:
        raise RuleError('deployment not initialised yet')
    if intent == 'open':
        if state.get('position') or any(p['kind'] == 'open' for p in state['pending']):
            raise RuleError('a position is already open or pending')
        legs = payload.get('legs') or []
        if not 1 <= len(legs) <= 4:
            raise RuleError('1 to 4 legs required')
        try:
            legs = [Leg.from_dict(l).to_dict() for l in legs]
        except (KeyError, ValueError, TypeError) as exc:
            raise RuleError(f'invalid leg: {exc}') from None
        state['pending'].append({'kind': 'open', 'legs': legs, 'tag': str(payload.get('tag', 'webhook'))[:60]})
    elif intent == 'close':
        if not state.get('position'):
            raise RuleError('no open position')
        state['pending'].append({'kind': 'close', 'reason': str(payload.get('reason', 'webhook'))[:60]})
    else:
        raise RuleError('intent must be "open" or "close"')
    store.update_deployment(did, state=state)
    store.audit(user_id, 'webhook.intent', {'deployment_id': did, 'payload': payload})
    return {'queued': intent, 'fills_at': 'next bar open'}


def deployment_view(dep):
    st = dep.get('state') or {}
    eq = st.get('equity') or []
    return {
        'id': dep['id'], 'strategy_id': dep['strategy_id'], 'strategy_name': dep.get('strategy_name'),
        'mode': dep['mode'], 'status': dep['status'], 'last_bar': dep['last_bar'],
        'capital': (dep.get('settings') or {}).get('capital'),
        'equity': eq[-1]['equity'] if eq else None, 'position': st.get('position'),
        'pending': st.get('pending', []), 'orders': (st.get('orders') or [])[-30:],
        'trades': (st.get('trades') or [])[-30:], 'created_at': dep['created_at'],
    }


def start_scheduler(config, interval_seconds):
    """Background loop that advances all active paper deployments."""
    def loop():
        while True:
            time.sleep(interval_seconds)
            for dep in store.active_deployments():
                try:
                    tick_deployment(dep, config)
                except Exception as exc:
                    print(f"[scheduler] deployment {dep['id']} failed: {exc}")
    t = threading.Thread(target=loop, name='paper-scheduler', daemon=True)
    t.start()
    return t
