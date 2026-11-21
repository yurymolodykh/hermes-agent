"""Execution-scoped worker operations on the ordinary authenticated authority.

Only explicit local compute registrations are enabled here. A delegated child is reserved by
the owner on its parent's receipt stream (gateway/session_worker_child.py), never registered.
"""
import asyncio
import hmac
import os

from gateway.session_admission import admission_fingerprint
from hermes_state_runtime import (
    RuntimeStoreError, _secret_digest, adopt_worker_execution,
    mutate_worker_execution, register_worker_execution,
)

_SCOPE = {'profile_id', 'session_id', 'execution_id', 'generation', 'pid', 'birth', 'secret'}


def _claim(connection, ref, params, *, db=None):
    """Resolve the producer claim.

    The two durable reads it needs (``worker_claim_origin``) must never run on the owner's event
    loop: on a large ``state.db`` one such read costs ~23 ms against ~0.04 ms on a small one
    (worker_receipts 94 944 vs 2 768 rows), and ``worker_request`` is an async handler. Under 7
    concurrent workers each ``worker.persist`` therefore blocked the loop for tens of ms of SQL,
    which is what starved the WS heartbeat/response writes (``loop stalled``) and pushed the 20 s
    receipt budget of *other* workers over the edge (``worker_receipt_timeout``). Callers pass
    ``db`` to have the read run on the worker thread that will do the mutation anyway.
    """
    import psutil
    authority = connection.authority
    if params.get('profile_id') != authority.profile_id or connection.actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if not connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    pid, birth = params.get('pid'), params.get('birth')
    secret = params.get('secret')
    if (type(pid) is not int or pid <= 0 or pid == os.getpid()
            or type(birth) not in (int, float) or not isinstance(secret, str) or not 16 <= len(secret) <= 256):
        raise RuntimeStoreError('invalid_params')
    try:
        process = psutil.Process(pid)
        from hermes_cli.process_identity import worker_birth
        if (worker_birth(process) != birth or not process.is_running()
                or process.status() == psutil.STATUS_ZOMBIE
                or process.username() != psutil.Process().username()):
            raise RuntimeStoreError('permission_denied')
    except psutil.Error as exc:
        raise RuntimeStoreError('worker_not_live') from exc
    # Reuse the durable adoption digest as the producer claim. Changing any
    # assignment, principal, profile, PID birth or secret cannot adopt it.
    from hermes_state_worker_compression import worker_claim_origin
    origin = worker_claim_origin(authority.db if db is None else db, params)
    return admission_fingerprint(canonical_target=origin, payload={
        key: params[key] for key in _SCOPE} | {'session_id': origin, 'principal': connection.actor.subject})


def _verify(connection, ref, params, claim, *, db=None):
    from hermes_state_worker_compression import worker_retry_target
    authority = connection.authority
    db = authority.db if db is None else db
    target = worker_retry_target(db, params)
    with db._read_ctx() as conn:
        row = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?',
                           (params['execution_id'],)).fetchone()
        if row is None:
            raise RuntimeStoreError('not_found')
        if row['session_id'] != target:
            raise RuntimeStoreError('permission_denied')
        if type(params['generation']) is not int or row['generation'] != params['generation']:
            raise RuntimeStoreError('stale_generation')
        if not hmac.compare_digest(row['adoption_digest'], _secret_digest(claim)):
            raise RuntimeStoreError('permission_denied')


def _authorize(db, connection, ref, params, operation):
    """``claim`` + ``_verify`` on the mutation's worker thread, never on the owner's event loop.

    Every durable read this needs is tens of ms on a large ``state.db`` (see ``_claim``); the
    handler is ``async``, so doing them inline put that SQL straight on the loop that also has to
    answer WS heartbeats and write every other worker's receipt inside a 20 s budget.
    """
    claim = _claim(connection, ref, params, db=db)
    if operation != 'register':
        _verify(connection, ref, params, claim, db=db)
    return claim


async def worker_request(connection, ref, params, *, operation):
    from gateway.session_authorities import require_serving
    require_serving(connection.authority)  # adopt/persist would write with the released epoch
    if operation == "persist" and isinstance(params.get("operation"), str) and params["operation"].startswith("delegation."):
        from gateway.session_worker_delegation import worker_delegation_request
        return await worker_delegation_request(connection, ref, params)
    if operation == "persist" and params.get("operation") == "child.reserve":
        from gateway.session_worker_child import worker_child_request
        return await worker_child_request(connection, ref, params)
    extra = {'register': {'kind'}, 'adopt': set(), 'persist': {'epoch', 'sequence', 'operation', 'payload'}}[operation]
    if set(params) != _SCOPE | extra:
        raise RuntimeStoreError('invalid_params')
    authority = connection.authority
    if operation == 'register':
        authority._require_admission_open()
    scope = {key: params[key] for key in ('execution_id', 'session_id', 'generation')}
    if operation == 'register':
        # Only a worker-adoption ticket (what a launched worker carries): an interactive client
        # must not park a 'registered' row that nothing reaps (the FIFO would pause until restart).
        authority.authorize(connection.actor, ref, 'worker:adopt')
        if params['kind'] != 'compute':
            raise RuntimeStoreError('unsupported_producer')

        def register():
            claim = _authorize(authority.db, connection, ref, params, 'register')
            return register_worker_execution(authority.db, epoch=authority.epoch, **scope,
                                             kind='compute', adoption_secret=claim, require_idle=True)
        return await asyncio.to_thread(register)
    if 'worker:adopt' not in connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    if operation == 'adopt':
        def adopt():
            claim = _authorize(authority.db, connection, ref, params, 'adopt')
            return adopt_worker_execution(authority.db, epoch=authority.epoch, **scope,
                                          adoption_secret=claim)
        return await asyncio.to_thread(adopt)

    def persist():
        _authorize(authority.db, connection, ref, params, 'persist')
        return mutate_worker_execution(authority.db, epoch=params['epoch'],
            **scope, sequence=params['sequence'], operation=params['operation'], payload=params['payload'])
    result = await asyncio.to_thread(persist)
    if params['operation'] == 'execution.finish':
        # Queued input parked behind this worker; nothing else would restart its FIFO.
        authority.wake_after_worker(params['session_id'])
    return result
