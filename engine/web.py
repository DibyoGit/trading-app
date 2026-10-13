"""Flask blueprint: Strategy Studio page + /api/v2 JSON API."""
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session

from . import data, indicators, plugins, service, store
from .ai import models as ai_models
from .ai.models import ModelError
from .rules import RuleError

bp = Blueprint('studio', __name__)
CONFIG = {}


def init_app(app, config):
    CONFIG.update(config)
    store.init()
    app.register_blueprint(bp)


def _uid():
    return session.get('user_id')


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _uid():
            return jsonify({'success': False, 'error': 'Not logged in'}), 401
        return fn(*a, **kw)
    return wrapper


def consent_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _uid():
            return jsonify({'success': False, 'error': 'Not logged in'}), 401
        if not store.has_consent(_uid()):
            return jsonify({'success': False, 'error': 'Accept the platform terms first'}), 403
        return fn(*a, **kw)
    return wrapper


def _err(msg, code=400):
    return jsonify({'success': False, 'error': str(msg)}), code


def _body():
    return request.get_json(silent=True) or {}


@bp.route('/studio')
def studio():
    if not _uid():
        return redirect('/')
    return render_template('studio.html')


@bp.route('/api/v2/meta')
@login_required
def meta():
    return jsonify({
        'indicators': indicators.CATALOG,
        'targets': ai_models.TARGETS,
        'model_types': ai_models.MODEL_TYPES,
        'default_features': ai_models.DEFAULT_FEATURES,
        'underlyings': {k: data.underlying_meta(k, CONFIG) for k in data.UNDERLYINGS},
        'plugins': plugins.describe(),
        'consent': store.has_consent(_uid()),
        'disclaimer_version': store.DISCLAIMER_VERSION,
        'has_api_key': store.has_api_key(_uid()),
        'username': session.get('username'),
    })


@bp.route('/api/v2/consent', methods=['POST'])
@login_required
def consent():
    if not _body().get('accept'):
        return _err('consent not given')
    if not store.has_consent(_uid()):
        store.record_consent(_uid())
    return jsonify({'success': True})


# ---- strategies --------------------------------------------------------------
@bp.route('/api/v2/strategies', methods=['GET'])
@login_required
def list_strategies():
    return jsonify(store.list_strategies(_uid()))


@bp.route('/api/v2/strategies/validate', methods=['POST'])
@login_required
def validate_strategy():
    b = _body()
    try:
        spec = service.normalise_strategy(b.get('kind', 'rules'), b.get('spec') or {})
    except RuleError as exc:
        return _err(exc)
    return jsonify({'success': True, 'spec': spec})


@bp.route('/api/v2/strategies', methods=['POST'])
@bp.route('/api/v2/strategies/<int:sid>', methods=['PUT'])
@consent_required
def save_strategy(sid=None):
    b = _body()
    kind = b.get('kind', 'rules')
    try:
        spec = service.normalise_strategy(kind, b.get('spec') or {})
    except RuleError as exc:
        return _err(exc)
    sid = store.save_strategy(_uid(), spec['name'], kind, spec, sid)
    if sid is None:
        return _err('strategy not found', 404)
    return jsonify({'success': True, 'id': sid, 'spec': spec})


@bp.route('/api/v2/strategies/<int:sid>', methods=['DELETE'])
@consent_required
def delete_strategy(sid):
    store.delete_strategy(_uid(), sid)
    return jsonify({'success': True})


@bp.route('/api/v2/strategies/<int:sid>/backtest', methods=['POST'])
@consent_required
def backtest(sid):
    row = store.get_strategy(_uid(), sid)
    if not row:
        return _err('strategy not found', 404)
    if row['kind'] == 'external':
        return _err('external strategies run on your own infrastructure; backtest them there')
    try:
        result = service.run_backtest(_uid(), row, _body(), CONFIG)
    except (RuleError, ModelError, ValueError) as exc:
        return _err(exc)
    return jsonify({'success': True, **result})


@bp.route('/api/v2/backtests')
@login_required
def list_backtests():
    return jsonify(store.list_backtests(_uid()))


@bp.route('/api/v2/backtests/<int:bid>')
@login_required
def get_backtest(bid):
    bt = store.get_backtest(_uid(), bid)
    return jsonify(bt) if bt else _err('not found', 404)


# ---- AI models ----------------------------------------------------------------
@bp.route('/api/v2/models', methods=['GET'])
@login_required
def list_models():
    return jsonify(store.list_models(_uid()))


@bp.route('/api/v2/models', methods=['POST'])
@consent_required
def train_model():
    try:
        result = service.train_model(_uid(), _body())
    except (ModelError, ValueError) as exc:
        return _err(exc)
    except ImportError:
        return _err('scikit-learn is not installed on the server', 500)
    return jsonify({'success': True, **result})


@bp.route('/api/v2/models/<name>', methods=['DELETE'])
@consent_required
def delete_model(name):
    ai_models.delete(_uid(), name)
    store.delete_model(_uid(), name)
    return jsonify({'success': True})


@bp.route('/api/v2/ai/sentiment', methods=['POST'])
@consent_required
def sentiment():
    from .ai.sentiment import SentimentUnavailable, analyse
    texts = _body().get('texts') or []
    if not isinstance(texts, list):
        return _err('texts must be a list of strings')
    try:
        return jsonify({'success': True, **analyse(texts)})
    except SentimentUnavailable as exc:
        return _err(exc, 503)


@bp.route('/api/v2/ai/translate', methods=['POST'])
@consent_required
def translate():
    from .ai.assistant import AssistantUnavailable, describe_to_spec
    try:
        spec = describe_to_spec(_body().get('text'))
    except ValueError as exc:
        return _err(exc)
    except AssistantUnavailable as exc:
        return _err(exc, 503)
    store.audit(_uid(), 'assistant.translate', {'text': _body().get('text'), 'spec': spec})
    return jsonify({'success': True, 'spec': spec})


# ---- deployments --------------------------------------------------------------
@bp.route('/api/v2/deployments', methods=['GET'])
@login_required
def list_deployments():
    return jsonify([service.deployment_view(d) for d in store.list_deployments(_uid())])


@bp.route('/api/v2/deployments', methods=['POST'])
@consent_required
def create_deployment():
    b = _body()
    row = store.get_strategy(_uid(), int(b.get('strategy_id', 0)))
    if not row:
        return _err('strategy not found', 404)
    if not b.get('confirm'):
        return _err('confirm that this strategy is your own and you take responsibility for its decisions')
    try:
        did = service.deploy(_uid(), row, b.get('settings') or {}, CONFIG)
    except (RuleError, ModelError, ValueError) as exc:
        return _err(exc)
    return jsonify({'success': True, 'id': did})


@bp.route('/api/v2/deployments/<int:did>/tick', methods=['POST'])
@consent_required
def tick(did):
    dep = store.get_deployment(_uid(), did)
    if not dep:
        return _err('not found', 404)
    try:
        return jsonify({'success': True, **service.tick_deployment(dep, CONFIG)})
    except (RuleError, ModelError, ValueError) as exc:
        return _err(exc)


@bp.route('/api/v2/deployments/<int:did>/stop', methods=['POST'])
@consent_required
def stop(did):
    dep = store.get_deployment(_uid(), did)
    if not dep:
        return _err('not found', 404)
    store.update_deployment(did, status='stopped')
    store.audit(_uid(), 'deployment.stopped', {'id': did})
    return jsonify({'success': True})


@bp.route('/api/v2/api-key', methods=['POST'])
@consent_required
def rotate_key():
    return jsonify({'success': True, 'api_key': store.rotate_api_key(_uid()),
                    'note': 'Shown once. Store it securely; rotating invalidates the old key.'})


@bp.route('/api/v2/webhook', methods=['POST'])
def webhook():
    """Intent endpoint for strategies the user runs on their own machines."""
    uid = store.user_for_key(request.headers.get('X-API-Key', ''))
    if not uid:
        return _err('invalid API key', 401)
    try:
        return jsonify({'success': True, **service.submit_external_intent(uid, _body())})
    except (RuleError, ValueError) as exc:
        return _err(exc)


@bp.route('/api/v2/audit')
@login_required
def audit():
    return jsonify(store.list_audit(_uid()))
