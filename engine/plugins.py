"""Discovery of Python strategy plugins from the `plugins/` folder.

Python plugins execute with full server privileges, so only the operator of a
deployment (not end users over the web) may install them. End users author
strategies through the safe rule DSL or run their own code externally and send
intents through the webhook API.
"""
import importlib.util
import inspect
import os

from .strategy_base import Strategy

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'plugins')


def discover():
    found = {}
    if not os.path.isdir(PLUGIN_DIR):
        return found
    for fname in sorted(os.listdir(PLUGIN_DIR)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        path = os.path.join(PLUGIN_DIR, fname)
        try:
            spec = importlib.util.spec_from_file_location(f'plugins.{fname[:-3]}', path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:
            print(f'[plugins] failed to load {fname}: {exc}')
            continue
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, Strategy) and cls is not Strategy and cls.__module__ == mod.__name__:
                key = f'{fname[:-3]}.{cls.__name__}'
                found[key] = cls
    return found


def describe():
    return [{'key': k, 'name': c.name, 'description': c.description, 'indicators': list(c.indicators),
             'models': list(c.models)} for k, c in discover().items()]
