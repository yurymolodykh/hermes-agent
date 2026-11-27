"""End to end: the real store, the real owner handler, and a worker whose close-time write is refused.

The unit tests above use stubs on both sides. This one wires the real ``RuntimeSessionStore`` to the
real ``hermes_state_runtime`` receipt handler, so the chain that actually failed on the live host —
worker emits ``prompt_cache_clock`` → owner refuses ``usage.main`` → ``self.failure`` sticks →
``flush_token_counts`` raises at close → the card's result is dropped — is exercised as a whole.
"""
import json
import pathlib

import pytest

from agent.runtime_session_store import RuntimeSessionStore
from hermes_state import SessionDB
import hermes_state_runtime as rt


class OwnerRPC:
    """A ``WorkerRPC`` stand-in that runs the real owner-side receipt transaction."""

    def __init__(self, db, scope):
        self.db, self.scope, self.calls = db, scope, []

    def __call__(self, method, **params):
        self.calls.append((method, sorted(params)))
        if method != 'worker.persist':
            return {'owner_epoch': self.scope['epoch']}
        return rt.mutate_worker_execution(
            self.db, **self.scope, sequence=params['sequence'],
            operation=params['operation'], payload=params['payload'])


def _owner(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cli')
    scope = dict(epoch=rt.begin_runtime_epoch(db, instance_id='owner'), execution_id='w',
                 session_id='s', generation=0)
    rt.register_worker_execution(db, **scope, kind='compute', adoption_secret='private')
    return db, scope


def _store(tmp_path, db, scope):
    (tmp_path / 'logs').mkdir(exist_ok=True)
    return RuntimeSessionStore(OwnerRPC(db, scope),
                               {'session_id': 's', 'profile_id': str(tmp_path), **scope},
                               tmp_path / 'outbox')


def _records(tmp_path):
    path = tmp_path / 'logs' / 'persist-failures.log'
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_real_receipt_refused_by_the_real_owner_names_itself_and_keeps_the_string(tmp_path):
    db, scope = _owner(tmp_path)
    store = _store(tmp_path, db, scope)
    try:
        # The owner refuses a usage field it does not know (this schema's own allow-list).
        with pytest.raises(rt.RuntimeStoreError, match='invalid_params'):
            store.queue_token_counts('s', input_tokens=13, output_tokens=4, not_a_usage_field=1)
        assert store.failure == 'invalid_params'
        first = _records(tmp_path)[0]
        assert first['operation'] == 'usage.main'
        assert 'not_a_usage_field' in first['param_keys']
        assert first['error_type'] == 'RuntimeStoreError'
        # The close-time surface still raises the compatible string, and logs its own record.
        with pytest.raises(Exception, match='invalid_params'):
            store.flush_token_counts()
        assert any(r.get('surface') == 'flush_token_counts' for r in _records(tmp_path))
    finally:
        store._outbox_owner.close()
        db.close()


def test_the_cache_clock_field_the_newer_worker_sends_is_accepted_end_to_end(tmp_path):
    """The exact payload a newer worker sends now persists, and the store closes clean."""
    db, scope = _owner(tmp_path)
    store = _store(tmp_path, db, scope)
    try:
        store.queue_token_counts('s', input_tokens=20348, output_tokens=98, api_call_count=1,
                                 model='model-a', task='turn',
                                 prompt_cache_clock=json.dumps({'tier': '30m', 'read_tokens': 7}))
        assert store.flush_token_counts() is True
        assert store.failure is None
        row = db.get_session('s')
        assert (row['input_tokens'], row['output_tokens']) == (20348, 98)
        assert not list(pathlib.Path(tmp_path, 'logs').glob('persist-failures.log')), \
            'an accepted receipt must leave no failure record'
    finally:
        store._outbox_owner.close()
        db.close()
