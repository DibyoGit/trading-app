"""SDK reference plugin: shows the plugin API. It is code documentation, not a
recommended strategy; nobody should trade it as-is.

Operators install Python plugins by dropping a file in this folder and
restarting the server. Each class that subclasses `Strategy` appears in the
Studio under "Python plugin".
"""
from engine.strategy_base import Leg, RiskLimits, Strategy


class VolSqueezeStraddleExample(Strategy):
    name = 'SDK example: vol squeeze straddle'
    description = 'Buys an ATM straddle when short-term vol is compressed versus long-term vol.'
    indicators = ['hv_10', 'hv_60']
    models = []
    risk = RiskLimits(stop_loss_pct=35, target_pct=50, max_hold_bars=5)

    def on_bar(self, ctx):
        ratio = self.params.get('squeeze_ratio', 0.7)
        short, long_ = ctx.value('hv_10'), ctx.value('hv_60')
        if short is None or long_ is None:
            return
        if ctx.position is None and short < ratio * long_:
            lots = int(self.params.get('lots', 1))
            ctx.open([Leg('BUY', 'CE', 'ATM', lots, 7), Leg('BUY', 'PE', 'ATM', lots, 7)], tag='vol squeeze')
        elif ctx.position is not None and short > long_:
            ctx.close('vol normalised')
