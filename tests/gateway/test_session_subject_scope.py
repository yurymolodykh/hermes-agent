"""The socket's owner scope (МПД Т5 / §2.2 П1, П2, П3).

``SessionAuthority.authorize`` compared the row's owner with the caller for a ``LOCAL`` source only.
A messaging lane (Telegram, Discord, Slack, ...) fell through, so with ``acl.multi_user`` on a
second person's dashboard socket listed and mounted the first person's chats, and ``attach`` then
streamed their live events. ``gateway/session_subject_scope.py`` is that comparison, computed from
the same ``hermes_acl_scope.owner_stamps`` the REST surface uses, so the two surfaces cannot
disagree about who owns a row.

Pinned here, one test per assertion:

* the scope module itself (flag off / host owner / own row / foreign row / unowned row);
* the socket: ``session.list`` and ``session.resume``/``attach`` refuse a foreign id, with the
  same answer a missing id gets (never an id oracle);
* the event stream: a published event for a foreign session never reaches the other socket;
* the owner is unaffected — the whole store, as before the step.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import hermes_acl_identity
import hermes_acl_scope
from gateway.config import Platform
from gateway.owner_identity import host_owner_subject
from gateway.session import SessionSource
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_contract import SessionActor
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch

ALICE = "person/alice"
BOB = "person/bob"
OWNER_LOGIN = "owner"


@pytest.fixture(autouse=True)
def _multi_user(monkeypatch):
    """``acl.enabled`` + ``acl.multi_user`` on, identity from the verified login, no header."""
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
def authority(tmp_path, monkeypatch):
    """One profile's store with three mounted messaging rows: alice's, bob's, and an unowned one."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")

    db = SessionDB(db_path=home / "state.db")
    rows = {"alice-chat": ALICE, "bob-chat": BOB, "unowned-chat": None}
    for sid, stamp in rows.items():
        db.create_session(sid, source="telegram", user_id=stamp, chat_id="c-" + sid,
                          session_key="k-" + sid)
        db.append_message(sid, role="user", content=f"needle {sid}")
    db._conn.commit()
    epoch = begin_runtime_epoch(db, instance_id="fixture")
    store = SessionAuthority(SimpleNamespace(), profile_id=str(home), instance_id="fixture",
                             db=db, epoch=epoch)
    for sid, stamp in rows.items():
        store.sessions[sid] = LiveSession(
            SessionSource(platform=Platform.TELEGRAM, chat_id="c-" + sid, user_id=stamp or "",
                          chat_type="dm"), "route-" + sid)
    try:
        yield store
    finally:
        db.close()


class Recorder:
    """A socket transport: every frame it is handed lands in the shared journal."""

    def __init__(self, label, journal):
        self.label, self.journal = label, journal

    def write(self, frame):
        params = frame.get("params") or {}
        self.journal.append((self.label, frame.get("method"), params.get("type"),
                             params.get("session_id")))
        return True

    def close(self):
        pass


@pytest.fixture
def journal():
    return []


def _identity(login):
    return {"user_id": login, "provider": "basic", "profile_id": None, "instance_id": "fixture"}


def _connect(authority, login, journal, label):
    from hermes_cli.web_routers.chat_ws import socket_operator

    identity = dict(_identity(login), profile_id=authority.profile_id)
    conn = AuthorityConnection(authority, Recorder(label, journal), identity,
                               operator=socket_operator(identity))
    return conn


def _wait_for(journal, predicate, timeout=2.0):
    """The fanout writes from its own thread: wait for the frame instead of guessing at a sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(predicate(row) for row in journal):
            return [row for row in journal if predicate(row)]
        time.sleep(0.02)
    return [row for row in journal if predicate(row)]


# ────────────────────────────────────────────────────────────────────────────
# 1. The scope module (П3): who may hold which stored row
# ────────────────────────────────────────────────────────────────────────────


class TestScopeModule:
    def test_actor_stamps_are_the_subjects_own_stamps(self):
        from gateway.session_subject_scope import actor_stamps

        assert actor_stamps(SimpleNamespace(subject=ALICE)) == hermes_acl_scope.owner_stamps(ALICE)

    def test_an_actor_without_a_subject_matches_nothing(self):
        """Fail closed: an unestablished subject must not widen to the whole store."""
        from gateway.session_subject_scope import actor_stamps

        assert actor_stamps(SimpleNamespace(subject="")) == []

    def test_an_unusable_registry_is_a_refusal_not_an_exception(self, monkeypatch):
        from gateway import session_subject_scope

        def _broken(_identity):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(hermes_acl_scope, "owner_stamps", _broken)
        assert session_subject_scope.actor_stamps(SimpleNamespace(subject=ALICE)) == []

    def test_the_live_source_names_the_owner_before_the_stored_row(self, authority):
        from gateway.session_subject_scope import session_stamp

        assert session_stamp(authority, "bob-chat", SimpleNamespace(user_id=BOB)) == BOB

    def test_a_cold_row_falls_back_to_its_stored_stamp(self, authority):
        from gateway.session_subject_scope import session_stamp

        assert session_stamp(authority, "bob-chat", None) == BOB

    def test_the_flag_off_answers_no_foreign_row_at_all(self, authority, monkeypatch):
        """The single-user contract step 3 shipped: nothing changes while the mode is off."""
        from gateway.session_subject_scope import foreign_session_in_multi_user

        monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
        alice = SessionActor(ALICE, authority.profile_id, frozenset({"session:read"}), "t")
        assert foreign_session_in_multi_user(authority, alice, "bob-chat", None) is False

    def test_the_host_owner_keeps_the_whole_store(self, authority):
        from gateway.session_subject_scope import foreign_session_in_multi_user

        owner = SessionActor(host_owner_subject(), authority.profile_id,
                             frozenset({"session:read"}), "t")
        for sid in ("alice-chat", "bob-chat", "unowned-chat"):
            assert foreign_session_in_multi_user(authority, owner, sid, None) is False

    def test_an_own_row_is_inside_the_scope(self, authority):
        from gateway.session_subject_scope import foreign_session_in_multi_user

        alice = SessionActor(ALICE, authority.profile_id, frozenset({"session:read"}), "t")
        assert foreign_session_in_multi_user(authority, alice, "alice-chat", None) is False

    def test_a_foreign_row_is_outside_the_scope(self, authority):
        from gateway.session_subject_scope import foreign_session_in_multi_user

        alice = SessionActor(ALICE, authority.profile_id, frozenset({"session:read"}), "t")
        assert foreign_session_in_multi_user(authority, alice, "bob-chat", None) is True

    def test_an_unowned_row_is_refused_too(self, authority):
        """An owner-scoped surface never discloses a transcript whose owner is unstated."""
        from gateway.session_subject_scope import foreign_session_in_multi_user

        alice = SessionActor(ALICE, authority.profile_id, frozenset({"session:read"}), "t")
        assert foreign_session_in_multi_user(authority, alice, "unowned-chat", None) is True


# ────────────────────────────────────────────────────────────────────────────
# 2. The socket surface (П2, П3): list, resume, attach
# ────────────────────────────────────────────────────────────────────────────


class TestSocketSurface:
    @pytest.mark.asyncio
    async def test_the_listing_shows_only_the_callers_rows(self, authority):
        alice = _connect(authority, "alice", [], "A")
        try:
            reply = await alice.dispatch({"id": 1, "method": "session.list", "params": {}})
            assert [row["id"] for row in reply["result"]["sessions"]] == ["alice-chat"]
        finally:
            await alice.close()

    @pytest.mark.asyncio
    async def test_a_foreign_id_is_refused_exactly_like_a_missing_one(self, authority):
        """Never an id oracle: the two answers are the same frame, byte for byte."""
        alice = _connect(authority, "alice", [], "A")
        try:
            foreign = await alice.dispatch({"id": 1, "method": "session.resume",
                                            "params": {"session_id": "bob-chat"}})
            missing = await alice.dispatch({"id": 1, "method": "session.resume",
                                            "params": {"session_id": "no-such-id"}})
            assert foreign == missing
            assert foreign["error"]["data"]["reason"] == "not_found"
        finally:
            await alice.close()

    @pytest.mark.asyncio
    async def test_attach_is_refused_with_the_same_answer(self, authority):
        """``attach`` is the subscribe path: the refusal has to happen before the join."""
        alice = _connect(authority, "alice", [], "A")
        try:
            from gateway.session_contract import SessionRef

            with pytest.raises(RuntimeStoreError, match="not_found"):
                await authority.attach(alice.actor, SessionRef(authority.profile_id, "bob-chat"))
            with pytest.raises(RuntimeStoreError, match="not_found"):
                await authority.attach(alice.actor, SessionRef(authority.profile_id, "no-such-id"))
        finally:
            await alice.close()

    @pytest.mark.asyncio
    async def test_the_owner_still_reads_every_row(self, authority):
        owner = _connect(authority, OWNER_LOGIN, [], "owner")
        try:
            reply = await owner.dispatch({"id": 1, "method": "session.list", "params": {}})
            assert {row["id"] for row in reply["result"]["sessions"]} == {
                "alice-chat", "bob-chat", "unowned-chat"}
            from gateway.session_contract import SessionRef

            snapshot = await authority.attach(
                owner.actor, SessionRef(authority.profile_id, "bob-chat"))
            assert snapshot.history[0]["content"] == "needle bob-chat"
        finally:
            await owner.close()

    @pytest.mark.asyncio
    async def test_a_second_person_is_not_a_host_operator(self, authority):
        from hermes_cli.web_routers.chat_ws import socket_operator

        assert socket_operator(dict(_identity("alice"), profile_id=authority.profile_id)) is False
        assert socket_operator(dict(_identity(OWNER_LOGIN), profile_id=authority.profile_id)) is True


# ────────────────────────────────────────────────────────────────────────────
# 3. Live events (criterion 4): A's socket never hears about B's session
# ────────────────────────────────────────────────────────────────────────────


class TestEventStream:
    @pytest.mark.asyncio
    async def test_a_foreign_sessions_event_never_reaches_the_socket(self, authority, journal):
        alice = _connect(authority, "alice", journal, "A")
        try:
            await authority.attach(alice.actor,
                                   __import__("gateway.session_contract", fromlist=["SessionRef"])
                                   .SessionRef(authority.profile_id, "alice-chat"))
            journal.clear()
            authority.sessions["bob-chat"].event_stream.publish(
                "bob-chat", {"text": "bob live"}, event_type="message.complete")
            authority.sessions["alice-chat"].event_stream.publish(
                "alice-chat", {"text": "alice live"}, event_type="message.complete")
            # The caller's own event proves the subscription is live, so "no leak" is not
            # "nothing was delivered to anybody".
            assert _wait_for(journal, lambda r: r[3] == "alice-chat")
            assert not [row for row in journal if row[3] == "bob-chat"]
        finally:
            await alice.close()

    @pytest.mark.asyncio
    async def test_the_owner_socket_still_receives_the_rows_it_owns(self, authority, journal):
        owner = _connect(authority, OWNER_LOGIN, journal, "owner")
        try:
            from gateway.session_contract import SessionRef

            await authority.attach(owner.actor, SessionRef(authority.profile_id, "bob-chat"))
            journal.clear()
            authority.sessions["bob-chat"].event_stream.publish(
                "bob-chat", {"text": "bob live"}, event_type="message.complete")
            assert _wait_for(journal, lambda r: r[3] == "bob-chat")
        finally:
            await owner.close()


# ────────────────────────────────────────────────────────────────────────────
# 4. Real canonical sessions end to end: create through the gateway, then open
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_person_cannot_open_a_canonical_local_session(tmp_path, monkeypatch,
                                                                     journal):
    """The same rule on a session that exists as a receipt, not just as a mounted row."""
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_local import create_local_session

    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"cli": []}})
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={},
                             _draining=False, _cached_agent_for=lambda route: None,
                             _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path),
                                                   instance_id="fixture")
    caps = frozenset({"session:create", "session:read", "session:submit", "session:control"})
    made = {}
    for login, subject in (("alice", ALICE), ("bob", BOB)):
        actor = SessionActor(subject, str(tmp_path), caps, login + "-native")
        ref = create_local_session(authority, actor, {"request_id": login, "source": "gui",
                                                     "cwd": str(tmp_path), "model": "fixture",
                                                     "toolsets": []})
        authority.db.append_message(ref.session_id, "user", f"{login} needle")
        made[login] = ref.session_id
    try:
        def _ident(login):
            return {"user_id": login, "provider": "basic", "profile_id": str(tmp_path),
                    "instance_id": "fixture"}

        from hermes_cli.web_routers.chat_ws import socket_operator

        alice = AuthorityConnection(authority, Recorder("A", journal), _ident("alice"),
                                    operator=socket_operator(_ident("alice")))
        owner = AuthorityConnection(authority, Recorder("owner", journal), _ident(OWNER_LOGIN),
                                    operator=socket_operator(_ident(OWNER_LOGIN)))
        own = await alice.dispatch({"id": 1, "method": "session.resume",
                                    "params": {"session_id": made["alice"]}})
        assert own["result"]["session_id"] == made["alice"]
        foreign = await alice.dispatch({"id": 2, "method": "session.resume",
                                        "params": {"session_id": made["bob"]}})
        assert "result" not in foreign and "bob needle" not in str(foreign)
        listing = await alice.dispatch({"id": 3, "method": "session.list", "params": {}})
        assert [row["id"] for row in listing["result"]["sessions"]] == [made["alice"]]
        as_owner = await owner.dispatch({"id": 4, "method": "session.resume",
                                         "params": {"session_id": made["bob"]}})
        assert as_owner["result"]["session_id"] == made["bob"]
        journal.clear()
        authority.sessions[made["bob"]].event_stream.publish(
            made["bob"], {"text": "bob live"}, event_type="message.complete")
        authority.sessions[made["alice"]].event_stream.publish(
            made["alice"], {"text": "alice live"}, event_type="message.complete")
        assert _wait_for(journal, lambda r: r[3] == made["alice"])
        assert not [row for row in journal if row[0] == "A" and row[3] == made["bob"]]
        await alice.close()
        await owner.close()
    finally:
        store._db.close()


@pytest.mark.asyncio
async def test_the_flag_off_keeps_the_single_user_socket(tmp_path, monkeypatch):
    """Regression guard: with ``acl.multi_user`` off one person owns the host, as before."""
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_local import create_local_session

    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"cli": []}})
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={},
                             _draining=False, _cached_agent_for=lambda route: None,
                             _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path),
                                                   instance_id="fixture")
    caps = frozenset({"session:create", "session:read", "session:submit", "session:control"})
    native = SessionActor("uid:1000", str(tmp_path), caps, "native")
    ref = create_local_session(authority, native, {"request_id": "one", "source": "gui",
                                                   "cwd": str(tmp_path), "model": "fixture",
                                                   "toolsets": []})
    try:
        identity = {"user_id": "uid:1000", "provider": "local", "profile_id": str(tmp_path),
                    "instance_id": "fixture"}
        from hermes_cli.web_routers.chat_ws import socket_operator

        connection = AuthorityConnection(authority, Recorder("socket", []), identity,
                                         operator=socket_operator(identity))
        reply = await connection.dispatch({"id": 1, "method": "session.resume",
                                           "params": {"session_id": ref.session_id}})
        assert reply["result"]["session_id"] == ref.session_id
        await connection.close()
    finally:
        store._db.close()
