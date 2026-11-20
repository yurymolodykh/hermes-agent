"""The bindings registry on the rights-check path: owner scope, fail-closed denial, tool reads.

Criterion this file exists for: a BROKEN registry must make an access check answer "deny", not
"allow", and must not shrink the answer to one stamp either (which would silently hide the owner's
own history — the 246-session failure mode the mission measured).

``hermes_acl_scope`` is the one place a read path turns a subject into an owner scope, and it now
consults ``hermes_acl_bindings``. These tests drive the resolvers directly with an explicit registry
path, so they prove the policy without a gateway process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import hermes_acl_bindings as hb
import hermes_acl_scope
from hermes_state import SessionDB

OWNER = "uid:1001"
OWNER_PERSON = "person/yury"
ALICE = "uid:2002"
ALICE_PERSON = "person/alice"


@pytest.fixture(autouse=True)
def _clean_cache():
    hb.reset_cache()
    yield
    hb.reset_cache()


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "acl" / "profile-bindings.json"


@pytest.fixture(autouse=True)
def _explicit_registry_path(monkeypatch, path):
    """EVERY test here points the module at its own tmp registry.

    Without this a test that only corrupts a file would silently keep reading the DEFAULT path
    (anchored to the Hermes root) and prove nothing — and would touch the operator's real model.
    """
    monkeypatch.setattr(hb, "bindings_path", lambda: path)
    hb.reset_cache()
    return path


@pytest.fixture
def registry(_explicit_registry_path) -> hb.BindingStore:
    """The owner's real stamps plus one second person, under the test's explicit path."""
    store = hb.BindingStore(_explicit_registry_path)
    store.bind(OWNER_PERSON, profiles=["01-orchestrator"], subjects=[OWNER, "cron-owner", "125805788"])
    store.bind(ALICE_PERSON, profiles=["01-alice"], subjects=[ALICE])
    hb.reset_cache()
    return store


@pytest.fixture
def acl_on(monkeypatch):
    """``acl.enabled`` on, identity trusted from the proxy header — the step-3 configuration."""
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_scope, "identity_trust", lambda: "trusted-proxy")
    monkeypatch.setattr(hermes_acl_scope, "identity_header", lambda: "X-Hermes-User-Id")
    return True


@pytest.fixture
def acl_off(monkeypatch):
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
    return True


class _Request:
    """Minimal duck-typed request: ``request_owner_scope`` reads only ``.headers``."""

    def __init__(self, identity: str) -> None:
        self.headers = {"X-Hermes-User-Id": identity}


def _corrupt(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. The registry WIDENS one person's scope to their own stamps, and only theirs
# ---------------------------------------------------------------------------


class TestScopeExpansion:
    def test_owner_person_resolves_to_every_legacy_stamp(self, acl_on, registry):
        scope = hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON))
        assert scope is not None
        assert set(scope) == {OWNER_PERSON, OWNER, "cron-owner", "125805788"}

    def test_a_legacy_stamp_resolves_to_the_same_scope(self, acl_on, registry):
        from_person = hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON))
        from_stamp = hermes_acl_scope.request_owner_scope(_Request(OWNER))
        assert set(from_stamp) == set(from_person)

    def test_another_person_gets_only_their_own_stamps(self, acl_on, registry):
        scope = hermes_acl_scope.request_owner_scope(_Request(ALICE_PERSON))
        assert set(scope) == {ALICE_PERSON, ALICE}
        assert OWNER not in scope and "cron-owner" not in scope

    def test_an_unknown_identity_gets_only_itself(self, acl_on, registry):
        """An identity the registry does not name must not inherit anybody's stamps."""
        assert hermes_acl_scope.request_owner_scope(_Request("tg:999")) == ["tg:999"]

    def test_no_registry_at_all_changes_nothing(self, acl_on, monkeypatch, path):
        """Additive: with no file the scope is exactly what the identity formats produce."""
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        assert hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON)) == [OWNER_PERSON]

    def test_acl_off_is_still_unscoped(self, acl_off, registry):
        assert hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON)) is None
        assert hermes_acl_scope.caller_owner_scope() is None


# ---------------------------------------------------------------------------
# 2. THE CRITERION: a broken registry DENIES
# ---------------------------------------------------------------------------


class TestBrokenRegistryDenies:
    def test_http_request_is_denied_with_an_empty_scope(self, acl_on, path):
        _corrupt(path)
        assert hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON)) == []

    def test_http_request_is_denied_even_for_a_registry_named_subject(self, acl_on, path, registry):
        """The file becomes corrupt AFTER a healthy load: the denial must not be cached away."""
        assert hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON))
        _corrupt(path)
        assert hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON)) == []

    def test_tool_read_is_denied_with_an_empty_scope(self, acl_on, path, monkeypatch):
        _corrupt(path)
        monkeypatch.setattr(hermes_acl_scope, "caller_subject", lambda: OWNER_PERSON)
        assert hermes_acl_scope.caller_owner_scope() == []

    def test_unreadable_registry_is_denied_too(self, acl_on, path, monkeypatch):
        """An I/O failure is the same class as a parse failure: refuse, never guess."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"persons": []}), encoding="utf-8")

        def _boom(*_args, **_kwargs):
            raise hb.BindingsUnavailable("simulated I/O failure")

        monkeypatch.setattr(hb.BindingStore, "read", _boom)
        assert hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON)) == []

    def test_a_broken_registry_while_acl_is_off_changes_nothing(self, acl_off, path):
        """Before enablement a corrupt file must not deny a single-user install anything."""
        _corrupt(path)
        assert hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON)) is None

    def test_the_denial_is_logged(self, acl_on, path, caplog):
        _corrupt(path)
        with caplog.at_level("WARNING"):
            hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON))
        assert any("DENYING" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# 3. The store turns the empty scope into "no rows", not "all rows"
# ---------------------------------------------------------------------------


class TestStoreDenial:
    @pytest.fixture
    def db(self, tmp_path):
        database = SessionDB(tmp_path / "state.db")
        try:
            yield database
        finally:
            close = getattr(database, "close", None)
            if callable(close):
                close()

    def _seed(self, db, session_id, owner):
        """A session ROW is all these tests need; appending messages pulls in the agent loop,
        whose bootstrap probes the real repo root (a pre-existing guard interaction unrelated to
        the registry)."""
        db.create_session(session_id, source="telegram", user_id=owner)
        db._conn.commit()

    def test_empty_scope_lists_nothing(self, db):
        for index in range(4):
            self._seed(db, f"s{index}", OWNER)
        assert len(db.list_sessions_rich(user_id=[], limit=50)) == 0

    def test_the_denied_scope_hides_the_owners_own_rows(self, acl_on, path, db):
        """End to end: corrupt registry → empty scope → the owner's rows are NOT served.
        Fails closed on the side that matters (an accidental allow is the dangerous direction)."""
        self._seed(db, "owner-1", OWNER)
        _corrupt(path)
        scope = hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON))
        assert db.list_sessions_rich(user_id=scope, limit=50) == []

    def test_a_healthy_registry_serves_the_owners_rows(self, acl_on, registry, db):
        self._seed(db, "owner-1", OWNER)
        self._seed(db, "owner-2", "cron-owner")
        self._seed(db, "alice-1", ALICE)
        scope = hermes_acl_scope.request_owner_scope(_Request(OWNER_PERSON))
        ids = {row["id"] for row in db.list_sessions_rich(user_id=scope, limit=50)}
        assert ids == {"owner-1", "owner-2"}
