"""A refused worker persistence call must name the operation it refused.

``RuntimeSessionStore.failure`` keeps only ``str(exc)``. An owner-side refusal is the bare
``invalid_params``, raised from many handlers, so a crash log said nothing about WHICH RPC
was rejected and the card's result was lost with no trace (t_7403b0fe). The provenance
record in ``<profile>/logs/persist-failures.log`` carries the operation and the parameter
NAMES — never values, the scope holds the worker secret.
"""
import json

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


def _store(tmp_path, rpc, **scope):
    return RuntimeSessionStore(rpc, {'session_id': 's', 'profile_id': str(tmp_path), **scope},
                               tmp_path / 'outbox')


def _records(tmp_path):
    path = tmp_path / 'logs' / 'persist-failures.log'
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_refused_operation_is_named_in_the_failure_log(tmp_path):
    def refuse(method, **params):
        raise WorkerPersistenceError('invalid_params')
    store = _store(tmp_path, refuse)
    with pytest.raises(WorkerPersistenceError, match='invalid_params'):
        store.queue_token_counts('s', input_tokens=13, prompt_cache_clock='{"tier": "30m"}')
    # The compatible string field is unchanged for existing callers.
    assert store.failure == 'invalid_params'
    records = _records(tmp_path)
    assert records, 'a refused persistence call must leave a provenance record'
    first = records[0]
    assert first['operation'] == 'usage.main'
    assert first['error'] == 'invalid_params'
    assert 'prompt_cache_clock' in first['param_keys']
    assert first['traceback'] and 'WorkerPersistenceError' in first['traceback']
    # Keys only: the payload and the scope carry the worker secret, never values here.
    assert 'secret' not in json.dumps(first)
    store._outbox_owner.close()


def test_refusal_of_a_journal_save_names_its_operation(tmp_path, monkeypatch):
    store = _store(tmp_path, lambda *a, **k: {})
    def fail_replace(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr('agent.runtime_session_store.os.replace', fail_replace)
    with pytest.raises(OSError, match='disk unavailable'):
        store.queue_token_counts('s', input_tokens=1)
    record = _records(tmp_path)[0]
    assert record['operation'] == 'usage.main'
    assert record['error_type'] == 'OSError'
    assert 'input_tokens' in record['param_keys']
    store._outbox_owner.close()


def test_close_time_flush_records_the_surface_and_stuck_operations(tmp_path):
    def refuse(method, **params):
        raise WorkerPersistenceError('invalid_params')
    store = _store(tmp_path, refuse)
    with pytest.raises(WorkerPersistenceError):
        store.queue_token_counts('s', input_tokens=1)
    with pytest.raises(WorkerPersistenceError, match='invalid_params'):
        store.flush_token_counts()
    records = _records(tmp_path)
    surfaces = [r for r in records if r.get('surface') == 'flush_token_counts']
    assert surfaces, 'the close-time re-raise must say where it surfaced'
    assert surfaces[0]['pending_operations'] == ['usage.main']
    store._outbox_owner.close()


def test_a_failure_log_that_cannot_be_written_never_masks_the_error(tmp_path):
    """A diagnostic that dies must not replace the failure it describes."""
    def refuse(method, **params):
        raise WorkerPersistenceError('invalid_params')
    store = _store(tmp_path, refuse)
    (tmp_path / 'logs').write_text('not a directory')  # the log cannot be opened
    with pytest.raises(WorkerPersistenceError, match='invalid_params'):
        store.queue_token_counts('s', input_tokens=1)
    assert store.failure == 'invalid_params'
    store._outbox_owner.close()


def test_no_profile_home_means_no_stray_log(tmp_path):
    """A store outside a profile (a test double, a bare CLI) writes no record anywhere."""
    def refuse(method, **params):
        raise WorkerPersistenceError('invalid_params')
    store = RuntimeSessionStore(refuse, {'session_id': 's'}, tmp_path / 'outbox')
    with pytest.raises(WorkerPersistenceError):
        store.queue_token_counts('s', input_tokens=1)
    assert not (tmp_path / 'logs' / 'persist-failures.log').exists()
    store._outbox_owner.close()
