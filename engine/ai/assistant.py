"""Plain-English -> rule-spec translator, powered by Claude (optional).

The user describes *their own* strategy; Claude only translates it into the
rule DSL so the user can review, edit and save it. The assistant is instructed
not to invent, improve or recommend strategies — authorship stays with the user.
Requires the `anthropic` package and ANTHROPIC_API_KEY (or an `ant` profile).
"""
import json
import os

MODEL = os.environ.get('ASSISTANT_MODEL', 'claude-opus-5-5')

SYSTEM = """You convert a trader's own written description of an options strategy into a JSON rule spec.

Rules for you:
- Translate only what the user wrote. Never add entry/exit conditions, legs, or risk settings the user did not state, and never suggest a strategy, an improvement, a market view, or whether to trade.
- If the description is too vague to express (for example no entry condition), set "error" to a short question asking the user for the missing detail and leave the other fields as empty strings / empty lists.
- Expressions may use only: open, high, low, close, volume, dow (0=Mon), sma_N, ema_N, rsi_N, atr_N, hv_N (annualised vol, 0.15 = 15%), ret_N (% return), zscore_N, bbw_N, model_<name> (probability from the user's own trained model), numbers, + - * /, comparisons, and/or/not, abs, min, max, crossed_above(a, b), crossed_below(a, b).
- Strike is "ATM", "ATM+k" or "ATM-k" (k = strike steps) or a number. action is BUY or SELL, type is CE or PE.
- Risk numbers are percentages of entry premium; use 0 for anything the user did not specify.
- Put any assumption you had to make in "assumptions" so the user can check it."""

SCHEMA = {
    'type': 'object',
    'properties': {
        'name': {'type': 'string'},
        'description': {'type': 'string'},
        'underlying': {'type': 'string', 'enum': ['NIFTY', 'BANKNIFTY']},
        'entry': {'type': 'string'},
        'exit': {'type': 'string'},
        'legs': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['BUY', 'SELL']},
                'type': {'type': 'string', 'enum': ['CE', 'PE']},
                'strike': {'type': 'string'},
                'lots': {'type': 'integer'},
                'expiry_days': {'type': 'integer'},
            },
            'required': ['action', 'type', 'strike', 'lots', 'expiry_days'],
            'additionalProperties': False,
        }},
        'risk': {
            'type': 'object',
            'properties': {
                'stop_loss_pct': {'type': 'number'},
                'target_pct': {'type': 'number'},
                'max_hold_bars': {'type': 'integer'},
                'exit_before_expiry_days': {'type': 'integer'},
            },
            'required': ['stop_loss_pct', 'target_pct', 'max_hold_bars', 'exit_before_expiry_days'],
            'additionalProperties': False,
        },
        'assumptions': {'type': 'array', 'items': {'type': 'string'}},
        'error': {'type': 'string'},
    },
    'required': ['name', 'description', 'underlying', 'entry', 'exit', 'legs', 'risk', 'assumptions', 'error'],
    'additionalProperties': False,
}


class AssistantUnavailable(RuntimeError):
    pass


def describe_to_spec(text):
    text = str(text or '').strip()
    if not text:
        raise ValueError('describe your strategy first')
    if len(text) > 4000:
        raise ValueError('description too long (4000 chars max)')
    try:
        import anthropic
    except ImportError:
        raise AssistantUnavailable('install the `anthropic` package to enable the strategy translator') from None

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            output_config={'effort': 'medium', 'format': {'type': 'json_schema', 'schema': SCHEMA}},
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
            messages=[{'role': 'user', 'content': text}],
        )
    except anthropic.AuthenticationError:
        raise AssistantUnavailable('set ANTHROPIC_API_KEY to enable the strategy translator') from None
    except anthropic.RateLimitError:
        raise AssistantUnavailable('translator is rate limited, try again shortly') from None
    except anthropic.APIConnectionError:
        raise AssistantUnavailable('cannot reach the Claude API') from None
    except anthropic.APIStatusError as exc:
        raise AssistantUnavailable(f'Claude API error {exc.status_code}') from None

    if response.stop_reason == 'refusal':
        raise AssistantUnavailable('the translator declined this request')
    if response.stop_reason == 'max_tokens':
        raise AssistantUnavailable('translation was cut off; shorten the description')
    raw = next((b.text for b in response.content if b.type == 'text'), '')
    return json.loads(raw)
