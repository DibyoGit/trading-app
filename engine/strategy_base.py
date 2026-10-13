"""Strategy SDK.

A strategy is code the *user* writes. It receives one completed bar at a time
and may request to open or close an options position. The simulator decides
fills (next bar open), costs and risk exits; the strategy only expresses intent.

Minimal plugin example::

    from engine.strategy_base import Strategy, Leg

    class MyStraddle(Strategy):
        name = 'My straddle'
        indicators = ['hv_10', 'hv_30']

        def on_bar(self, ctx):
            if ctx.position is None and ctx.value('hv_10') < 0.8 * ctx.value('hv_30'):
                ctx.open([Leg('BUY', 'CE', 'ATM'), Leg('BUY', 'PE', 'ATM')], tag='vol squeeze')
            elif ctx.position is not None and ctx.bars_held >= 3:
                ctx.close('time exit')
"""
from dataclasses import dataclass, asdict, field

VALID_ACTIONS = ('BUY', 'SELL')
VALID_TYPES = ('CE', 'PE')


@dataclass
class Leg:
    action: str            # BUY / SELL
    opt_type: str          # CE / PE
    strike: object = 'ATM'  # 'ATM', 'ATM+2', 'ATM-1' or absolute strike
    lots: int = 1
    expiry_days: int = 7   # calendar days to expiry at entry (snapped to expiry weekday)

    def __post_init__(self):
        self.action = str(self.action).upper()
        self.opt_type = str(self.opt_type).upper()
        if self.action not in VALID_ACTIONS:
            raise ValueError(f'leg action must be BUY or SELL, got {self.action}')
        if self.opt_type not in VALID_TYPES:
            raise ValueError(f'leg type must be CE or PE, got {self.opt_type}')
        self.lots = int(self.lots)
        self.expiry_days = int(self.expiry_days)
        if self.lots < 1:
            raise ValueError('lots must be >= 1')
        if self.expiry_days < 1:
            raise ValueError('expiry_days must be >= 1')

    @classmethod
    def from_dict(cls, d):
        return cls(action=d['action'], opt_type=d.get('type') or d.get('opt_type'),
                   strike=d.get('strike', 'ATM'), lots=d.get('lots', 1),
                   expiry_days=d.get('expiry_days', 7))

    def to_dict(self):
        d = asdict(self)
        d['type'] = d.pop('opt_type')
        return d


@dataclass
class RiskLimits:
    stop_loss_pct: float = 0      # exit when loss >= this % of entry premium basis (0 = off)
    target_pct: float = 0         # exit when profit >= this % of entry premium basis (0 = off)
    max_hold_bars: int = 0        # exit after N bars (0 = off)
    max_lots_per_leg: int = 10    # hard cap, protects against fat-finger specs
    exit_before_expiry_days: int = 0  # exit when any leg has <= N calendar days left

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def to_dict(self):
        return asdict(self)


class Strategy:
    """Base class for user strategies."""
    name = 'Unnamed strategy'
    description = ''
    indicators = []   # indicator names this strategy reads, e.g. ['sma_20', 'rsi_14']
    models = []       # AI model names (trained by the same user) whose outputs it reads
    risk = None       # optional RiskLimits

    def __init__(self, params=None):
        self.params = params or {}

    def on_start(self, ctx):
        pass

    def on_bar(self, ctx):
        raise NotImplementedError


@dataclass
class Context:
    """What a strategy sees on each bar. Built by the simulator."""
    i: int
    date: object
    bar: dict
    prev: dict
    position: object
    bars_held: int
    params: dict
    _intents: list = field(default_factory=list)
    _logs: list = field(default_factory=list)

    def value(self, name, default=None):
        v = self.bar.get(name, default)
        return default if v != v else v  # NaN -> default

    def prev_value(self, name, default=None):
        v = self.prev.get(name, default) if self.prev else default
        return default if v != v else v

    def open(self, legs, tag=''):
        legs = [leg if isinstance(leg, Leg) else Leg.from_dict(leg) for leg in legs]
        self._intents.append({'kind': 'open', 'legs': [leg.to_dict() for leg in legs], 'tag': tag})

    def close(self, reason='strategy exit'):
        self._intents.append({'kind': 'close', 'reason': reason})

    def log(self, msg):
        self._logs.append(str(msg))
