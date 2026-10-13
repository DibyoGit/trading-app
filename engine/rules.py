"""No-code rule strategies: JSON spec + safe boolean expressions.

Expressions are parsed with `ast` and evaluated against a whitelist of node
types. No attribute access, subscripts, imports, lambdas or arbitrary calls, so
user-supplied rules cannot execute code on the server.

Example spec::

    {
      "name": "Oversold bounce call",
      "underlying": "NIFTY",
      "entry": "rsi_14 < 30 and close > sma_200",
      "exit": "rsi_14 > 55 or crossed_below(close, sma_20)",
      "legs": [{"action": "BUY", "type": "CE", "strike": "ATM", "lots": 1, "expiry_days": 14}],
      "risk": {"stop_loss_pct": 40, "target_pct": 80, "max_hold_bars": 8}
    }
"""
import ast
import math

from .indicators import compute
from .strategy_base import Leg, RiskLimits, Strategy

MAX_EXPR_LEN = 500

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Lt, ast.LtE, ast.Gt,
    ast.GtE, ast.Eq, ast.NotEq, ast.Name, ast.Load, ast.Constant, ast.Call,
)
_FUNCS = {'abs', 'min', 'max', 'crossed_above', 'crossed_below'}


class RuleError(ValueError):
    pass


def parse_expr(expr):
    if not isinstance(expr, str) or not expr.strip():
        raise RuleError('expression is empty')
    if len(expr) > MAX_EXPR_LEN:
        raise RuleError(f'expression longer than {MAX_EXPR_LEN} chars')
    try:
        tree = ast.parse(expr.strip(), mode='eval')
    except SyntaxError as exc:
        raise RuleError(f'syntax error: {exc.msg}') from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleError(f'not allowed in rules: {type(node).__name__}')
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            raise RuleError('only numeric constants are allowed')
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
                raise RuleError(f'allowed functions: {", ".join(sorted(_FUNCS))}')
            if node.func.id.startswith('crossed') and (
                    len(node.args) != 2 or not all(isinstance(a, (ast.Name, ast.Constant)) for a in node.args)):
                raise RuleError(f'{node.func.id}(a, b) takes two indicator names or numbers')
    return tree


def referenced_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _FUNCS and node.id not in ('True', 'False'):
            names.add(node.id)
    return names


def validate_names(names):
    """Check every name resolves to an indicator or a model reference."""
    import pandas as pd
    probe = pd.DataFrame({c: [1.0, 2.0, 3.0] for c in ('open', 'high', 'low', 'close', 'volume')},
                         index=pd.bdate_range('2024-01-01', periods=3))
    for name in names:
        if name.startswith('model_'):
            continue
        try:
            compute(probe, name)
        except KeyError:
            raise RuleError(f'unknown indicator "{name}"') from None


def _is_nan(v):
    return isinstance(v, float) and math.isnan(v)


def evaluate(tree, row, prev):
    """Evaluate a parsed expression. Missing / NaN data makes the rule False."""

    def ev(node, scope):
        if isinstance(node, ast.Expression):
            return ev(node.body, scope)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in ('True', 'False'):
                return node.id == 'True'
            v = scope.get(node.id, float('nan'))
            return float('nan') if v is None else v
        if isinstance(node, ast.BoolOp):
            vals = (ev(v, scope) for v in node.values)
            return all(bool(v) and not _is_nan(v) for v in vals) if isinstance(node.op, ast.And) \
                else any(bool(v) and not _is_nan(v) for v in vals)
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand, scope)
            if isinstance(node.op, ast.Not):
                return not v if not _is_nan(v) else False
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left, scope), ev(node.right, scope)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return a / b if b else float('nan')
        if isinstance(node, ast.Compare):
            left = ev(node.left, scope)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp, scope)
                if _is_nan(left) or _is_nan(right):
                    return False
                ok = {ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right,
                      ast.GtE: left >= right, ast.Eq: left == right, ast.NotEq: left != right}[type(op)]
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            fn = node.func.id
            if fn in ('crossed_above', 'crossed_below'):
                if not prev:
                    return False
                a, b = (ev(x, row) for x in node.args)
                pa, pb = (ev(x, prev) for x in node.args)
                if any(_is_nan(v) for v in (a, b, pa, pb)):
                    return False
                return (pa <= pb and a > b) if fn == 'crossed_above' else (pa >= pb and a < b)
            args = [ev(x, scope) for x in node.args]
            if any(_is_nan(v) for v in args):
                return float('nan')
            return {'abs': abs, 'min': min, 'max': max}[fn](*args)
        raise RuleError(f'unsupported node {type(node).__name__}')

    return ev(tree, row)


def validate_spec(spec):
    """Validate a rule spec dict. Returns normalized spec or raises RuleError."""
    if not isinstance(spec, dict):
        raise RuleError('spec must be a JSON object')
    name = str(spec.get('name', '')).strip()
    if not name:
        raise RuleError('name is required')
    entry_tree = parse_expr(spec.get('entry', ''))
    exit_expr = spec.get('exit') or ''
    exit_tree = parse_expr(exit_expr) if exit_expr.strip() else None
    names = referenced_names(entry_tree) | (referenced_names(exit_tree) if exit_tree else set())
    validate_names(names)
    legs_raw = spec.get('legs') or []
    if not legs_raw or len(legs_raw) > 4:
        raise RuleError('a strategy needs 1 to 4 option legs')
    try:
        legs = [Leg.from_dict(l) for l in legs_raw]
    except (KeyError, ValueError, TypeError) as exc:
        raise RuleError(f'invalid leg: {exc}') from None
    risk = RiskLimits.from_dict(spec.get('risk'))
    for leg in legs:
        if leg.lots > risk.max_lots_per_leg:
            raise RuleError(f'leg lots {leg.lots} exceed max_lots_per_leg {risk.max_lots_per_leg}')
    return {
        'name': name,
        'description': str(spec.get('description', ''))[:1000],
        'underlying': str(spec.get('underlying', 'NIFTY')).upper(),
        'entry': spec['entry'].strip(),
        'exit': exit_expr.strip(),
        'legs': [l.to_dict() for l in legs],
        'risk': risk.to_dict(),
        'names': sorted(names),
    }


class RuleStrategy(Strategy):
    """Strategy built from a validated rule spec."""

    def __init__(self, spec, params=None):
        super().__init__(params)
        spec = validate_spec(spec)
        self.spec = spec
        self.name = spec['name']
        self.description = spec['description']
        self.entry_tree = parse_expr(spec['entry'])
        self.exit_tree = parse_expr(spec['exit']) if spec['exit'] else None
        self.legs = [Leg.from_dict(l) for l in spec['legs']]
        self.risk = RiskLimits.from_dict(spec['risk'])
        self.indicators = [n for n in spec['names'] if not n.startswith('model_')]
        self.models = [n[len('model_'):] for n in spec['names'] if n.startswith('model_')]

    def on_bar(self, ctx):
        if ctx.position is None:
            if evaluate(self.entry_tree, ctx.bar, ctx.prev):
                ctx.open(self.legs, tag='entry rule')
        elif self.exit_tree is not None and evaluate(self.exit_tree, ctx.bar, ctx.prev):
            ctx.close('exit rule')
