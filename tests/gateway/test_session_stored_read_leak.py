"""A stored history binding never hands a row that names its own owner to a second person.

The socket read path (``gateway/session_stored_reads._readable_target``) falls back to
``gateway/session_listing.legacy_readable`` -> ``hermes_state_mutation_binding.authorize_history``
when ``session.resume``'s own check answers ``not_found``. ``authorize_history`` used to answer
``True`` as soon as a stored ``gateway.history_control.v1.<sid>`` binding named the caller, which
made the binding a pass for the whole file: a row stamped ``user_id='uid:1001'`` (the host owner)
became readable by ``person/<employee>`` whose binding had stuck to it. The binding waives only
the "ownership unstated" requirement; the ownership check runs first, always.

Also pinned: a routed row (``session_key``/``chat_id``/``origin_json``) never becomes history, so
``legacy_readable`` refuses it under any binding.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB

CAPS = frozenset({'session:read', 'session:create'})
HOST_OWNER = 'uid:1001'
EMPLOYEE = 'person/maria.safronova'


def _actor(subject):
    from gateway.session_contract import SessionActor
    return SessionActor(subject, '/home/hermes/.hermes', CAPS, 'desktop')


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with SessionDB(tmp_path / 'state.db') as db:
        yield SimpleNamespace(db=db, profile_id='/home/hermes/.hermes')


def _bind(store, sid, subject):
    """The binding an earlier ``claim``/import stored: ``[profile_id, subject]``."""
    from hermes_state_mutation_binding import BINDING_PREFIX
    store.db._execute_write(lambda conn: conn.execute(
        'INSERT INTO state_meta(key,value) VALUES(?,?)',
        (BINDING_PREFIX + sid, json.dumps([store.profile_id, subject], separators=(',', ':')))))


def _authorize(store, actor, sid):
    from hermes_state_mutation_binding import authorize_history
    with store.db._read_ctx() as conn:
        return authorize_history(conn, actor, sid)


def test_a_host_owner_row_bound_to_an_employee_is_refused(store):
    """The decisive case: ``user_id='uid:1001'`` + binding naming the employee."""
    from hermes_state_runtime import RuntimeStoreError

    store.db.create_session('local-owned-by-owner', source='desktop', user_id=HOST_OWNER)
    _bind(store, 'local-owned-by-owner', EMPLOYEE)
    with pytest.raises(RuntimeStoreError) as denied:
        _authorize(store, _actor(EMPLOYEE), 'local-owned-by-owner')
    assert denied.value.reason == 'permission_denied'


def test_legacy_readable_refuses_the_same_row_under_its_own_binding(store):
    """The socket fallback the leak came through answers False, not a transcript."""
    from gateway.session_listing import legacy_readable

    store.db.create_session('local-owned-by-owner', source='desktop', user_id=HOST_OWNER)
    _bind(store, 'local-owned-by-owner', EMPLOYEE)
    authority = SimpleNamespace(db=store.db, profile_id=store.profile_id,
                                _native_legacy_transports={})
    actor = _actor(EMPLOYEE)
    authority._native_legacy_transports[actor.transport_id] = actor
    assert legacy_readable(authority, actor, 'local-owned-by-owner') is False


def test_the_owners_own_row_still_reads(store):
    """Regression: a row the actor itself owns keeps working, binding or not."""
    store.db.create_session('local-owners-own', source='desktop', user_id=HOST_OWNER)
    _bind(store, 'local-owners-own', HOST_OWNER)
    actor = _actor(HOST_OWNER)
    assert _authorize(store, actor, 'local-owners-own') is True


def test_an_unowned_row_reads_by_binding_alone(store):
    """A ``nous:*`` row names no owner, so the binding is what carries it."""
    store.db.create_session('local-unowned', source='desktop', user_id='nous:acct')
    _bind(store, 'local-unowned', EMPLOYEE)
    assert _authorize(store, _actor(EMPLOYEE), 'local-unowned') is True


def test_a_binding_that_names_another_person_is_refused(store):
    """The check the binding itself carries is unchanged."""
    from hermes_state_runtime import RuntimeStoreError

    store.db.create_session('local-unowned', source='desktop', user_id='nous:acct')
    _bind(store, 'local-unowned', EMPLOYEE)
    with pytest.raises(RuntimeStoreError) as denied:
        _authorize(store, _actor('person/someone-else'), 'local-unowned')
    assert denied.value.reason == 'permission_denied'


@pytest.mark.parametrize('route', [
    {'session_key': 'agent:01-orchestrator:telegram:dm:543447'},
    {'chat_id': '543447681'},
    {'origin_json': json.dumps({'platform': 'local', 'chat_id': 'x'})},
])
def test_a_routed_row_never_passes_on_a_binding(store, route):
    """A routed row goes its own way (route + profile), never through legacy history."""
    from gateway.session_listing import legacy_readable

    store.db.create_session('local-routed', source='desktop', user_id=HOST_OWNER, **route)
    _bind(store, 'local-routed', HOST_OWNER)
    actor = _actor(HOST_OWNER)
    authority = SimpleNamespace(db=store.db, profile_id=store.profile_id,
                                _native_legacy_transports={})
    authority._native_legacy_transports[actor.transport_id] = actor
    assert _authorize(store, actor, 'local-routed') is False
    assert legacy_readable(authority, actor, 'local-routed') is False
