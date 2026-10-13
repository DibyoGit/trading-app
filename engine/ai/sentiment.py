"""Financial-news sentiment via Hugging Face FinBERT (ProsusAI/finbert).

Backends, in order:
  1. local `transformers` pipeline (if installed) — loaded lazily once, CPU
  2. Hugging Face Inference API (if HF_TOKEN env var is set)
Results are cached by text so repeated headlines cost nothing.

The user supplies the text (their own news feed / headlines). Output is a
sentiment score they may use in their own process; it is not a trade call.
"""
import hashlib
import os
import threading

import requests

MODEL_ID = os.environ.get('SENTIMENT_MODEL', 'ProsusAI/finbert')
HF_API_URL = f'https://router.huggingface.co/hf-inference/models/{MODEL_ID}'
MAX_TEXTS = 20
MAX_CHARS = 512

_cache = {}
_pipe = None
_lock = threading.Lock()


class SentimentUnavailable(RuntimeError):
    pass


def _local_pipeline():
    global _pipe
    if _pipe is None:
        with _lock:
            if _pipe is None:
                from transformers import pipeline  # optional dependency
                _pipe = pipeline('text-classification', model=MODEL_ID, top_k=None, device=-1)
    return _pipe


def _normalise(scores):
    by_label = {s['label'].lower(): float(s['score']) for s in scores}
    pos, neg = by_label.get('positive', 0.0), by_label.get('negative', 0.0)
    label = max(by_label, key=by_label.get) if by_label else 'neutral'
    return {'label': label, 'score': round(pos - neg, 4), 'probs': {k: round(v, 4) for k, v in by_label.items()}}


def _remote(texts):
    token = os.environ.get('HF_TOKEN')
    if not token:
        raise SentimentUnavailable('install `transformers` + `torch` or set HF_TOKEN for the Hugging Face API')
    resp = requests.post(HF_API_URL, headers={'Authorization': f'Bearer {token}'},
                         json={'inputs': texts}, timeout=30)
    if resp.status_code != 200:
        raise SentimentUnavailable(f'Hugging Face API error {resp.status_code}: {resp.text[:200]}')
    out = resp.json()
    return out if isinstance(out[0], list) else [out]


def analyse(texts):
    texts = [str(t)[:MAX_CHARS] for t in texts if str(t).strip()][:MAX_TEXTS]
    if not texts:
        return {'backend': None, 'results': [], 'aggregate': 0.0}
    keys = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
    missing = [t for t, k in zip(texts, keys) if k not in _cache]
    backend = 'cache'
    if missing:
        try:
            raw = _local_pipeline()(missing)
            backend = 'local'
        except ImportError:
            raw = _remote(missing)
            backend = 'huggingface-api'
        for t, scores in zip(missing, raw):
            _cache[hashlib.sha1(t.encode()).hexdigest()] = _normalise(scores)
    results = [{'text': t, **_cache[k]} for t, k in zip(texts, keys)]
    agg = sum(r['score'] for r in results) / len(results)
    return {'backend': backend, 'model': MODEL_ID, 'results': results, 'aggregate': round(agg, 4)}
