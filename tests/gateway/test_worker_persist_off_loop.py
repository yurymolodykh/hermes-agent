"""The owner's event loop must never carry the durable reads a worker receipt needs.

On a large ``state.db`` ``worker_claim_origin`` alone costs ~23 ms (94 944 receipts) against
~0.04 ms on a small one (2 768). ``gateway.session_worker.worker_request`` is an async handler, so
running ``_claim``/``_verify`` inline put that SQL on the loop that also has to answer WS
heartbeats and hand every other worker its receipt inside a 20 s budget: the stalls were logged as
``ws write slow (loop stalled >10s)`` and the losses as ``worker_receipt_timeout``.
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def wired(monkeypatch):
    """A connection whose DB reads record the thread they ran on, plus stubs for the durable writes."""
    from gateway import session_worker

    seen = SimpleNamespace(claim=[], verify=[], mutate=[], adopt=[], register=[], loop_ident=None)

    def record(bucket, value=None):
        seen.__dict__[bucket].append(threading.get_ident())
        return value

    db = SimpleNamespace(profile_id='p1')
    monkeypatch.setattr(session_worker, '_claim',
                        lambda conn, ref, params, *, db=None: record(
                            'claim', ('claim', threading.get_ident())))
    monkeypatch.setattr(session_worker, '_verify',
                        lambda conn, ref, params, claim, *, db=None: record('verify'))
    monkeypatch.setattr(session_worker, 'mutate_worker_execution',
                        lambda *a, **kw: record('mutate', {'value': None}))
    monkeypatch.setattr(session_worker, 'adopt_worker_execution',
                        lambda *a, **kw: record('adopt', {'value': None}))
    monkeypatch.setattr(session_worker, 'register_worker_execution',
                        lambda *a, **kw: record('register', {'execution_id': 'e'}))
    monkeypatch.setattr(session_worker, 'require_serving', lambda auth: None, raising=False)

    authority = SimpleNamespace(profile_id='p1', epoch='ep', db=db,
                                _require_admission_open=lambda: None,
                                authorize=lambda *a: None,
                                wake_after_worker=lambda sid: None)
    connection = SimpleNamespace(authority=authority,
                                 actor=SimpleNamespace(profile_id='p1', capabilities={'worker:adopt'},
                                                       subject='user'))
    return session_worker, connection, seen


def _params(**extra):
    base = {'profile_id': 'p1', 'session_id': 's', 'execution_id': 'e', 'generation': 1,
            'pid': 2, 'birth': 3.0, 'secret': 'x' * 32}
    base.update(extra)
    return base


def _run(seen, coro):
    async def wrapped():
        seen.loop_ident = threading.get_ident()
        return await coro
    return asyncio.run(wrapped())


def test_persist_reads_run_off_the_event_loop(wired):
    """The regression itself: claim/verify/mutate on a worker thread, none on the loop."""
    session_worker, connection, seen = wired
    params = _params(epoch='ep', sequence=1, operation='transcript.append', payload={})
    _run(seen, session_worker.worker_request(connection, object(), params, operation='persist'))
    for bucket in ('claim', 'verify', 'mutate'):
        assert seen.__dict__[bucket], bucket
        assert seen.loop_ident not in seen.__dict__[bucket], \
            f'{bucket} ran on the event-loop thread: {seen.__dict__[bucket]}'
    assert seen.claim[0] is not None


def test_persist_keeps_the_authorization_before_the_write(wired):
    """Order is load-bearing: verification gates the mutation, it is not a side effect of it."""
    session_worker, connection, seen = wired
    order = []
    original_claim, original_verify = session_worker._claim, session_worker._verify
    session_worker._claim = lambda *a, **kw: order.append('claim') or original_claim(*a, **kw)
    session_worker._verify = lambda *a, **kw: order.append('verify') or original_verify(*a, **kw)
    session_worker.mutate_worker_execution = lambda *a, **kw: order.append('mutate') or {'value': None}
    params = _params(epoch='ep', sequence=1, operation='transcript.append', payload={})
    _run(seen, session_worker.worker_request(connection, object(), params, operation='persist'))
    assert order == ['claim', 'verify', 'mutate']


def test_adopt_authorizes_off_loop(wired):
    session_worker, connection, seen = wired
    params = _params()
    _run(seen, session_worker.worker_request(connection, object(), params, operation='adopt'))
    for bucket in ('claim', 'verify', 'adopt'):
        assert seen.__dict__[bucket], bucket
        assert seen.loop_ident not in seen.__dict__[bucket], bucket


def test_register_skips_the_verify_read(wired):
    """A registration has no generation/adoption digest yet: verifying it would read for nothing."""
    session_worker, connection, seen = wired
    params = _params(kind='compute')
    _run(seen, session_worker.worker_request(connection, object(), params, operation='register'))
    assert seen.claim and seen.register and not seen.verify


def test_a_refused_verification_never_reaches_the_write(wired):
    """The refusal keeps its wire reason and the transcript is untouched."""
    session_worker, connection, seen = wired

    def refuse(*a, **kw):
        from hermes_state_runtime import RuntimeStoreError
        raise RuntimeStoreError('stale_generation')

    session_worker._verify = refuse
    params = _params(epoch='ep', sequence=1, operation='transcript.append', payload={})
    with pytest.raises(Exception) as exc:
        _run(seen, session_worker.worker_request(connection, object(), params, operation='persist'))
    assert 'stale_generation' in str(exc.value)
    assert not seen.mutate


def test_an_unknown_param_is_still_refused_before_any_read(wired):
    session_worker, connection, seen = wired
    params = _params(epoch='ep', sequence=1, operation='transcript.append', payload={}, bogus=1)
    with pytest.raises(Exception) as exc:
        _run(seen, session_worker.worker_request(connection, object(), params, operation='persist'))
    assert 'invalid_params' in str(exc.value)
    assert not seen.claim
