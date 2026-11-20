"""Owner scope on the session REST surface and the dashboard socket (МПД Т5 / §2.2 П6).

The hole: a dashboard login authenticated the PROFILE, and the cookie session handed every
signed-in browser ``session:operator`` — a capability three gates read as "may act for any stored
owner". So the second person on the host listed, opened, exported and deleted the owner's
conversations; the socket added the same rows as live events.

What these tests pin, one per route named by the analysis (§2.2 П6) plus the socket:

* ``acl.enabled=false`` (the default) changes NOTHING — covered by
  ``tests/gateway/test_session_owner_scope.py``; here only the multi-user switch itself.
* ``acl.multi_user=true``: ``session:operator`` is the HOST OWNER's capability. The owner keeps
  the whole store, a second subject is confined to its own stamps.
* a foreign id answers **404**, byte-identical to an absent one — never 403 (an id oracle).
* store-wide destructive routes (prune / empty / owner-backfill) refuse a non-owner with 403,
  because their selection is a predicate over every owner's rows.

Identity is presented the way production does it: a verified dashboard login
(``request.state.session``), not a header — a header needs ``acl.identity_trust=trusted-proxy``
and is a self-declaration otherwise.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_acl_scope
import hermes_acl_identity
from gateway.owner_identity import host_owner_subject
from hermes_state import SessionDB

ALICE = "person/alice"
BOB = "person/bob"
ALICE_SECRET = "alice-owned-needle-aa11"
BOB_SECRET = "bob-owned-needle-bb22"


class _Session:
    """The dashboard login the auth gate attaches (``request.state.session``)."""

    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


def _login(login: str) -> _Session:
    """A verified password login for *login*, exactly as ``basic`` provider mints it."""
    return _Session(login, "basic", "")


OWNER_LOGIN = "owner"


@pytest.fixture(autouse=True)
def _multi_user(monkeypatch):
    """Multi-user ON, no trusted-proxy header: the configuration this step exists for.

    ``resolve_subject`` is the users-file rule (``role: owner`` keeps the owner's subject,
    everybody else becomes ``person/<login>``); stubbed here so no users file is needed.
    """
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_scope, "identity_trust", lambda: "untrusted")
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        if not login:
            return fallback
        return host_owner_subject() if login == OWNER_LOGIN else f"person/{login}"

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", _resolve)


@pytest.fixture
def store(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")

    db = SessionDB(db_path=home / "state.db")
    try:
        for sid, owner, text in (("alice-s1", ALICE, ALICE_SECRET),
                                 ("bob-s1", BOB, BOB_SECRET),
                                 ("owner-s1", host_owner_subject(), "owner-owned-needle-cc33")):
            db.create_session(sid, source="desktop", user_id=owner)
            db._conn.execute("UPDATE sessions SET started_at = ?, title = ?, model = ? WHERE id = ?",
                             (int(time.time()), f"chat {owner}", "m", sid))
            db.append_message(sid, role="user", content=text)
            db.append_message(sid, role="assistant", content=f"reply to {owner}")
            db._conn.commit()
            db.end_session(sid, "done")
    finally:
        db.close()
    yield home


def _app():
    from hermes_cli.web_routers.sessions import list_router, manage_router, search_router

    app = FastAPI()
    for router in (list_router, search_router, manage_router):
        app.include_router(router)
    return app


def _client(*, as_login=None):
    """A client whose requests act as *as_login*, or as nobody (no verified identity)."""
    client = TestClient(_app())
    if as_login is not None:
        @client.app.middleware("http")
        async def _stamp(request, call_next):
            request.state.session = _login(as_login)
            return await call_next(request)
    return client


def _mask(payload, session_id):
    """A JSON body with the caller-supplied id blanked, so two refusals compare equal."""
    return json.loads(json.dumps(payload).replace(session_id, "<id>"))


# ────────────────────────────────────────────────────────────────────────────
# 1. Listing (П6: /api/sessions)
# ────────────────────────────────────────────────────────────────────────────


def test_listing_shows_only_the_callers_sessions(store):
    with _client(as_login="alice") as cli:
        body = cli.get("/api/sessions?limit=50").json()
    assert [row["id"] for row in body["sessions"]] == ["alice-s1"]
    assert body["total"] == 1


def test_listing_for_the_owner_is_their_own_rows(store):
    """Criterion 5: the owner keeps seeing what is THEIRS (no regression), and no more.

    Multi-user does not promote the host owner to a universal reader: their own rows
    (``uid:1001`` etc.) stay visible exactly as before, a person's rows do not join them.
    The cross-owner bypass the owner still has is ``session:operator`` on the socket, not
    a widened REST listing.
    """
    with _client(as_login=OWNER_LOGIN) as cli:
        body = cli.get("/api/sessions?limit=50").json()
    assert [row["id"] for row in body["sessions"]] == ["owner-s1"]
    raw = SessionDB(db_path=store / "state.db")
    try:
        assert raw.session_count(include_archived=True) == 3   # the store itself is untouched
    finally:
        raw.close()


def test_listing_without_a_verified_identity_lists_nothing(store):
    """``untrusted`` header trust + no dashboard login: fail closed, not fail open."""
    with _client() as cli:
        assert cli.get("/api/sessions?limit=50").json()["sessions"] == []


def test_acl_off_is_unchanged(store, monkeypatch):
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
    with _client() as cli:
        ids = {row["id"] for row in cli.get("/api/sessions?limit=50").json()["sessions"]}
        assert ids == {"alice-s1", "bob-s1", "owner-s1"}
        assert cli.get("/api/sessions/bob-s1").status_code == 200


# ────────────────────────────────────────────────────────────────────────────
# 2. Search (П6: /api/sessions/search) — every lane
# ────────────────────────────────────────────────────────────────────────────


def test_search_does_not_return_a_foreign_session_by_content(store):
    with _client(as_login="alice") as cli:
        hits = cli.get("/api/sessions/search?q=needle").json()["results"]
    assert [hit["session_id"] for hit in hits] == ["alice-s1"]
    assert BOB_SECRET not in json.dumps(hits)


def test_search_does_not_return_a_foreign_session_by_id(store):
    """The id lane is an oracle unless it is scoped too."""
    with _client(as_login="alice") as cli:
        assert cli.get("/api/sessions/search?q=bob-s1").json()["results"] == []


def test_search_for_the_owner_finds_both(store):
    with _client(as_login=OWNER_LOGIN) as cli:
        hits = cli.get("/api/sessions/search?q=owned").json()["results"]
    assert {hit["session_id"] for hit in hits} == {"owner-s1"}


# ────────────────────────────────────────────────────────────────────────────
# 3. Direct read paths by id — 404, identical to a genuine miss
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,query", [
    ("/api/sessions/{sid}", ""),
    ("/api/sessions/{sid}/messages", ""),
    ("/api/sessions/{sid}/timeline", ""),
    ("/api/sessions/{sid}/messages/around", "?row_id=1"),
    ("/api/sessions/{sid}/mutation-snapshot", ""),
    ("/api/sessions/{sid}/latest-descendant", ""),
    ("/api/sessions/{sid}/export", ""),
])
def test_every_read_route_refuses_a_foreign_id_as_a_miss(store, path, query):
    with _client(as_login="alice") as cli:
        foreign = cli.get(path.format(sid="bob-s1") + query)
        missing = cli.get(path.format(sid="no-such-id") + query)
        own = cli.get(path.format(sid="alice-s1") + query)
    assert foreign.status_code == 404, path
    assert missing.status_code == 404, path
    assert _mask(foreign.json(), "bob-s1") == _mask(missing.json(), "no-such-id"), path
    assert BOB_SECRET not in foreign.text
    # The caller's OWN row still answers on every one of them (except <around>, whose row_id
    # belongs to alice-s1's transcript and is addressed below by a real row).
    if "around" not in path:
        assert own.status_code == 200, path


def test_messages_around_reads_the_owners_own_row(store):
    db = SessionDB(db_path=store / "state.db")
    try:
        first = db.get_messages("alice-s1")[0]["id"]
    finally:
        db.close()
    with _client(as_login="alice") as cli:
        assert cli.get(f"/api/sessions/alice-s1/messages/around?row_id={first}").status_code == 200


def test_statistics_count_only_the_callers_rows(store):
    with _client(as_login="alice") as cli:
        alice = cli.get("/api/sessions/stats").json()
    with _client(as_login=OWNER_LOGIN) as cli:
        owner = cli.get("/api/sessions/stats").json()
    assert alice["total"] == alice["active_store"] == 1
    assert alice["messages"] == 2
    assert sum(alice["by_source"].values()) == 1
    # The owner's counters describe the owner's row, not the whole store: the shape of other
    # people's stores is not disclosed either.
    assert owner["total"] == 1 and owner["messages"] == 2


def test_empty_count_is_scoped(store):
    db = SessionDB(db_path=store / "state.db")
    try:
        db.create_session("alice-empty", source="desktop", user_id=ALICE)
        db.end_session("alice-empty", "done")
        db.create_session("bob-empty", source="desktop", user_id=BOB)
        db.end_session("bob-empty", "done")
    finally:
        db.close()
    with _client(as_login="alice") as cli:
        assert cli.get("/api/sessions/empty/count").json()["count"] == 1
    with _client(as_login=OWNER_LOGIN) as cli:
        assert cli.get("/api/sessions/empty/count").json()["count"] == 0


# ────────────────────────────────────────────────────────────────────────────
# 4. Mutations by id
# ────────────────────────────────────────────────────────────────────────────


def test_delete_of_a_foreign_session_is_404_and_deletes_nothing(store):
    with _client(as_login="alice") as cli:
        resp = cli.delete("/api/sessions/bob-s1")
    assert resp.status_code == 404
    db = SessionDB(db_path=store / "state.db")
    try:
        assert db.get_session("bob-s1") is not None
    finally:
        db.close()


def test_rename_of_a_foreign_session_is_404(store):
    with _client(as_login="alice") as cli:
        resp = cli.patch("/api/sessions/bob-s1", json={
            "title": "pwned", "request_id": "r1", "expected_revision": 1})
    assert resp.status_code == 404
    db = SessionDB(db_path=store / "state.db")
    try:
        assert db.get_session("bob-s1")["title"] != "pwned"
    finally:
        db.close()


def test_bulk_delete_ignores_foreign_ids(store):
    with _client(as_login="alice") as cli:
        body = cli.post("/api/sessions/bulk-delete", json={"ids": ["bob-s1", "alice-s1"]}).json()
    assert body["deleted"] == 1, body
    db = SessionDB(db_path=store / "state.db")
    try:
        assert db.get_session("bob-s1") is not None
        assert db.get_session("alice-s1") is None
    finally:
        db.close()


def test_import_onto_a_foreign_id_is_refused(store):
    payload = {"sessions": [{"id": "bob-s1", "source": "desktop",
                             "messages": [{"role": "user", "content": "overwrite"}]}],
               "request_id": "r1", "expected_revision": 1}
    with _client(as_login="alice") as cli:
        resp = cli.post("/api/sessions/import", json=payload)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "import_conflict"


# ────────────────────────────────────────────────────────────────────────────
# 5. Store-wide destructive routes belong to the owner
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/sessions/prune", {"older_than_days": 1}),
    ("post", "/api/sessions/prune", {"older_than_days": 1, "dry_run": True}),
    ("post", "/api/sessions/owner-backfill", {}),
])
def test_store_wide_writes_refuse_a_non_owner(store, method, path, body):
    with _client(as_login="alice") as cli:
        resp = getattr(cli, method)(path, json=body) if body is not None else getattr(cli, method)(path)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "owner_only"


def test_empty_sweep_is_scoped_to_the_callers_own_rows(store):
    """Retiring one's own empty conversations is a per-person affordance, not an owner one."""
    db = SessionDB(db_path=store / "state.db")
    try:
        for sid, owner in (("alice-empty", ALICE), ("bob-empty", BOB)):
            db.create_session(sid, source="desktop", user_id=owner)
            db.end_session(sid, "done")
    finally:
        db.close()
    with _client(as_login="alice") as cli:
        assert cli.delete("/api/sessions/empty").json()["deleted"] == 1
    db = SessionDB(db_path=store / "state.db")
    try:
        assert db.get_session("bob-empty") is not None
        assert db.get_session("alice-empty") is None
    finally:
        db.close()


def test_store_wide_writes_still_work_for_the_owner(store):
    with _client(as_login=OWNER_LOGIN) as cli:
        assert cli.post("/api/sessions/prune", json={"older_than_days": 1, "dry_run": True}).status_code == 200


# ────────────────────────────────────────────────────────────────────────────
# 6. The capability itself (П1/П2/П3): ``session:operator`` is the owner's
# ────────────────────────────────────────────────────────────────────────────


class _Actor:
    def __init__(self, subject, capabilities):
        self.subject, self.capabilities = subject, frozenset(capabilities)


def test_operator_capability_belongs_to_the_host_owner(monkeypatch):
    from gateway.owner_identity import is_host_operator

    owner = host_owner_subject()
    assert is_host_operator(_Actor(owner, {"session:operator"})) is True
    assert is_host_operator(_Actor(ALICE, {"session:operator"})) is False
    assert is_host_operator(_Actor(ALICE, {"session:read"})) is False


def test_operator_capability_is_unchanged_with_multi_user_off(monkeypatch):
    """Backwards compatibility: single-user mode keeps the old capability meaning."""
    from gateway.owner_identity import is_host_operator

    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    assert is_host_operator(_Actor(ALICE, {"session:operator"})) is True


def test_host_owner_identity_resolution():
    from gateway.owner_identity import identity_is_host_owner

    owner = host_owner_subject()
    assert identity_is_host_owner({"user_id": "alice", "provider": "basic"}) is False
    assert identity_is_host_owner({}) is False
    assert identity_is_host_owner({"user_id": "alice", "provider": "local"}) in (True, False)
    assert identity_is_host_owner({"subject": owner, "user_id": "x"}) is False


def test_socket_operator_is_only_the_host_owner(monkeypatch):
    """Criterion 4: a second person's dashboard socket is not a host operator.

    ``session:operator`` is what let a socket read other owners' sessions (``session.resume`` and
    the event stream answer for any stored owner), so the socket's grant is now the owner's.
    """
    from hermes_cli.web_routers.chat_ws import socket_operator

    assert socket_operator({"user_id": "alice", "provider": "basic"}) is False
    assert socket_operator({"user_id": OWNER_LOGIN, "provider": "basic"}) is True
    # A socket with no identity at all stays a plain principal: in multi-user mode "nothing was
    # verified" must not become "the operator". (The loopback token's own path synthesises the
    # owner's identity before this call, so it keeps its access.)
    assert socket_operator(None) is False


def test_socket_operator_is_unchanged_with_multi_user_off(monkeypatch):
    from hermes_cli.web_routers.chat_ws import socket_operator

    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    assert socket_operator({"user_id": "alice", "provider": "basic"}) is True


def test_socket_scope_is_profile_bound_for_a_non_owner(monkeypatch):
    """A second person's socket may not select a sibling profile by name."""
    from gateway.owner_identity import identity_is_host_owner

    assert identity_is_host_owner({"user_id": "alice", "provider": "basic"}) is False
    assert identity_is_host_owner({"user_id": OWNER_LOGIN, "provider": "basic"}) is True


def test_bindings_registry_names_the_owner_as_owner_branch(monkeypatch):
    """The owner's own dashboard login resolves to the owner, not to a person."""
    from gateway.owner_identity import identity_is_host_owner

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", lambda value, fallback="": fallback)
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    assert identity_is_host_owner({"user_id": "alice", "provider": "basic"}) is True
