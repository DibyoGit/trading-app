"""Event-driven options simulator shared by backtests and paper deployments.

Timing model (no look-ahead):
  * strategy sees bar i only after it closes
  * intents raised on bar i fill at bar i+1 OPEN
  * positions are marked to market at each CLOSE
  * risk exits (stop / target / time) detected at close i, filled at open i+1
  * legs reaching expiry settle at intrinsic value on the expiry bar's close

Option premiums are modelled with Black-Scholes using historical volatility
times a configurable IV multiplier, because free historical NSE option-chain
data is not available. Treat results as an approximation and plug in a real
option-chain source for production-grade research.
"""
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from .pricing import bs_price, resolve_strike
from .strategy_base import Context, RiskLimits

DEFAULTS = {
    'capital': 500000.0,
    'risk_free_rate': 0.065,
    'iv_multiplier': 1.10,      # IV usually trades above realised vol
    'iv_floor': 0.08,
    'vol_lookback': 20,
    'slippage_pct': 0.5,        # % of premium paid on every fill
    'cost_per_order': 20.0,     # flat brokerage per leg per side
    'sell_stt_pct': 0.1,        # STT on option sell premium
    'short_margin_pct': 12.0,   # approx SPAN+exposure, % of notional per short leg
    'expiry_weekday': 1,        # NIFTY weekly expiry: Tuesday (0=Mon)
}


def _snap_expiry(date, days, weekday):
    target = (date + timedelta(days=days)).normalize()
    return target + timedelta(days=(weekday - target.weekday()) % 7)


class Simulator:
    def __init__(self, strategy, df, lot_size, strike_step, settings=None, state=None):
        self.strategy = strategy
        self.df = df
        self.lot_size = int(lot_size)
        self.step_size = strike_step
        self.cfg = {**DEFAULTS, **(settings or {})}
        self.risk = strategy.risk or RiskLimits()
        vol_col = f"hv_{int(self.cfg['vol_lookback'])}"
        if vol_col not in df.columns:
            from .indicators import hv
            df[vol_col] = hv(df['close'], int(self.cfg['vol_lookback']))
        self.vol_col = vol_col
        self.records = df.to_dict('records')
        self.dates = list(df.index)
        self.state = state or {
            'cash': float(self.cfg['capital']), 'position': None, 'pending': [],
            'trades': [], 'equity': [], 'orders': [], 'bars_held': 0, 'last_i': -1,
            'costs': 0.0, 'rejections': 0,
        }

    # ---- pricing -----------------------------------------------------------
    def _sigma(self, i):
        v = self.records[i].get(self.vol_col)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = 0.15
        return max(self.cfg['iv_floor'], v * self.cfg['iv_multiplier'])

    def _leg_price(self, leg, spot, date, sigma):
        t = max(0.0, (pd.Timestamp(leg['expiry']) - pd.Timestamp(date)).days) / 365.0
        if t == 0:
            return max(0.0, spot - leg['strike']) if leg['type'] == 'CE' else max(0.0, leg['strike'] - spot)
        return bs_price(spot, leg['strike'], t, self.cfg['risk_free_rate'], sigma, leg['type'])

    def _position_value(self, spot, date, sigma):
        pos = self.state['position']
        if not pos:
            return 0.0
        total = 0.0
        for leg in pos['legs']:
            px = self._leg_price(leg, spot, date, sigma)
            leg['mark'] = round(px, 2)
            total += (1 if leg['action'] == 'BUY' else -1) * px * leg['qty']
        return total

    # ---- order handling ----------------------------------------------------
    def _fill_cost(self, premium, qty, action):
        slip = premium * self.cfg['slippage_pct'] / 100
        fill = premium + slip if action == 'BUY' else max(0.05, premium - slip)
        cost = self.cfg['cost_per_order']
        if action == 'SELL':
            cost += fill * qty * self.cfg['sell_stt_pct'] / 100
        return fill, cost

    def _open(self, intent, i, spot):
        date = pd.Timestamp(self.dates[i])
        sigma = self._sigma(i - 1 if i > 0 else i)  # vol known at decision time
        legs, cash_needed, cost_total = [], 0.0, 0.0
        for spec in intent['legs']:
            lots = min(int(spec['lots']), self.risk.max_lots_per_leg)
            strike = resolve_strike(spec['strike'], spot, self.step_size)
            expiry = _snap_expiry(date, int(spec['expiry_days']), self.cfg['expiry_weekday'])
            leg = {'action': spec['action'], 'type': spec['type'], 'strike': strike,
                   'expiry': expiry.strftime('%Y-%m-%d'), 'lots': lots, 'qty': lots * self.lot_size}
            premium = self._leg_price(leg, spot, date, sigma)
            fill, cost = self._fill_cost(premium, leg['qty'], leg['action'])
            leg['entry'] = round(fill, 2)
            if leg['action'] == 'BUY':
                cash_needed += fill * leg['qty']
            else:
                cash_needed += spot * leg['qty'] * self.cfg['short_margin_pct'] / 100 - fill * leg['qty']
            cost_total += cost
            legs.append(leg)
        if cash_needed + cost_total > self.state['cash']:
            self.state['rejections'] += 1
            self._order(i, 'REJECTED', legs, f"insufficient capital (needs ~{cash_needed + cost_total:,.0f})")
            return
        for leg in legs:
            sign = -1 if leg['action'] == 'BUY' else 1
            self.state['cash'] += sign * leg['entry'] * leg['qty']
        self.state['cash'] -= cost_total
        self.state['costs'] += cost_total
        basis = sum(l['entry'] * l['qty'] for l in legs)
        self.state['position'] = {'legs': legs, 'entry_date': date.strftime('%Y-%m-%d'),
                                  'entry_spot': round(spot, 2), 'basis': basis,
                                  'net_entry': sum((1 if l['action'] == 'BUY' else -1) * l['entry'] * l['qty'] for l in legs),
                                  'entry_cost': cost_total, 'tag': intent.get('tag', '')}
        self.state['bars_held'] = 0
        self._order(i, 'OPEN', legs, intent.get('tag', ''))

    def _close(self, i, spot, reason, settle=False):
        pos = self.state['position']
        date = pd.Timestamp(self.dates[i])
        sigma = self._sigma(i)
        exit_value, cost_total = 0.0, 0.0
        for leg in pos['legs']:
            premium = self._leg_price(leg, spot, date, sigma)
            closing_action = 'SELL' if leg['action'] == 'BUY' else 'BUY'
            if settle or premium <= 0:  # exchange settlement: no slippage or brokerage
                fill, cost = premium, 0.0
            else:
                fill, cost = self._fill_cost(premium, leg['qty'], closing_action)
            leg['exit'] = round(fill, 2)
            exit_value += (1 if leg['action'] == 'BUY' else -1) * fill * leg['qty']
            cost_total += cost
        self.state['cash'] += exit_value - cost_total
        self.state['costs'] += cost_total
        pnl = exit_value - pos['net_entry'] - pos['entry_cost'] - cost_total
        self.state['trades'].append({
            'entry_date': pos['entry_date'], 'exit_date': date.strftime('%Y-%m-%d'),
            'entry_spot': pos['entry_spot'], 'exit_spot': round(spot, 2),
            'legs': pos['legs'], 'pnl': round(pnl, 2),
            'return_pct': round(pnl / pos['basis'] * 100, 2) if pos['basis'] else 0.0,
            'bars_held': self.state['bars_held'], 'reason': reason, 'tag': pos['tag'],
        })
        self._order(i, 'CLOSE', pos['legs'], reason)
        self.state['position'] = None
        self.state['bars_held'] = 0

    def _order(self, i, kind, legs, note):
        self.state['orders'].append({'date': pd.Timestamp(self.dates[i]).strftime('%Y-%m-%d'), 'kind': kind,
                                     'legs': [dict(l) for l in legs], 'note': note})

    # ---- main loop ---------------------------------------------------------
    def _risk_exit(self, i, value):
        pos = self.state['position']
        if not pos:
            return None
        unrealized = value - pos['net_entry']
        basis = pos['basis'] or 1
        r = self.risk
        if r.stop_loss_pct and unrealized <= -basis * r.stop_loss_pct / 100:
            return f'stop loss {r.stop_loss_pct}%'
        if r.target_pct and unrealized >= basis * r.target_pct / 100:
            return f'target {r.target_pct}%'
        if r.max_hold_bars and self.state['bars_held'] >= r.max_hold_bars:
            return f'max hold {r.max_hold_bars} bars'
        if r.exit_before_expiry_days:
            date = pd.Timestamp(self.dates[i])
            if min((pd.Timestamp(l['expiry']) - date).days for l in pos['legs']) <= r.exit_before_expiry_days:
                return 'pre-expiry exit'
        return None

    def step(self, i):
        st = self.state
        row = self.records[i]
        # 1) fill pending intents at this bar's open
        pending, st['pending'] = st['pending'], []
        for intent in pending:
            if intent['kind'] == 'close' and st['position']:
                self._close(i, row['open'], intent['reason'])
            elif intent['kind'] == 'open' and not st['position']:
                self._open(intent, i, row['open'])
        # 2) settle expired legs at close
        date = pd.Timestamp(self.dates[i])
        if st['position'] and any(pd.Timestamp(l['expiry']) <= date for l in st['position']['legs']):
            self._close(i, row['close'], 'expiry settlement', settle=True)
        # 3) mark to market
        value = self._position_value(row['close'], date, self._sigma(i))
        if st['position']:
            st['bars_held'] += 1
        st['equity'].append({'date': date.strftime('%Y-%m-%d'), 'equity': round(st['cash'] + value, 2),
                             'spot': round(row['close'], 2), 'in_market': st['position'] is not None})
        # 4) risk exits queue for next open
        reason = self._risk_exit(i, value)
        if reason:
            st['pending'].append({'kind': 'close', 'reason': reason})
            st['last_i'] = i
            return []
        # 5) strategy decision on closed bar
        ctx = Context(i=i, date=date, bar=row, prev=self.records[i - 1] if i > 0 else {},
                      position=st['position'], bars_held=st['bars_held'], params=self.strategy.params)
        self.strategy.on_bar(ctx)
        for intent in ctx._intents:
            if intent['kind'] == 'open' and st['position'] is None and not any(p['kind'] == 'open' for p in st['pending']):
                st['pending'].append(intent)
            elif intent['kind'] == 'close' and st['position'] is not None:
                st['pending'].append(intent)
        st['last_i'] = i
        return ctx._logs

    def run(self, start=0, end=None):
        end = len(self.records) if end is None else end
        self.strategy.on_start(None)
        for i in range(start, end):
            self.step(i)
        return self.state


def metrics(state, capital):
    eq = pd.Series([e['equity'] for e in state['equity']],
                   index=pd.to_datetime([e['date'] for e in state['equity']]))
    trades = state['trades']
    out = {'start_capital': capital, 'end_equity': round(float(eq.iloc[-1]), 2) if len(eq) else capital,
           'trades': len(trades), 'total_costs': round(state['costs'], 2), 'rejected_orders': state['rejections']}
    if len(eq) < 2:
        return out
    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365)
    out['total_return_pct'] = round((eq.iloc[-1] / capital - 1) * 100, 2)
    out['cagr_pct'] = round(((eq.iloc[-1] / capital) ** (1 / years) - 1) * 100, 2) if eq.iloc[-1] > 0 else -100.0
    dd = eq / eq.cummax() - 1
    out['max_drawdown_pct'] = round(float(dd.min()) * 100, 2)
    out['sharpe'] = round(float(rets.mean() / rets.std() * np.sqrt(252)), 2) if rets.std() > 0 else 0.0
    out['exposure_pct'] = round(sum(e['in_market'] for e in state['equity']) / len(state['equity']) * 100, 1)
    if trades:
        pnls = np.array([t['pnl'] for t in trades])
        wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
        out['win_rate_pct'] = round(len(wins) / len(pnls) * 100, 1)
        out['avg_trade_pnl'] = round(float(pnls.mean()), 2)
        out['best_trade'] = round(float(pnls.max()), 2)
        out['worst_trade'] = round(float(pnls.min()), 2)
        out['profit_factor'] = round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None
        out['avg_bars_held'] = round(float(np.mean([t['bars_held'] for t in trades])), 1)
    # Benchmark: buy & hold underlying over the same window
    spot = [e['spot'] for e in state['equity']]
    out['underlying_return_pct'] = round((spot[-1] / spot[0] - 1) * 100, 2)
    return out
