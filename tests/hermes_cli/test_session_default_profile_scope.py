"""A session list with no ``?profile=`` answers from the CALLER's own store (МПД Т5).

The hole: ``GET /api/sessions`` (and every sibling route) resolved an omitted ``profile`` to the
home the backend launched with. On the shared host that is the ROOT ``state.db``, so a signed-in
person's sidebar read somebody else's store: their own conversations live in
``profiles/01-<login>/state.db`` and never appeared, and the page came back empty rather than
wrong (the row scope was doing its job on the wrong file).

The rule pinned here is ONE default, resolved in ``_session_acl.session_profile``:

* a person naming no profile gets their own orchestrator root — the same answer
  ``hermes_acl_results.resolve_profile`` already gives the results surfaces;
* a NAMED profile is untouched, including the foreign-name refusal (0 rows, never another's);
* ``acl.multi_user`` off, the host owner, and a caller owning no profile stay exactly as before.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_scope
from gateway.owner_identity import host_owner_subject
from hermes_state import SessionDB

ALICE = "person/alice"
BOB = "person/bob"
OWNER_LOGIN = "owner"
ALICE_ROOT = "01-alice"
BOB_ROOT = "01-bob"
SHARED_ROOT = "02-hermes-platform"
#: The stamp the host owner's agent writes into a shared profile's store (live: ``uid:1001``).
SHARED_OWNER_STAMP = "uid:1001"

ALICE_DOC = {
    "slug": ALICE, "display_name": "Alice", "shared": False,
    "profiles": [ALICE_ROOT], "subjects": [], "owner": ALICE, "source": "admin",
}
BOB_DOC = {
    "slug": BOB, "display_name": "Bob", "shared": False,
    "profiles": [BOB_ROOT], "subjects": [], "owner": BOB, "source": "admin",
}
SHARED_DOC = {
    "slug": "shared/service", "display_name": "Shared", "shared": True,
    "profiles": [SHARED_ROOT], "subjects": [], "owner": "admin", "source": "admin",
}


class _Session:
    """The verified dashboard login the auth gate attaches (``request.state.session``)."""

    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
    hb.reset_cache()
    yield home
    hb.reset_cache()


@pytest.fixture
def registry(_isolated_home):
    path = _isolated_home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [ALICE_DOC, BOB_DOC, SHARED_DOC],
    }), encoding="utf-8")
    hb.reset_cache()
    yield path
    hb.reset_cache()


@pytest.fixture
def multi_user(monkeypatch):
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_scope, "identity_trust", lambda: "untrusted")
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        if not login:
            return fallback
        return host_owner_subject() if login == OWNER_LOGIN else f"person/{login}"

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", _resolve)
    return True


def _profile_home(root: Path, name: str) -> Path:
    """A live named profile home: the identity marker every enumerator looks for."""
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    return home


def _seed(db_path: Path, rows) -> None:
    db = SessionDB(db_path=db_path)
    try:
        for sid, owner, title in rows:
            db.create_session(sid, source="desktop", user_id=owner)
            db._conn.execute(
                "UPDATE sessions SET started_at = ?, title = ?, model = ? WHERE id = ?",
                (int(time.time()), title, "m", sid))
            db.append_message(sid, role="user", content=f"hello from {title}")
            db._conn.commit()
            db.end_session(sid, "done")
    finally:
        db.close()


@pytest.fixture
def homes(_isolated_home, registry):
    """Root store + Alice's and Bob's own stores, each seeded with its own rows.

    Alice ALSO has rows in the root store: a row scope alone would show them, so the listing
    below proves the STORE was switched, not merely filtered. Those rows are ``person/alice``
    stamped on purpose — the defect is the wrong file, and keeping a visible row in the wrong
    file is what makes "0 sessions" impossible to mistake for a pass.
    """
    root_db = _isolated_home / "state.db"
    alice_home = _profile_home(_isolated_home, ALICE_ROOT)
    bob_home = _profile_home(_isolated_home, BOB_ROOT)
    shared_home = _profile_home(_isolated_home, SHARED_ROOT)
    _seed(root_db, [
        ("root-owner-1", host_owner_subject(), "owner chat"),
        ("root-alice-1", ALICE, "alice chat left in the root store"),
    ])
    _seed(alice_home / "state.db", [("alice-1", ALICE, "alice own chat"),
                                    ("alice-2", ALICE, "alice second chat")])
    _seed(bob_home / "state.db", [("bob-1", BOB, "bob own chat")])
    # A SHARED profile's rows are stamped with whoever happened to work in it — on the live
    # host that is the host owner's ``uid:1001``, never the employee reading the team's work.
    # That is exactly why a stamp-only filter hides a shared profile from every employee.
    _seed(shared_home / "state.db", [("shared-1", SHARED_OWNER_STAMP, "team chat"),
                                     ("shared-2", SHARED_OWNER_STAMP, "team chat two")])
    return {"root": root_db, "alice": alice_home / "state.db", "bob": bob_home / "state.db",
            "shared": shared_home / "state.db"}


def _app():
    from hermes_cli.web_routers.sessions import list_router, manage_router, search_router

    app = FastAPI()
    for router in (list_router, search_router, manage_router):
        app.include_router(router)
    return app


def _client(*, as_login=None):
    client = TestClient(_app())
    if as_login is not None:
        @client.app.middleware("http")
        async def _stamp(request, call_next):
            request.state.session = _Session(as_login)
            return await call_next(request)
    return client


def _ids(cli, path):
    body = cli.get(path).json()
    return {row["id"] for row in body["sessions"]}, body


# ────────────────────────────────────────────────────────────────────────────
# The defect: no ?profile= reads the caller's own store
# ────────────────────────────────────────────────────────────────────────────


def test_unprofiled_list_reads_the_callers_own_store(homes, multi_user, registry):
    """RED before the fix: this returned Alice's ROOT rows (or nothing at all)."""
    with _client(as_login="alice") as cli:
        ids, body = _ids(cli, "/api/sessions?limit=50")
    assert ids == {"alice-1", "alice-2"}
    assert "root-alice-1" not in ids
    assert {row["profile"] for row in body["sessions"]} == {ALICE_ROOT}


def test_unprofiled_list_never_shows_another_person(homes, multi_user, registry):
    with _client(as_login="bob") as cli:
        ids, body = _ids(cli, "/api/sessions?limit=50")
    assert ids == {"bob-1"}
    assert {row["profile"] for row in body["sessions"]} == {BOB_ROOT}


def test_unprofiled_stats_and_search_use_the_same_store(homes, multi_user, registry):
    """The other unprofiled reads must not answer from a different file than the list."""
    with _client(as_login="alice") as cli:
        assert cli.get("/api/sessions/stats").json()["total"] == 2
        hits = cli.get("/api/sessions/search?q=hello").json()["results"]
    assert {hit["session_id"] for hit in hits} == {"alice-1", "alice-2"}


def test_unprofiled_detail_opens_a_row_of_the_callers_own_store(homes, multi_user, registry):
    with _client(as_login="alice") as cli:
        assert cli.get("/api/sessions/alice-1").status_code == 200
        # A row that exists in the ROOT store but not in hers is a genuine miss, not a hit.
        assert cli.get("/api/sessions/root-alice-1").status_code == 404


# ────────────────────────────────────────────────────────────────────────────
# The boundaries that must NOT move
# ────────────────────────────────────────────────────────────────────────────


def test_named_profile_is_still_honoured(homes, multi_user, registry):
    with _client(as_login="alice") as cli:
        ids, _ = _ids(cli, f"/api/sessions?limit=50&profile={ALICE_ROOT}")
    assert ids == {"alice-1", "alice-2"}


def test_foreign_profile_still_answers_nothing(homes, multi_user, registry):
    """Negative control: the default must not become a way into somebody else's store."""
    with _client(as_login="alice") as cli:
        ids, body = _ids(cli, f"/api/sessions?limit=50&profile={BOB_ROOT}")
        assert cli.get(f"/api/sessions/bob-1?profile={BOB_ROOT}").status_code == 404
    assert ids == set()
    assert body["total"] == 0


def test_acl_off_keeps_the_launch_store(homes, monkeypatch, registry):
    """The shipped single-user default is untouched: the root store, as before."""
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    with _client() as cli:
        ids, _ = _ids(cli, "/api/sessions?limit=50")
    assert ids == {"root-owner-1", "root-alice-1"}


def test_owner_keeps_the_launch_store(homes, multi_user, registry):
    """The host owner's dashboard is unchanged — the launch store, and only its own rows.

    ``root-alice-1`` is stamped ``person/alice``, which is NOT an owner stamp even in the root
    store: the owner's row scope excluded it before this change too (verified against the
    pre-fix default), so the invariant is "same answer as before", not "every root row".
    """
    with _client(as_login=OWNER_LOGIN) as cli:
        ids, _ = _ids(cli, "/api/sessions?limit=50")
    assert ids == {"root-owner-1"}


def test_person_without_a_registry_profile_keeps_the_fail_closed_answer(homes, multi_user,
                                                                      _isolated_home):
    """No registry record → no own store → unchanged behaviour, and never a 404 for the list."""
    import hermes_acl_bindings as bindings

    path = _isolated_home / "acl" / "profile-bindings.json"
    path.write_text(json.dumps({
        "schema": bindings.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00",
        "written_by": "admin", "persons": [BOB_DOC],
    }), encoding="utf-8")
    bindings.reset_cache()
    with _client(as_login="alice") as cli:
        response = cli.get("/api/sessions?limit=50")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["sessions"]}
    # Her stamps still admit her root rows; the store is simply not redirected.
    assert ids == {"root-alice-1"}


def test_unverified_caller_is_unchanged_and_closed(homes, multi_user, registry):
    with _client() as cli:
        body = cli.get("/api/sessions?limit=50").json()
    assert body["sessions"] == []


# ────────────────────────────────────────────────────────────────────────────
# A SHARED profile's work is the team's: every verified person reads it
# ────────────────────────────────────────────────────────────────────────────
#
# The policy is "personal correspondence and personal profiles are isolated; everything the team
# produced is common". The registry already says which profiles are shared (``shared/service``),
# so row filtering must consult that fact: a shared profile's rows are stamped with whoever ran
# the turn there (the host owner's ``uid:1001`` on the live host), and a stamp-only filter
# therefore hid the whole team's work behind the owner's identity.


def test_shared_profile_list_is_readable_by_any_verified_person(homes, multi_user, registry):
    """RED before the fix: a shared profile answered 200 with 0 rows (store had 307)."""
    with _client(as_login="alice") as cli:
        ids, body = _ids(cli, f"/api/sessions?limit=50&profile={SHARED_ROOT}")
    assert ids == {"shared-1", "shared-2"}
    # Each row stays in its own profile — the listing never re-labels them into Alice's.
    assert {row["profile"] for row in body["sessions"]} == {SHARED_ROOT}
    assert body["total"] == 2


def test_shared_profile_rows_are_the_same_for_every_person(homes, multi_user, registry):
    """Team material is one answer, not one answer per viewer."""
    with _client(as_login="bob") as cli:
        bob_ids, _ = _ids(cli, f"/api/sessions?limit=50&profile={SHARED_ROOT}")
    assert bob_ids == {"shared-1", "shared-2"}


def test_shared_profile_row_opens_by_id(homes, multi_user, registry):
    with _client(as_login="alice") as cli:
        assert cli.get(f"/api/sessions/shared-1?profile={SHARED_ROOT}").status_code == 200
        assert cli.get(f"/api/sessions/shared-1/messages?profile={SHARED_ROOT}").status_code == 200


def test_shared_profile_list_does_not_leak_personal_rows(homes, multi_user, registry):
    """Opening the shared store must not become a way into anybody's personal profile."""
    with _client(as_login="alice") as cli:
        ids, _ = _ids(cli, f"/api/sessions?limit=50&profile={SHARED_ROOT}")
        # Negative controls: a personal store of another person stays closed, by list and by id.
        foreign_ids, foreign_body = _ids(cli, f"/api/sessions?limit=50&profile={BOB_ROOT}")
        assert cli.get(f"/api/sessions/bob-1?profile={BOB_ROOT}").status_code == 404
        assert cli.get(f"/api/sessions/bob-1?profile={SHARED_ROOT}").status_code == 404
    assert ids == {"shared-1", "shared-2"}
    assert foreign_ids == set()
    assert foreign_body["total"] == 0


def test_shared_profile_is_closed_to_an_unverified_caller(homes, multi_user, registry):
    """Fail closed: no verified identity means no owner scope, and shared rows are not public."""
    with _client() as cli:
        body = cli.get(f"/api/sessions?limit=50&profile={SHARED_ROOT}").json()
        assert cli.get(f"/api/sessions/shared-1?profile={SHARED_ROOT}").status_code == 404
    assert body["sessions"] == []
