"""A named endpoint (``providers.<name>``) resolves inside ``route_runtime``, not through
``resolve_runtime_provider``, so the per-card key pool chokepoint has to be applied there too.

Live defect this pins (t_afdf38ac, 21.11.2026): ``providers.letovo`` is a named entry, so every
managed worker of a card took the early return and kept the launch credential — the background
key #1 — while the pool state showed the card's own reserved key. Ten cards ran on one account.
"""
import json

import pytest


BASE_URL = 'https://llm.example.invalid/openai'


class _Policy:
    def __init__(self, base_url=BASE_URL):
        self.request_json = json.dumps({'base_url': base_url})


FROZEN = {'model': {'provider': 'letovo', 'default': 'flash-model', 'base_url': BASE_URL,
                    'api_mode': 'openai'}}
NAMED_RUNTIME = {'provider': 'letovo', 'api_key': 'launch-background-key', 'base_url': BASE_URL,
                 'api_mode': 'chat_completions', 'source': 'custom_provider:letovo'}


def _patch_named(monkeypatch):
    from gateway import session_worker_construct as swc
    monkeypatch.setattr(
        'hermes_cli.runtime_provider_custom._resolve_named_custom_runtime',
        lambda **kwargs: dict(NAMED_RUNTIME))


def test_named_endpoint_takes_the_card_key(monkeypatch):
    from agent import kanban_key_pool
    from gateway.session_worker_construct import route_runtime

    _patch_named(monkeypatch)
    seen = {}

    def fake_apply(runtime):
        seen['runtime'] = dict(runtime)
        return {**runtime, 'api_key': 'reserved-card-key',
                'key_pool': {'key_id': 4, 'label': 'account-04', 'role': 'card'}}

    monkeypatch.setattr(kanban_key_pool, 'apply_card_key', fake_apply)
    runtime, fallback = route_runtime(_Policy(), FROZEN, 'flash-model', 'launch-background-key')

    assert fallback is None
    # The pool hook saw the named entry's own runtime (endpoint and source intact)...
    assert seen['runtime']['base_url'] == BASE_URL
    assert seen['runtime']['source'] == 'custom_provider:letovo'
    # ...and its answer is what the worker wires.
    assert runtime['api_key'] == 'reserved-card-key'
    assert runtime['key_pool']['key_id'] == 4
    assert runtime['base_url'] == BASE_URL
    assert runtime['api_mode'] == 'chat_completions'


def test_named_endpoint_card_without_a_slot_fails_closed(monkeypatch):
    from agent import kanban_key_pool
    from gateway.session_worker_construct import route_runtime

    _patch_named(monkeypatch)

    def refuse(runtime):
        raise kanban_key_pool.KeyPoolError('no free card key slot')

    monkeypatch.setattr(kanban_key_pool, 'apply_card_key', refuse)
    with pytest.raises(kanban_key_pool.KeyPoolError):
        route_runtime(_Policy(), FROZEN, 'flash-model', None)


def test_named_endpoint_on_another_host_keeps_its_own_credential(monkeypatch):
    """The pool owns ONE endpoint: a named entry elsewhere comes back with its own credential."""
    from agent import kanban_key_pool
    from gateway.session_worker_construct import route_runtime

    other = {'provider': 'deepseek-kb', 'api_key': 'kb-own-key',
             'base_url': 'https://kb.example.invalid/v1', 'api_mode': 'chat_completions',
             'source': 'custom_provider:deepseek-kb'}
    monkeypatch.setattr('hermes_cli.runtime_provider_custom._resolve_named_custom_runtime',
                        lambda **kwargs: dict(other))
    calls = []
    real = kanban_key_pool.apply_card_key
    monkeypatch.setattr(kanban_key_pool, 'apply_card_key',
                        lambda runtime: (calls.append(runtime), real(runtime))[1])
    frozen = {**FROZEN, 'model': {**FROZEN['model'], 'provider': 'deepseek-kb',
                                  'base_url': 'https://kb.example.invalid/v1'}}
    runtime, fallback = route_runtime(_Policy('https://kb.example.invalid/v1'), frozen,
                                      'flash-model', None)

    assert fallback is None
    assert len(calls) == 1              # the hook runs on this path...
    assert runtime['api_key'] == 'kb-own-key'   # ...and the endpoint guard leaves it alone


def test_named_endpoint_plumbing_failure_keeps_the_unpooled_runtime(monkeypatch, caplog):
    from agent import kanban_key_pool
    from gateway.session_worker_construct import route_runtime

    _patch_named(monkeypatch)

    def broken(runtime):
        raise OSError('state file unreadable')

    monkeypatch.setattr(kanban_key_pool, 'apply_card_key', broken)
    runtime, fallback = route_runtime(_Policy(), FROZEN, 'flash-model', 'launch-background-key')

    assert fallback is None
    assert runtime['api_key'] == 'launch-background-key'
    assert any('per-card key to a named endpoint' in r.message % r.args if r.args else
               'per-card key to a named endpoint' in r.message for r in caplog.records)
