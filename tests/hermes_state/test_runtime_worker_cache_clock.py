"""A worker's usage receipt carries advisory cache-clock metadata written by a newer runtime.

``agent/turn_usage.cache_clock_column`` adds ``prompt_cache_clock`` to the usage batch. An owner
running the same schema accepts it; one whose handler knows only the older field set refused the
whole receipt with ``invalid_params``, which is sticky for the rest of the turn: the worker then
failed at ``flush_token_counts`` on close and the card's result was lost (t_7403b0fe).
"""
import json

import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt


def _scope(db):
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    scope = dict(epoch=epoch, execution_id='w', session_id='s', generation=0)
    rt.register_worker_execution(db, **scope, kind='compute', adoption_secret='private')
    return scope


def test_usage_receipt_accepts_the_cache_clock_field(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', source='cli')
        scope = _scope(db)
        clock = json.dumps({'tier': '30m', 'read_tokens': 7, 'guaranteed_until': 1795653819.0})
        payload = {'input_tokens': 13, 'output_tokens': 7, 'api_call_count': 1,
                   'model': 'model-a', 'billing_provider': 'provider', 'prompt_cache_clock': clock}
        rt.mutate_worker_execution(db, **scope, sequence=1, operation='usage.main', payload=payload)
        row = db.get_session('s')
        assert (row['input_tokens'], row['output_tokens']) == (13, 7)
    finally:
        db.close()


def test_usage_receipt_still_refuses_a_clock_that_is_not_a_bounded_string(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', source='cli')
        scope = _scope(db)
        for bad in ({'nested': True}, 42, 'x' * 5000):
            with pytest.raises(rt.RuntimeStoreError, match='invalid_params'):
                rt.mutate_worker_execution(db, **scope, sequence=1, operation='usage.main',
                                           payload={'input_tokens': 1, 'prompt_cache_clock': bad})
    finally:
        db.close()
