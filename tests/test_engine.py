import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import data  # noqa: E402
from engine.ai import models as ai_models  # noqa: E402
from engine.indicators import enrich  # noqa: E402
from engine.pricing import bs_greeks, bs_price, implied_vol, resolve_strike  # noqa: E402
from engine.rules import RuleError, RuleStrategy, evaluate, parse_expr, validate_spec  # noqa: E402
from engine.simulator import Simulator, metrics  # noqa: E402
from engine.strategy_base import Leg, RiskLimits, Strategy  # noqa: E402


@pytest.fixture
def bars():
    return data._synthetic('NIFTY', periods=800, seed=3)


# ---- pricing ------------------------------------------------------------------
def test_put_call_parity():
    s, k, t, r, v = 24000, 24100, 30 / 365, 0.065, 0.15
    c, p = bs_price(s, k, t, r, v, 'CE'), bs_price(s, k, t, r, v, 'PE')
    assert math.isclose(c - p, s - k * math.exp(-r * t), rel_tol=1e-9)


def test_implied_vol_roundtrip():
    px = bs_price(24000, 24000, 7 / 365, 0.065, 0.18, 'CE')
    assert abs(implied_vol(px, 24000, 24000, 7 / 365, 0.065, 'CE') - 0.18) < 1e-3


def test_greeks_signs():
    g = bs_greeks(24000, 24000, 7 / 365, 0.065, 0.15, 'PE')
    assert -1 < g['delta'] < 0 and g['gamma'] > 0 and g['theta'] < 0 and g['vega'] > 0


def test_resolve_strike():
    assert resolve_strike('ATM', 24012, 50) == 24000
    assert resolve_strike('ATM+2', 24012, 50) == 24100
    assert resolve_strike('ATM-1', 24012, 50) == 23950
    assert resolve_strike(24500, 24012, 50) == 24500


# ---- rules / sandbox ------------------------------------------------------------
@pytest.mark.parametrize('expr', [
    "__import__('os').system('echo hi')",
    "close.__class__",
    "open[0]",
    "(lambda: 1)()",
    "eval('1')",
    "'abc' == 'abc'",
])
def test_rule_sandbox_rejects_code(expr):
    with pytest.raises(RuleError):
        parse_expr(expr)


def test_rule_evaluation_and_nan():
    tree = parse_expr('rsi_14 < 30 and close > sma_200')
    assert evaluate(tree, {'rsi_14': 25, 'close': 100, 'sma_200': 90}, {}) is True
    assert evaluate(tree, {'rsi_14': 25, 'close': 100, 'sma_200': float('nan')}, {}) is False


def test_crossed_above():
    tree = parse_expr('crossed_above(ema_9, ema_21)')
    assert evaluate(tree, {'ema_9': 11, 'ema_21': 10}, {'ema_9': 9, 'ema_21': 10})
    assert not evaluate(tree, {'ema_9': 11, 'ema_21': 10}, {'ema_9': 12, 'ema_21': 10})


def test_validate_spec_unknown_indicator():
    with pytest.raises(RuleError):
        validate_spec({'name': 'x', 'entry': 'foo_9 > 1', 'legs': [{'action': 'BUY', 'type': 'CE'}]})


def test_validate_spec_lot_cap():
    with pytest.raises(RuleError):
        validate_spec({'name': 'x', 'entry': 'close > 0', 'legs': [{'action': 'BUY', 'type': 'CE', 'lots': 50}]})


# ---- simulator --------------------------------------------------------------------
class PeekStrategy(Strategy):
    """Records what it sees to prove no future data leaks into on_bar."""
    indicators = []

    def __init__(self):
        super().__init__()
        self.seen = []

    def on_bar(self, ctx):
        self.seen.append((ctx.i, ctx.bar['close']))


def test_no_lookahead(bars):
    s = PeekStrategy()
    Simulator(s, bars.copy(), 65, 50).run(start=1)
    closes = bars['close'].tolist()
    assert all(closes[i] == c for i, c in s.seen)


def test_fill_next_open_and_costs(bars):
    class OnceStrategy(Strategy):
        risk = RiskLimits(max_hold_bars=3)

        def on_bar(self, ctx):
            if ctx.i == 50 and ctx.position is None:
                ctx.open([Leg('BUY', 'CE', 'ATM', 1, 14)])

    sim = Simulator(OnceStrategy(), bars.copy(), 65, 50)
    state = sim.run(start=1)
    first = state['orders'][0]
    assert first['kind'] == 'OPEN' and first['date'] == bars.index[51].strftime('%Y-%m-%d')
    assert len(state['trades']) == 1 and state['trades'][0]['reason'].startswith('max hold')
    assert state['costs'] > 0
    eq_end = state['equity'][-1]['equity']
    assert math.isclose(eq_end, 500000 + state['trades'][0]['pnl'], abs_tol=0.05)


def test_rule_strategy_backtest_runs(bars):
    spec = {'name': 't', 'entry': 'rsi_14 < 40', 'exit': 'rsi_14 > 60',
            'legs': [{'action': 'SELL', 'type': 'PE', 'strike': 'ATM-2', 'lots': 1, 'expiry_days': 7}],
            'risk': {'stop_loss_pct': 100}}
    strat = RuleStrategy(spec)
    df = enrich(bars, strat.indicators)
    state = Simulator(strat, df, 65, 50).run(start=1)
    m = metrics(state, 500000)
    assert m['trades'] > 0 and 'max_drawdown_pct' in m


def test_simulator_state_resume_matches_full_run(bars):
    """Paper deployments resume from saved state; results must equal one continuous run."""
    spec = {'name': 't', 'entry': 'crossed_above(ema_5, ema_20)', 'exit': 'crossed_below(ema_5, ema_20)',
            'legs': [{'action': 'BUY', 'type': 'CE', 'strike': 'ATM', 'lots': 1, 'expiry_days': 21}]}
    df = enrich(bars, RuleStrategy(spec).indicators)
    full = Simulator(RuleStrategy(spec), df.copy(), 65, 50).run(start=1)
    a = Simulator(RuleStrategy(spec), df.copy(), 65, 50)
    a.run(start=1, end=400)
    import json
    saved = json.loads(json.dumps(a.state))
    b = Simulator(RuleStrategy(spec), df.copy(), 65, 50, state=saved)
    b.run(start=400)
    assert [t['pnl'] for t in b.state['trades']] == [t['pnl'] for t in full['trades']]


# ---- AI models ------------------------------------------------------------------
def test_walk_forward_has_no_label_leak(bars):
    """On a random walk, honest OOS accuracy must hover near the baseline."""
    cfg = ai_models.validate_config({'name': 'rw', 'model_type': 'logreg', 'horizon': 5})
    oos, m, _ = ai_models.walk_forward(bars, cfg)
    assert m['oos_samples'] > 200
    assert m['auc'] < 0.62
    first_pred = oos.first_valid_index()
    assert bars.index.get_loc(first_pred) >= cfg['min_train']


def test_model_config_validation():
    with pytest.raises(ai_models.ModelError):
        ai_models.validate_config({'name': 'Bad Name!'})
    with pytest.raises(ai_models.ModelError):
        ai_models.validate_config({'name': 'ok', 'features': ['nope_3']})
