"""User-trained predictive models with leakage-safe walk-forward validation.

Design choices for accuracy *and* sustainability:
  * small CPU models (logistic regression, random forest, gradient boosting) —
    on tabular daily data they match or beat deep nets at a tiny energy cost
  * expanding-window walk-forward with an embargo gap equal to the label
    horizon, so no training label overlaps a test bar (prevents the classic
    look-ahead leak that inflates backtest accuracy)
  * every metric is reported next to a naive baseline; "edge" is what matters
  * retrain cadence is configurable (default monthly) instead of every bar
"""
import json
import os
import re
import time

import numpy as np
import pandas as pd

from ..indicators import compute

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'data', 'models')
NAME_RE = re.compile(r'^[a-z][a-z0-9_]{1,30}$')

DEFAULT_FEATURES = ['ret_1', 'ret_5', 'ret_20', 'rsi_14', 'hv_10', 'hv_30', 'zscore_20', 'bbw_20', 'dow']

TARGETS = {
    'direction': 'Underlying closes higher after `horizon` bars',
    'big_move': 'Absolute move over `horizon` bars exceeds `threshold_pct` (either direction)',
    'vol_expansion': 'Realised vol over next `horizon` bars exceeds current hv_20',
}

MODEL_TYPES = {
    'logreg': 'Logistic regression (fast, interpretable)',
    'rf': 'Random forest (200 shallow trees)',
    'hgb': 'Histogram gradient boosting (strong tabular baseline)',
}


class ModelError(ValueError):
    pass


def _make_estimator(kind, seed=42):
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if kind == 'logreg':
        return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500))
    if kind == 'rf':
        return RandomForestClassifier(n_estimators=200, max_depth=5, min_samples_leaf=20,
                                      n_jobs=1, random_state=seed)
    if kind == 'hgb':
        return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200,
                                              l2_regularization=1.0, random_state=seed)
    raise ModelError(f'unknown model type {kind}')


def validate_config(cfg):
    name = str(cfg.get('name', '')).strip().lower()
    if not NAME_RE.match(name):
        raise ModelError('model name: 2-31 chars, lowercase letters, digits, underscore, start with a letter')
    features = cfg.get('features') or DEFAULT_FEATURES
    if not 1 <= len(features) <= 30:
        raise ModelError('choose 1-30 features')
    probe = pd.DataFrame({c: np.arange(1.0, 4.0) for c in ('open', 'high', 'low', 'close', 'volume')},
                         index=pd.bdate_range('2024-01-01', periods=3))
    for f in features:
        try:
            compute(probe, f)
        except KeyError:
            raise ModelError(f'unknown feature "{f}"') from None
    target = cfg.get('target', 'direction')
    if target not in TARGETS:
        raise ModelError(f'target must be one of {list(TARGETS)}')
    kind = cfg.get('model_type', 'logreg')
    if kind not in MODEL_TYPES:
        raise ModelError(f'model_type must be one of {list(MODEL_TYPES)}')
    horizon = int(cfg.get('horizon', 5))
    if not 1 <= horizon <= 30:
        raise ModelError('horizon must be 1-30 bars')
    return {
        'name': name, 'features': list(features), 'target': target, 'model_type': kind,
        'horizon': horizon, 'threshold_pct': float(cfg.get('threshold_pct', 2.0)),
        'retrain_every': max(5, int(cfg.get('retrain_every', 21))),
        'min_train': max(150, int(cfg.get('min_train', 300))),
        'underlying': str(cfg.get('underlying', 'NIFTY')).upper(),
    }


def build_dataset(df, cfg):
    X = pd.DataFrame({f: compute(df, f) for f in cfg['features']}, index=df.index)
    h = cfg['horizon']
    fwd = df['close'].shift(-h) / df['close'] - 1
    if cfg['target'] == 'direction':
        y = (fwd > 0).astype(float)
    elif cfg['target'] == 'big_move':
        y = (fwd.abs() * 100 > cfg['threshold_pct']).astype(float)
    else:
        logret = np.log(df['close'] / df['close'].shift(1))
        fwd_vol = logret[::-1].rolling(h).std()[::-1].shift(-1) * np.sqrt(252)
        cur = logret.rolling(20).std() * np.sqrt(252)
        y = (fwd_vol > cur).astype(float)
    y[fwd.isna()] = np.nan  # label unknown for the last h bars
    return X, y


def walk_forward(df, cfg):
    """Return (oos_proba Series aligned to df.index, metrics dict, final fitted model)."""
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, brier_score_loss, roc_auc_score

    t0 = time.perf_counter()
    X, y = build_dataset(df, cfg)
    valid = X.notna().all(axis=1)
    Xv, yv = X[valid], y[valid]
    n, h = len(Xv), cfg['horizon']
    if n < cfg['min_train'] + 60:
        raise ModelError(f'not enough data: {n} usable bars, need {cfg["min_train"] + 60}')

    oos = pd.Series(np.nan, index=df.index, name=f"model_{cfg['name']}")
    fits = 0
    start = cfg['min_train']
    while start < n:
        end = min(start + cfg['retrain_every'], n)
        train_end = start - h  # embargo: last training label must resolve before test window
        tr_y = yv.iloc[:train_end]
        mask = tr_y.notna()
        if mask.sum() >= 100 and tr_y[mask].nunique() == 2:
            model = _make_estimator(cfg['model_type'])
            model.fit(Xv.iloc[:train_end][mask].values, tr_y[mask].values)
            oos.loc[Xv.index[start:end]] = model.predict_proba(Xv.iloc[start:end].values)[:, 1]
            fits += 1
        start = end

    eval_idx = oos.dropna().index.intersection(yv.dropna().index)
    p, t = oos.loc[eval_idx].values, yv.loc[eval_idx].values
    if len(t) < 30:
        raise ModelError('too few out-of-sample points to evaluate')
    pred = (p >= 0.5).astype(float)
    train_rate = float(yv.iloc[:cfg['min_train']].mean())
    baseline_pred = np.full_like(t, 1.0 if train_rate >= 0.5 else 0.0)
    metrics = {
        'oos_samples': int(len(t)),
        'accuracy': round(float(accuracy_score(t, pred)), 4),
        'baseline_accuracy': round(float(accuracy_score(t, baseline_pred)), 4),
        'balanced_accuracy': round(float(balanced_accuracy_score(t, pred)), 4),
        'auc': round(float(roc_auc_score(t, p)), 4) if len(set(t)) == 2 else None,
        'brier': round(float(brier_score_loss(t, p)), 4),
        'positive_rate': round(float(t.mean()), 4),
        'refits': fits,
    }
    metrics['edge_vs_baseline'] = round(metrics['accuracy'] - metrics['baseline_accuracy'], 4)

    final = _make_estimator(cfg['model_type'])
    mask = yv.notna()
    final.fit(Xv[mask].values, yv[mask].values)
    recent = Xv[mask].iloc[-250:]
    metrics['importance'] = feature_importance(final, cfg['features'], recent.values, yv[mask].iloc[-250:].values)
    metrics['compute_seconds'] = round(time.perf_counter() - t0, 2)
    metrics['verdict'] = _verdict(metrics)
    return oos, metrics, final


def _verdict(m):
    auc, edge = m.get('auc') or 0.5, m['edge_vs_baseline']
    if auc >= 0.56 and edge > 0.01:
        return 'Shows out-of-sample skill. Still verify it adds value inside a strategy backtest.'
    if auc >= 0.52:
        return 'Weak signal. Likely not tradeable alone; may help as a filter.'
    return 'No reliable out-of-sample skill. Do not rely on this model.'


def feature_importance(model, features, X=None, y=None):
    est = model
    if hasattr(model, 'steps'):
        est = model.steps[-1][1]
    if hasattr(est, 'coef_'):
        vals = np.abs(est.coef_[0])
    elif hasattr(est, 'feature_importances_'):
        vals = est.feature_importances_
    elif X is not None and len(X) > 50:
        # Models without built-in importances: permutation importance on recent data
        from sklearn.inspection import permutation_importance
        res = permutation_importance(model, X, y, n_repeats=5, random_state=0, scoring='roc_auc')
        vals = np.clip(res.importances_mean, 0, None)
    else:
        return []
    total = float(vals.sum()) or 1.0
    return sorted(({'feature': f, 'importance': round(float(v) / total, 4)} for f, v in zip(features, vals)),
                  key=lambda x: -x['importance'])


def _paths(user_id, name):
    base = os.path.join(MODEL_DIR, f'user_{int(user_id)}')
    return base, os.path.join(base, f'{name}.joblib'), os.path.join(base, f'{name}_oos.csv'), \
        os.path.join(base, f'{name}.json')


def save(user_id, cfg, model, oos, metrics):
    import joblib
    base, mpath, opath, jpath = _paths(user_id, cfg['name'])
    os.makedirs(base, exist_ok=True)
    joblib.dump(model, mpath)
    oos.dropna().to_csv(opath, header=True)
    with open(jpath, 'w', encoding='utf-8') as f:
        json.dump({'config': cfg, 'metrics': metrics}, f, indent=2)


def load_oos(user_id, name):
    _, _, opath, _ = _paths(user_id, name)
    if not os.path.exists(opath):
        return None
    s = pd.read_csv(opath, index_col=0, parse_dates=True).iloc[:, 0]
    s.name = f'model_{name}'
    return s


def predict_latest(user_id, name, df):
    """Predict on the newest bars with the final model (for paper/live use)."""
    import joblib
    _, mpath, _, jpath = _paths(user_id, name)
    if not os.path.exists(mpath):
        return None
    with open(jpath, encoding='utf-8') as f:
        cfg = json.load(f)['config']
    model = joblib.load(mpath)
    X = pd.DataFrame({f: compute(df, f) for f in cfg['features']}, index=df.index).dropna()
    if X.empty:
        return None
    return pd.Series(model.predict_proba(X.values)[:, 1], index=X.index, name=f'model_{name}')


def delete(user_id, name):
    for p in _paths(user_id, name)[1:]:
        if os.path.exists(p):
            os.remove(p)


def attach_model_columns(df, user_id, names, live=False):
    """Add model_<name> columns. Backtests use stored out-of-sample predictions;
    bars after the training data use the final model (genuinely unseen data)."""
    for name in names:
        col = f'model_{name}'
        oos = load_oos(user_id, name)
        series = pd.Series(np.nan, index=df.index)
        if oos is not None:
            series.loc[series.index.intersection(oos.index)] = oos
        if live or (oos is not None and df.index[-1] > oos.index[-1]):
            latest = predict_latest(user_id, name, df)
            if latest is not None:
                after = latest.index > (oos.index[-1] if oos is not None else pd.Timestamp.min)
                series.loc[latest.index[after]] = latest[after]
        df[col] = series
    return df
