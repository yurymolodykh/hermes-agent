"""Owner scope for session history: store filter, HTTP surface, ``session_search``.

The hole this closes: the profile API key authenticates the PROFILE, not a person inside it, and
``sessions.user_id`` was never consulted on any read path — so any key-holder listed and opened
everybody's dialogs in that profile (#t_93b857ee).

Scope of these tests, stated up front:

* ``acl.enabled=false`` (the default) must change NOTHING. Proven by a live before/after dump
  comparison recorded in the task evidence, and by the identity fixtures below that assert the
  unscoped call returns the whole profile.
* ``acl.enabled=true`` scopes every read path to the caller's owner stamps, and fails CLOSED:
  an unestablished subject sees nothing, never everything.
* Known limitation, not covered because it does not exist: **channel/group MEMBERSHIP is not
  verified**. A group chat is served because the profile says so (``chat_type``); there is no
  live platform-API membership lookup on this path. ``is_shared_chat`` only names the transport,
  and no test here should be read as proving a group member's rights.
"""

import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import hermes_acl_scope
from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_context import _VAR_MAP, _UNSET, clear_session_vars, set_session_vars
from hermes_state import SessionDB
from tools.session_search_tool import session_search

ALICE = "tg:111"
BOB = "tg:222"
CAROL = "tg:999"

CAROL_SECRET = "CAROL-PRIVATE-NEEDLE-zzz9"
ALICE_SECRET = "alice-own-needle-qqq7"


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_session_ctx():
    """Every test starts with NO bound session vars (a fresh task in production)."""
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    try:
        yield d
    finally:
        close = getattr(d, "close", None)
        if callable(close):
            close()


@pytest.fixture
def acl_on(monkeypatch):
    """``acl.enabled=true`` with a trusted proxy identity header.

    Patched at the three accessors ``hermes_acl_scope`` reads rather than through ``config.yaml``:
    the file path is exercised once, end to end, by
    ``TestConfigWiring::test_enabled_flag_reads_the_real_config_file`` — every other test here is
    about the policy, not about YAML parsing.
    """
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_scope, "identity_trust", lambda: "trusted-proxy")
    monkeypatch.setattr(hermes_acl_scope, "identity_header", lambda: "X-Hermes-User-Id")
    return True


@pytest.fixture
def acl_off(monkeypatch):
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
    return True


def _seed(db, session_id, owner, title, needle):
    """One session with an owner stamp, a title and one searchable message pair."""
    db.create_session(session_id, source="telegram", user_id=owner)
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (int(time.time()), title, session_id))
    db.append_message(session_id, role="user", content=f"question {needle}")
    db.append_message(session_id, role="assistant", content=f"answer {needle}")
    db._conn.commit()
    return session_id


@pytest.fixture
def populated(db):
    """Three owners' sessions in ONE profile store — the exact shape of the live report."""
    _seed(db, "alice-s1", ALICE, "ALICE OWN CHAT", ALICE_SECRET)
    _seed(db, "bob-s1", BOB, "BOB CHAT", "bob-needle-kkk3")
    _seed(db, "carol-s9", CAROL, "CAROL PRIVATE", CAROL_SECRET)
    return db


def _as_caller(actor_id):
    """Bind a one-sender turn by *actor_id* (``set_session_vars`` is how the gateway binds it)."""
    tokens = set_session_vars(platform="telegram", source="telegram", chat_id="c1",
                              chat_type="dm", user_id="route-id", actor_id=actor_id)
    return tokens


def _as_merged_turn(actor_id, second_sender_event):
    """A turn whose input was folded from a SECOND sender: ACTOR must read empty."""
    tokens = set_session_vars(platform="telegram", source="telegram", chat_id="-1001",
                              chat_type="group", user_id="route-id", actor_id=actor_id,
                              turn_event=second_sender_event)
    return tokens


def _merged_event(sender):
    """A ``MessageEvent`` whose ``user_id`` is ``""`` — what ``absorb_turn_input`` writes."""
    event = MessageEvent(text="second sender", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id=sender))
    event.user_id = ""
    return event


# ────────────────────────────────────────────────────────────────────────────
# 1. Store: list_sessions_rich / counts take an owner scope
# ────────────────────────────────────────────────────────────────────────────


class TestStoreOwnerFilter:
    def test_list_sessions_rich_returns_only_the_owner(self, populated):
        rows = populated.list_sessions_rich(user_id=ALICE, limit=50)
        assert [r["id"] for r in rows] == ["alice-s1"]

    def test_list_sessions_rich_accepts_the_several_stamps_of_one_subject(self, populated):
        rows = populated.list_sessions_rich(user_id=[ALICE, "111"], limit=50)
        assert [r["id"] for r in rows] == ["alice-s1"]

    def test_none_scope_is_the_unscoped_legacy_listing(self, populated):
        """``None`` = no filter: the CLI/desktop surface of the host owner sees everything."""
        rows = populated.list_sessions_rich(user_id=None, limit=50)
        assert {r["id"] for r in rows} == {"alice-s1", "bob-s1", "carol-s9"}

    def test_empty_scope_lists_nothing_fail_closed(self, populated):
        """An unestablished subject is ``[]``, NOT ``None``: 1 = 0, never 'everything'."""
        assert populated.list_sessions_rich(user_id=[], limit=50) == []

    def test_row_with_no_owner_stamp_never_matches_a_scope(self, populated):
        populated.create_session("unstamped", source="telegram")
        populated._conn.commit()
        ids = {r["id"] for r in populated.list_sessions_rich(user_id=ALICE, limit=50)}
        assert "unstamped" not in ids
        # ...and it is still there for the unscoped surface
        assert "unstamped" in {r["id"] for r in populated.list_sessions_rich(user_id=None, limit=50)}

    def test_count_matches_the_owner_scoped_page(self, populated):
        """``session_count`` must pair with ``list_sessions_rich`` or 'load more' lies."""
        listed = populated.list_sessions_rich(user_id=ALICE, limit=50)
        counted = populated.session_count(user_id=ALICE)
        assert counted == len(listed) == 1
        assert populated.session_count(user_id=None) == 3

    def test_bounded_recent_inherits_the_scope(self, populated):
        sessions = populated.list_recent_sessions_bounded(limit=10, user_id=ALICE)
        assert [s["id"] for s in sessions] == ["alice-s1"]


# ────────────────────────────────────────────────────────────────────────────
# 2. Identity source: ACTOR, not the routing USER_ID
# ────────────────────────────────────────────────────────────────────────────


class TestActorIdentity:
    def test_one_sender_turn_reports_that_sender_as_the_subject(self, _acl):
        tokens = _as_caller(ALICE)
        try:
            assert hermes_acl_scope.caller_subject() == ALICE
        finally:
            clear_session_vars(tokens)

    def test_route_user_id_is_never_the_subject(self, _acl):
        """The bug the upstream patch carried: ``USER_ID`` is the ROUTE, empty in a shared chat.

        Here USER_ID is a plausible-looking value and ACTOR is a different person: the scope must
        follow ACTOR. Reading USER_ID would hand this caller the ROUTE's owner's history.
        """
        tokens = set_session_vars(platform="telegram", source="telegram", chat_id="c1",
                                  chat_type="dm", user_id=CAROL, actor_id=ALICE)
        try:
            assert hermes_acl_scope.caller_owner_scope() == [ALICE]
        finally:
            clear_session_vars(tokens)

    def test_empty_route_with_a_real_actor_still_scopes(self, _acl):
        """Telegram observe mode: USER_ID is deliberately empty, ACTOR carries the person."""
        tokens = set_session_vars(platform="telegram", source="telegram", chat_id="-1001",
                                  chat_type="group", user_id="", actor_id=ALICE)
        try:
            assert hermes_acl_scope.caller_subject() == ALICE
        finally:
            clear_session_vars(tokens)

    def test_merged_two_sender_turn_has_no_subject(self, _acl):
        """``absorb_turn_input`` blanks ``user_id`` when a second person's input joins the turn.

        Such a turn answers two people at once, so it must see NEITHER's history — not "the first
        sender's", which is what reading the copied ACTOR contextvar alone would do.
        """
        tokens = _as_merged_turn(ALICE, _merged_event(BOB))
        try:
            assert hermes_acl_scope.caller_subject() == ""
            assert hermes_acl_scope.caller_owner_scope() == []
        finally:
            clear_session_vars(tokens)

    def test_unknown_subject_is_empty_scope_not_unscoped(self, _acl):
        tokens = _as_caller("")
        try:
            assert hermes_acl_scope.caller_owner_scope() == []
        finally:
            clear_session_vars(tokens)

    def test_acl_disabled_returns_no_scope_at_all(self, acl_off):
        """``None`` (unscoped) — distinct from ``[]`` (nothing). Compat depends on it."""
        tokens = _as_caller(ALICE)
        try:
            assert hermes_acl_scope.caller_owner_scope() is None
        finally:
            clear_session_vars(tokens)


@pytest.fixture
def _acl(acl_on):
    """``acl_on`` under a name that reads in the identity tests above."""
    return acl_on


# ────────────────────────────────────────────────────────────────────────────
# 3. session_search: all three surfaces
# ────────────────────────────────────────────────────────────────────────────


def _search(**kw):
    kw.setdefault("limit", 5)
    out = session_search(db=kw.pop("db"), **kw)
    return json.loads(out)


def _mask(res, *needles):
    """Response text with the caller's OWN inputs masked out.

    A miss echoes the id/query the caller supplied, so two misses can never be byte-equal while
    the id differs — and they must NOT be: the caller already knows what it asked for. The
    security property is that a foreign row and a genuine miss produce the SAME error shape and
    wording, with nothing about the row's existence, owner or content in it.
    """
    text = json.dumps(res, sort_keys=True)
    for needle in needles:
        text = text.replace(json.dumps(needle)[1:-1], "<CALLER-INPUT>")
    return text


class TestSessionSearchRead:
    def test_foreign_session_id_is_plainly_not_found(self, populated, acl_on):
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, session_id="carol-s9")
        finally:
            clear_session_vars(tokens)
        assert res["success"] is False
        assert "session_id not found" in res["error"]
        assert CAROL_SECRET not in json.dumps(res)
        assert "CAROL PRIVATE" not in json.dumps(res)

    def test_foreign_miss_is_byte_identical_to_a_real_miss(self, populated, acl_on):
        """Anti-oracle: 'someone else's' and 'no such row' must be indistinguishable."""
        tokens = _as_caller(ALICE)
        try:
            foreign = _search(db=populated, session_id="carol-s9")
            missing = _search(db=populated, session_id="no-such-session-at-all")
        finally:
            clear_session_vars(tokens)
        assert _mask(foreign, "carol-s9") == _mask(missing, "no-such-session-at-all")
        assert foreign["success"] is missing["success"] is False
        assert "not found" in foreign["error"] and "not found" in missing["error"]

    def test_own_session_reads_in_full(self, populated, acl_on):
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, session_id="alice-s1")
        finally:
            clear_session_vars(tokens)
        assert res["success"] is True
        assert ALICE_SECRET in json.dumps(res)

    def test_no_actor_reads_nothing(self, populated, acl_on):
        """Merged/unknown turn: even a session it might have owned is not served."""
        tokens = _as_merged_turn(ALICE, _merged_event(BOB))
        try:
            res = _search(db=populated, session_id="alice-s1")
        finally:
            clear_session_vars(tokens)
        assert res["success"] is False
        assert ALICE_SECRET not in json.dumps(res)


class TestSessionSearchDiscovery:
    def test_text_only_in_a_foreign_session_returns_nothing(self, populated, acl_on):
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, query=CAROL_SECRET)
        finally:
            clear_session_vars(tokens)
        # The query is echoed back (the caller typed it); nothing FROM the row may appear.
        assert res["results"] == [] and res["count"] == 0
        assert "carol-s9" not in json.dumps(res)
        assert "CAROL PRIVATE" not in json.dumps(res)

    def test_foreign_hit_is_indistinguishable_from_a_genuine_miss(self, populated, acl_on):
        tokens = _as_caller(ALICE)
        try:
            foreign = _search(db=populated, query=CAROL_SECRET)
            genuine = _search(db=populated, query="no-such-token-anywhere-whatsoever")
        finally:
            clear_session_vars(tokens)
        assert _mask(foreign, CAROL_SECRET) == _mask(genuine, "no-such-token-anywhere-whatsoever")
        assert foreign["count"] == genuine["count"] == 0
        assert foreign.get("results") == genuine.get("results")

    def test_own_text_is_found(self, populated, acl_on):
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, query=ALICE_SECRET)
        finally:
            clear_session_vars(tokens)
        assert "alice-s1" in json.dumps(res)

    def test_foreign_title_match_does_not_leak(self, populated, acl_on):
        """The title shortcut bypasses the FTS query, so it needs its own owner check."""
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, query="CAROL PRIVATE")
        finally:
            clear_session_vars(tokens)
        assert "carol-s9" not in json.dumps(res)
        assert CAROL_SECRET not in json.dumps(res)


class TestSessionSearchBrowse:
    def test_browse_lists_only_the_callers_sessions(self, populated, acl_on):
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated)
        finally:
            clear_session_vars(tokens)
        assert "carol-s9" not in json.dumps(res)
        assert "alice-s1" in json.dumps(res)

    def test_merged_turn_browses_an_empty_list(self, populated, acl_on):
        tokens = _as_merged_turn(ALICE, _merged_event(BOB))
        try:
            res = _search(db=populated)
        finally:
            clear_session_vars(tokens)
        assert "carol-s9" not in json.dumps(res)
        assert "alice-s1" not in json.dumps(res)

    def test_acl_disabled_browse_is_unscoped(self, populated, acl_off):
        """Backwards compatibility: the host owner's CLI still sees every session."""
        res = _search(db=populated)
        assert "carol-s9" in json.dumps(res)
        assert "alice-s1" in json.dumps(res)


class TestLineageOwnership:
    """Both levels of the lineage are checked: the ROOT's owner and the HIT's own owner."""

    def _child(self, db, parent_id, child_id, owner):
        db.create_session(child_id, source="telegram", parent_session_id=parent_id, user_id=owner)
        db.append_message(child_id, role="user", content=f"child needle {CAROL_SECRET}")
        db.append_message(child_id, role="assistant", content="child answer")
        db._conn.commit()

    def test_child_with_another_owner_is_not_read_under_a_shared_root(self, populated, acl_on):
        """Root owned by ALICE, continuation child owned by CAROL: the child must stay closed."""
        self._child(populated, "alice-s1", "child-of-alice", CAROL)
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, session_id="child-of-alice")
        finally:
            clear_session_vars(tokens)
        assert res["success"] is False
        assert CAROL_SECRET not in json.dumps(res)

    def test_foreign_root_hides_an_owned_child(self, populated, acl_on):
        """Mirror case: a child the caller owns under somebody else's root is not a read path."""
        self._child(populated, "carol-s9", "child-of-carol", ALICE)
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, session_id="child-of-carol")
        finally:
            clear_session_vars(tokens)
        assert res["success"] is False

    def test_scroll_rebind_onto_a_foreign_descendant_is_refused(self, populated, acl_on):
        """Naming your OWN parent plus a foreign child's message id must not rebind."""
        self._child(populated, "alice-s1", "carol-child", CAROL)
        anchor = populated._conn.execute(
            "SELECT id FROM messages WHERE session_id = 'carol-child' ORDER BY id LIMIT 1").fetchone()[0]
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, session_id="alice-s1", around_message_id=anchor)
        finally:
            clear_session_vars(tokens)
        assert res["success"] is False
        assert CAROL_SECRET not in json.dumps(res)

    def test_scroll_rebind_onto_an_own_descendant_still_works(self, populated, acl_on):
        self._child(populated, "alice-s1", "alice-child", ALICE)
        anchor = populated._conn.execute(
            "SELECT id FROM messages WHERE session_id = 'alice-child' ORDER BY id LIMIT 1").fetchone()[0]
        tokens = _as_caller(ALICE)
        try:
            res = _search(db=populated, session_id="alice-s1", around_message_id=anchor)
        finally:
            clear_session_vars(tokens)
        assert res["success"] is True
        assert ALICE_SECRET or "child needle" in json.dumps(res)


# ────────────────────────────────────────────────────────────────────────────
# 4. HTTP surface: the live report, reproduced
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def http_adapter(populated, acl_on):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-test"}))
    adapter._session_db = populated
    return adapter


def _app(adapter):
    app = web.Application()
    app.router.add_get("/api/sessions", adapter._handle_list_sessions)
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_delete("/api/sessions/{session_id}", adapter._handle_delete_session)
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    return app


AUTH = {"Authorization": "Bearer sk-test"}


@pytest.mark.asyncio
class TestHttpSurface:
    async def test_foreign_session_is_absent_from_the_listing(self, http_adapter):
        """Report t_f2146480: alice's key listed carol-s9. It must not."""
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            resp = await cli.get("/api/sessions?limit=50", headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert resp.status == 200
            ids = [s["id"] for s in (await resp.json())["data"]]
        assert "carol-s9" not in ids
        assert "bob-s1" not in ids

    async def test_own_sessions_are_listed_in_full(self, http_adapter):
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            resp = await cli.get("/api/sessions?limit=50", headers={**AUTH, "X-Hermes-User-Id": ALICE})
            ids = [s["id"] for s in (await resp.json())["data"]]
        assert ids == ["alice-s1"]

    async def test_direct_get_on_a_foreign_session_is_404_not_403(self, http_adapter):
        """404, not 403: a 403 would confirm the id exists (the surface must not be an oracle)."""
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            resp = await cli.get("/api/sessions/carol-s9", headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert resp.status == 404
            body = await resp.json()
            assert CAROL_SECRET not in json.dumps(body)
            assert "CAROL PRIVATE" not in json.dumps(body)

            missing = await cli.get("/api/sessions/no-such-id", headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert missing.status == 404
            # Same envelope and code; only the id the caller itself supplied differs.
            assert _mask(await missing.json(), "no-such-id") == _mask(body, "carol-s9")

    async def test_own_session_get_succeeds(self, http_adapter):
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            resp = await cli.get("/api/sessions/alice-s1", headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert resp.status == 200

    async def test_foreign_messages_endpoint_is_404(self, http_adapter):
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            resp = await cli.get("/api/sessions/carol-s9/messages",
                                 headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert resp.status == 404
            assert CAROL_SECRET not in await resp.text()

    async def test_foreign_delete_is_404_and_deletes_nothing(self, http_adapter, populated):
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            resp = await cli.delete("/api/sessions/carol-s9", headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert resp.status == 404
        assert populated.get_session("carol-s9") is not None

    async def test_untrusted_identity_source_closes_the_surface(self, http_adapter, monkeypatch):
        """``acl.enabled`` without a trusted proxy: nobody's history, and say so."""
        monkeypatch.setattr(hermes_acl_scope, "identity_trust", lambda: "untrusted")
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            # Even presenting the header must not help: it is a self-declaration.
            resp = await cli.get("/api/sessions?limit=50",
                                 headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert (await resp.json())["data"] == []
            own = await cli.get("/api/sessions/alice-s1", headers={**AUTH, "X-Hermes-User-Id": ALICE})
            assert own.status == 404

    async def test_acl_disabled_api_is_unchanged(self, http_adapter, monkeypatch):
        """Backwards compatibility is a hard requirement: unscoped listing and open-by-id."""
        monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
        async with TestClient(TestServer(_app(http_adapter))) as cli:
            resp = await cli.get("/api/sessions?limit=50", headers=AUTH)
            ids = {s["id"] for s in (await resp.json())["data"]}
            assert ids == {"alice-s1", "bob-s1", "carol-s9"}
            opened = await cli.get("/api/sessions/carol-s9", headers=AUTH)
            assert opened.status == 200


# ────────────────────────────────────────────────────────────────────────────
# 5. The flag itself is read from config.yaml (one end-to-end wiring check)
# ────────────────────────────────────────────────────────────────────────────


class TestConfigWiring:
    def test_enabled_flag_reads_the_real_config_file(self, tmp_path, monkeypatch):
        """``acl`` defaults must be off, and a written config.yaml must actually turn them on."""
        from hermes_cli.config import DEFAULT_CONFIG, load_config

        assert DEFAULT_CONFIG["acl"]["enabled"] is False
        assert DEFAULT_CONFIG["acl"]["identity_trust"] == "untrusted"

        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "acl:\n  enabled: true\n  identity_trust: trusted-proxy\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        cfg = load_config()
        assert cfg["acl"]["enabled"] is True
        assert hermes_acl_scope.acl_enabled() is True
        assert hermes_acl_scope.identity_trust() == "trusted-proxy"
