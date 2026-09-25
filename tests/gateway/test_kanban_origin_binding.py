"""Authenticated exact-origin binding: WebUI-signed claim -> owner-bound sub.

Covers the fixed critical defect from review #839: ``kanban_create`` used to
return ``subscribed=false`` for every ``api_server`` session, so no task
created from an Open WebUI chat could ever deliver its terminal question to
the originating chat. The verified path is: WebUI signs the authenticated
user into ``X-OpenWebUI-User-Jwt``; the api_server middleware resolves the
claim + ``X-OpenWebUI-Chat-Id`` into a request-scoped binding; the kanban
auto-subscribe path re-verifies ownership against the server-owned chat
registry and only then writes an owner-bound subscription. Everything else
stays fail closed (negative parity with
``tests/tools/test_kanban_tools.py::test_api_server_create_does_not_claim_an_unverified_delivery_route``).
"""
import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.api_server import APIServerAdapter
from gateway.session_context import clear_session_vars

RAW_CHAT = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"
BOUND_CHAT = "owui-" + RAW_CHAT
OWNER = "owner-A"
PROFILE = "01-orchestrator"
SECRET = "unit-test-forward-secret"

JWT_HEADER = "X-OpenWebUI-User-Jwt"
CHAT_HEADER = "X-OpenWebUI-Chat-Id"


def _mint(sub: str = OWNER, now: int | None = None, secret: str = SECRET) -> str:
    """Mint a token exactly like open_webui.utils.headers._mint_forward_user_jwt."""
    now = int(time.time()) if now is None else now

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({
        "sub": sub, "iss": "open-webui", "iat": now, "exp": now + 300,
    }).encode())
    sig = b64(hmac.new(secret.encode(), f"{head}.{payload}".encode(),
                       hashlib.sha256).digest())
    return f"{head}.{payload}.{sig}"


@pytest.fixture()
def owui_env(tmp_path, monkeypatch):
    """Isolated hermes home + kanban board + OWUI registry with one chat."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    registry = tmp_path / "webui.db"
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(registry))
    monkeypatch.setenv("HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED", "1")
    monkeypatch.setenv("FORWARD_USER_INFO_HEADER_JWT_SECRET", SECRET)
    with sqlite3.connect(registry) as db:
        db.execute(
            "CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        db.execute(
            "INSERT INTO chat VALUES (?, ?, ?)",
            (RAW_CHAT, OWNER, json.dumps({"history": {"messages": {
                "u": {"id": "u", "role": "user", "parentId": None}},
                "currentId": "u"}})))
    from hermes_cli import kanban_db as kb

    kb.init_db()
    return {"registry": registry, "tmp": tmp_path}


def _bind(origin: dict | str = ""):
    return APIServerAdapter._bind_api_server_session(
        chat_id=BOUND_CHAT, session_key="api-key", session_id=BOUND_CHAT,
        origin=origin if isinstance(origin, str) else json.dumps(origin))


def _binding(sub: str = OWNER, chat: str = BOUND_CHAT, profile: str | None = PROFILE) -> dict:
    origin = {"platform": "api_server", "chat_id": chat, "session_id": chat,
              "thread_id": None, "user_id": sub}
    if profile:
        origin["profile"] = profile
    return origin


# ---------------------------------------------------------------------------
# Claim verification (gateway/origin_binding.py)
# ---------------------------------------------------------------------------


class TestOriginBindingVerification:
    def test_valid_claim_and_chat_resolve(self, owui_env, monkeypatch):
        from gateway import origin_binding as ob

        origin = ob.resolve_request_origin(
            {JWT_HEADER: _mint(), CHAT_HEADER: RAW_CHAT}, PROFILE)
        assert origin is not None
        assert origin["chat_id"] == BOUND_CHAT
        assert origin["user_id"] == OWNER
        assert origin["profile"] == PROFILE
        assert origin["session_id"] == origin["chat_id"]
        assert origin["thread_id"] is None

    def test_no_secret_configured_fails_closed(self, owui_env, monkeypatch):
        from gateway import origin_binding as ob

        monkeypatch.delenv("FORWARD_USER_INFO_HEADER_JWT_SECRET", raising=False)
        assert ob.resolve_request_origin(
            {JWT_HEADER: _mint(), CHAT_HEADER: RAW_CHAT}, None) is None

    def test_foreign_secret_rejected(self, owui_env):
        from gateway import origin_binding as ob

        assert ob.resolve_request_origin(
            {JWT_HEADER: _mint(secret="not-the-secret"),
             CHAT_HEADER: RAW_CHAT}, None) is None

    def test_expired_claim_rejected(self, owui_env):
        from gateway import origin_binding as ob

        stale = _mint(now=int(time.time()) - 1000)
        assert ob.resolve_request_origin(
            {JWT_HEADER: stale, CHAT_HEADER: RAW_CHAT}, None) is None

    def test_non_uuid_chat_header_rejected(self, owui_env):
        from gateway import origin_binding as ob

        assert ob.resolve_request_origin(
            {JWT_HEADER: _mint(), CHAT_HEADER: "not-a-uuid"}, None) is None

    def test_missing_claim_is_unbound_not_error(self, owui_env):
        from gateway import origin_binding as ob

        assert ob.resolve_request_origin({CHAT_HEADER: RAW_CHAT}, None) is None


# ---------------------------------------------------------------------------
# HTTP middleware behaviour
# ---------------------------------------------------------------------------


def _middleware_app() -> web.Application:
    adapter = APIServerAdapter.__new__(APIServerAdapter)
    app = web.Application(middlewares=[adapter._make_origin_binding_middleware()])

    async def probe(request):
        from gateway.platforms.api_server import _api_request_origin_binding

        return web.json_response({"binding": _api_request_origin_binding.get()})

    app.router.add_get("/probe", probe)
    return app


@pytest.mark.asyncio
async def test_unsigned_request_stays_unbound(owui_env):
    app = _middleware_app()
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/probe")
        assert resp.status == 200
        assert (await resp.json())["binding"] == ""


@pytest.mark.asyncio
async def test_signed_request_carries_verified_binding(owui_env):
    from gateway.platforms.api_server import _api_request_profile

    token = _api_request_profile.set(PROFILE)
    try:
        app = _middleware_app()
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get(
                "/probe", headers={JWT_HEADER: _mint(), CHAT_HEADER: RAW_CHAT})
            assert resp.status == 200
            binding = json.loads((await resp.json())["binding"])
            assert binding["chat_id"] == BOUND_CHAT
            assert binding["user_id"] == OWNER
            assert binding["profile"] == PROFILE
    finally:
        _api_request_profile.reset(token)


@pytest.mark.asyncio
async def test_forged_claim_rejected_with_401(owui_env):
    app = _middleware_app()
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(
            "/probe", headers={JWT_HEADER: _mint(secret="wrong"),
                               CHAT_HEADER: RAW_CHAT})
        assert resp.status == 401
        resp2 = await cli.get(
            "/probe", headers={JWT_HEADER: "garbage.token.here",
                               CHAT_HEADER: RAW_CHAT})
        assert resp2.status == 401


# ---------------------------------------------------------------------------
# Owner-bound subscription (tools/kanban_origin_subscribe.py)
# ---------------------------------------------------------------------------


def _subs():
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, notifier_profiles=[PROFILE])
    finally:
        conn.close()


def test_verified_binding_subscribes_owner_bound(owui_env):
    from tools import kanban_tools as kt

    tokens = _bind(_binding())
    try:
        out = json.loads(kt._handle_create({"title": "origin task", "assignee": "peer"}))
        assert out["ok"] is True
        assert out["subscribed"] is True
    finally:
        clear_session_vars(tokens)
    subs = _subs()
    assert len(subs) == 1
    sub = subs[0]
    assert sub["platform"] == "api_server"
    assert sub["chat_id"] == BOUND_CHAT
    assert sub["user_id"] == OWNER
    assert sub["notifier_profile"] == PROFILE
    assert sub["delivery_mode"] == "notify+wake"


def test_unbound_session_still_fail_closed(owui_env):
    """Parity with the reviewer's negative: no claim -> no subscription."""
    from tools import kanban_tools as kt

    tokens = _bind("")
    try:
        out = json.loads(kt._handle_create({"title": "unbound", "assignee": "peer"}))
        assert out["ok"] is True
        assert out["subscribed"] is False
    finally:
        clear_session_vars(tokens)
    assert _subs() == []


def test_forged_owner_binding_refused(owui_env):
    """A binding claiming a foreign owner never survives the registry check."""
    from tools import kanban_tools as kt

    tokens = _bind(_binding(sub="owner-B"))
    try:
        out = json.loads(kt._handle_create({"title": "forged owner", "assignee": "peer"}))
        assert out["subscribed"] is False
    finally:
        clear_session_vars(tokens)
    assert _subs() == []


def test_unknown_chat_refused(owui_env):
    from tools import kanban_tools as kt

    other = "owui-11111111-2222-3333-4444-555555555555"
    tokens = _bind(_binding(chat=other))
    try:
        out = json.loads(kt._handle_create({"title": "foreign chat", "assignee": "peer"}))
        assert out["subscribed"] is False
    finally:
        clear_session_vars(tokens)
    assert _subs() == []


# ---------------------------------------------------------------------------
# End to end: create -> blocked question -> exact chat -> readback ACK
# ---------------------------------------------------------------------------


class _NoPush:
    supports_async_delivery = False
    sends = 0

    async def send(self, *a, **kw):
        _NoPush.sends += 1
        raise AssertionError("adapter send forbidden on the exact-origin path")


async def _tick(runner):
    original_sleep = asyncio.sleep

    async def sleep(delay, *a, **kw):
        if delay == 5:
            return
        runner._running = False
        await original_sleep(0)

    asyncio.sleep = sleep
    try:
        await runner._kanban_notifier_watcher(interval=1)
    finally:
        asyncio.sleep = original_sleep


def _runner():
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    r = GatewayRunner.__new__(GatewayRunner)
    r._running = True
    r.adapters = {Platform.API_SERVER: _NoPush()}
    r._kanban_sub_fail_counts = {}
    r._kanban_dispatcher_lock_handle = object()
    return r


def test_create_to_owner_chat_delivery_e2e(owui_env, monkeypatch):
    """Real input path: verified claim -> subscribe -> blocked -> outbox ->
    persisted owner chat -> cursor ACK. Mirrors
    tests/gateway/test_kanban_origin_notifier_integration.py but WITHOUT the
    hand-inserted subscription row: the subscription now comes from the
    actual kanban_create input path under test."""
    from gateway.origin_outbox import counts
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.setattr(
        "gateway.run.GatewayRunner._active_profile_name", lambda self: PROFILE)

    tokens = _bind(_binding())
    try:
        out = json.loads(kt._handle_create(
            {"title": "provider question", "assignee": "peer"}))
        assert out["subscribed"] is True
        task_id = out["task_id"]
        conn = kb.connect()
        try:
            kb.block_task(conn, task_id, reason="Choose provider explicitly before release")
        finally:
            conn.close()
    finally:
        clear_session_vars(tokens)

    outbox = owui_env["tmp"] / "delivery-outbox.sqlite"
    initial = _subs()[0]["last_event_id"]

    # Tick 1: stage exactly one pending outbox event; cursor stays pinned.
    asyncio.run(_tick(_runner()))
    assert counts(outbox)["pending"] == 1, counts(outbox)
    assert _subs()[0]["last_event_id"] == initial

    # Tick 2: drain writes into the owner chat and ACKs after readback.
    asyncio.run(_tick(_runner()))
    assert counts(outbox)["acknowledged"] == 1, counts(outbox)
    assert _NoPush.sends == 0
    assert _subs()[0]["last_event_id"] > initial

    with sqlite3.connect(owui_env["registry"]) as db:
        row = db.execute(
            "SELECT chat FROM chat WHERE id=? AND user_id=?",
            (RAW_CHAT, OWNER)).fetchone()
    assert row is not None
    chat = json.loads(row[0])
    assert any("Choose provider explicitly" in m.get("content", "")
               for m in chat["history"]["messages"].values())
