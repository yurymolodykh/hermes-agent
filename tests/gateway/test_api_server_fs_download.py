"""The api_server's read-only file surface: the chat's "Download" has a route.

Before this, the desktop chat's Download button asked ``GET /api/fs/download`` on the very
listener its conversation runs on, and the api_server platform registered no file route at all —
every download answered 404 while the preview of the same file worked (its bytes ride in the
transcript as a data URL). These tests drive the REAL aiohttp app over a real socket, with the
platform's own auth and the panel's own read rules.
"""

from __future__ import annotations

import base64
import hashlib
import os

import pytest
import pytest_asyncio

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

KEY = "test-api-server-key-1234567890"
AUTH = {"Authorization": f"Bearer {KEY}"}
PAYLOAD = b"<html><body>" + b"x" * 4096 + b"</body></html>"

# This repo's pytest config is `asyncio_mode=strict`, so each socket-driving test names its mark.


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME: the panel's helpers resolve config and profiles through it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in ("HERMES_DASHBOARD_FILES_ROOT", "ACM_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    return tmp_path


def _adapter(home) -> APIServerAdapter:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    adapter._api_key = KEY
    return adapter


def _client(adapter) -> TestClient:
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        if path.startswith("/api/fs"):
            app.router.add_route(method, path, handler)
    return TestClient(TestServer(app))


@pytest_asyncio.fixture()
async def client(home):
    test_client = _client(_adapter(home))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


def _write(home, name="report.html", data=PAYLOAD):
    target = home / name
    target.write_bytes(data)
    return target


# ── the route exists and serves the real bytes ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_serves_the_file_as_an_attachment(client, home):
    target = _write(home)
    response = await client.get("/api/fs/download", params={"path": str(target)}, headers=AUTH)

    assert response.status == 200
    body = await response.read()
    assert body == PAYLOAD
    assert hashlib.sha256(body).hexdigest() == hashlib.sha256(PAYLOAD).hexdigest()
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "report.html" in response.headers["Content-Disposition"]
    assert response.headers["Content-Type"].startswith("text/html")


@pytest.mark.asyncio
async def test_head_reports_the_size_without_the_body(client, home):
    target = _write(home)
    response = await client.head("/api/fs/download", params={"path": str(target)}, headers=AUTH)

    assert response.status == 200
    assert response.headers["Content-Length"] == str(len(PAYLOAD))
    assert await response.read() == b""


@pytest.mark.asyncio
async def test_read_data_url_returns_the_same_bytes(client, home):
    target = _write(home)
    response = await client.get(
        "/api/fs/read-data-url", params={"path": str(target)}, headers=AUTH)

    assert response.status == 200
    data_url = (await response.json())["dataUrl"]
    prefix, _, encoded = data_url.partition(",")
    assert prefix == "data:text/html;base64"
    assert hashlib.sha256(base64.b64decode(encoded)).hexdigest() == hashlib.sha256(PAYLOAD).hexdigest()


# ── the read rules are the panel's ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_without_the_api_key_is_401(client, home):
    target = _write(home)
    response = await client.get("/api/fs/download", params={"path": str(target)})
    assert response.status == 401


@pytest.mark.asyncio
async def test_download_of_a_missing_path_is_a_json_404(client, home):
    response = await client.get(
        "/api/fs/download", params={"path": str(home / "nope.html")}, headers=AUTH)
    assert response.status == 404
    assert (await response.json())["detail"]


@pytest.mark.asyncio
async def test_download_refuses_a_sensitive_file(client, home):
    secret = home / ".env"
    secret.write_text("API_SERVER_KEY=nope\n", encoding="utf-8")
    response = await client.get("/api/fs/download", params={"path": str(secret)}, headers=AUTH)
    assert response.status == 403


@pytest.mark.asyncio
async def test_download_refuses_a_credential_home_tree(client, tmp_path, monkeypatch):
    """A network listener does not hand out ``~/.ssh``/``~/.aws`` even where the panel's basename
    denylist alone would: the gateway's outbound credential denylist applies too."""
    import os
    import pathlib

    key = tmp_path / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    assert os.path.expanduser("~") == str(tmp_path)

    for route in ("/api/fs/download", "/api/fs/read-data-url"):
        response = await client.get(route, params={"path": str(key)}, headers=AUTH)
        assert response.status == 403, route


@pytest.mark.asyncio
async def test_download_refuses_a_path_outside_the_locked_root(client, home, monkeypatch):
    """With a managed files root configured, a path outside it is the panel's 403."""
    root = home / "managed"
    root.mkdir()
    outside = home / "outside.txt"
    outside.write_bytes(b"nope")
    monkeypatch.setenv("HERMES_DASHBOARD_FILES_ROOT", str(root))

    response = await client.get("/api/fs/download", params={"path": str(outside)}, headers=AUTH)
    assert response.status == 403


@pytest.mark.asyncio
async def test_download_refuses_a_file_over_the_size_ceiling(client, home, monkeypatch):
    import hermes_cli.web_server as panel

    monkeypatch.setattr(panel, "_MANAGED_FILE_MAX_BYTES", 16)
    target = _write(home)
    response = await client.get("/api/fs/download", params={"path": str(target)}, headers=AUTH)
    assert response.status == 413


@pytest.mark.asyncio
async def test_data_url_refuses_a_file_over_its_own_ceiling(client, home, monkeypatch):
    import hermes_cli.web_server as panel

    monkeypatch.setattr(panel, "_FS_DATA_URL_MAX_BYTES", 16)
    target = _write(home)
    response = await client.get(
        "/api/fs/read-data-url", params={"path": str(target)}, headers=AUTH)
    assert response.status == 413


@pytest.mark.asyncio
async def test_data_url_revalidates_an_unchanged_file(client, home):
    target = _write(home)
    first = await client.get("/api/fs/read-data-url", params={"path": str(target)}, headers=AUTH)
    etag = first.headers["ETag"]
    second = await client.get(
        "/api/fs/read-data-url", params={"path": str(target)},
        headers={**AUTH, "If-None-Match": etag})
    assert second.status == 304


# ── session_id and profile ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_session_id_is_a_404(client, home, monkeypatch):
    target = _write(home)
    from hermes_cli.web_routers import sessions as sessions_router

    async def missing(session_id, request=None, profile=None):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Session not found")

    monkeypatch.setattr(sessions_router, "get_session_detail", missing)
    response = await client.get(
        "/api/fs/download", params={"path": str(target), "session_id": "sess-other"}, headers=AUTH)
    assert response.status == 404


@pytest.mark.asyncio
async def test_an_owned_session_id_serves_the_file(client, home, monkeypatch):
    target = _write(home)
    seen = {}

    async def owned(session_id, request=None, profile=None):
        seen["session_id"] = session_id
        seen["profile"] = profile
        return {"id": session_id, "cwd": str(home)}

    monkeypatch.setattr(
        "hermes_cli.web_routers.sessions.get_session_detail", owned)
    response = await client.get(
        "/api/fs/download", params={"path": str(target), "session_id": "sess-mine"}, headers=AUTH)

    assert response.status == 200
    assert await response.read() == PAYLOAD
    assert seen == {"session_id": "sess-mine", "profile": None}


@pytest.mark.asyncio
async def test_an_unknown_profile_query_is_a_404(client, home):
    target = _write(home)
    response = await client.get(
        "/api/fs/download", params={"path": str(target), "profile": "nope-not-a-profile"},
        headers=AUTH)
    assert response.status == 404


# ── no write surface, and the panel is untouched ─────────────────────────────────────────


def test_the_platform_registers_no_write_route(home):
    rows = {(method, path) for method, path, _h in _adapter(home)._http_route_table()}
    assert ("GET", "/api/fs/download") in rows
    assert ("HEAD", "/api/fs/download") in rows
    assert ("GET", "/api/fs/read-data-url") in rows
    assert ("HEAD", "/api/fs/read-data-url") in rows
    for method, path in rows:
        if not path.startswith("/api/fs"):
            continue
        assert method in {"GET", "HEAD"}, f"{method} {path} is a write route on the chat listener"
        assert not path.endswith(("/upload", "/mkdir", "/delete"))


def test_the_fs_module_imports_the_panels_rules_rather_than_restating_them():
    """A restated read rule drifts: the module must not define its own sensitive-path or
    containment check."""
    from gateway.platforms import api_server_fs

    source = open(api_server_fs.__file__, encoding="utf-8").read()
    for own_rule in ("def _is_sensitive_path", "def _fs_regular_file", "def _hosted_fs_read_guard"):
        assert own_rule not in source, own_rule
    assert "panel._fs_regular_file" in source
    assert "panel._hosted_fs_read_guard" in source


@pytest.mark.asyncio
async def test_a_profile_query_matching_the_prefix_is_accepted(tmp_path, monkeypatch):
    """A ``/p/<profile>/`` request may name that same profile (the Desktop adds it to every call)
    and is refused when it names another one — a key must not read a neighbour's files."""
    root = tmp_path / "hermes"
    (root / "profiles" / "lokaj").mkdir(parents=True)
    (root / "profiles" / "other").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "profiles" / "lokaj" / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "profiles" / "other" / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    adapter = _adapter(root)
    # The named profile's own key: the platform resolves it through the profile runtime scope,
    # which a bare test app has no runner for.
    adapter._expected_api_key = lambda: KEY
    from gateway.platforms.api_server import _api_request_profile

    @web.middleware
    async def as_lokaj(request, handler):
        token = _api_request_profile.set("lokaj")
        try:
            return await handler(request)
        finally:
            _api_request_profile.reset(token)

    app = web.Application(middlewares=[as_lokaj])
    for method, path, handler in adapter._http_route_table():
        if path.startswith("/api/fs"):
            app.router.add_route(method, path, handler)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    target = root / "note.txt"
    target.write_bytes(b"mine")
    try:
        mine = await test_client.get(
            "/api/fs/download",
            params={"path": str(target), "profile": "lokaj"}, headers=AUTH)
        assert mine.status == 200
        # An EXISTING profile that is not this request's: the panel would serve it, so the
        # refusal here is the cross-profile one (403). A profile that does not exist at all is
        # the panel's own 404, checked separately below.
        other = await test_client.get(
            "/api/fs/download",
            params={"path": str(target), "profile": "other"}, headers=AUTH)
        assert other.status == 403
        unknown = await test_client.get(
            "/api/fs/download",
            params={"path": str(target), "profile": "someone-else"}, headers=AUTH)
        assert unknown.status == 404
    finally:
        await test_client.close()


def test_the_platform_serves_the_download_under_a_profile_prefix(home):
    """Every native route is mirrored under ``/p/<profile>/`` by ``connect()``; the paths this
    module registers must therefore be plain (the prefix is added around them)."""
    from gateway.platforms import api_server_fs

    rows = api_server_fs.http_routes(None)
    assert [p for _m, p, _h in rows] == [
        "/api/fs/download", "/api/fs/download", "/api/fs/read-data-url", "/api/fs/read-data-url"]
    assert all(not p.startswith("/p/") for _m, p, _h in rows)


def test_os_independent_path_name():
    """The data-URL mime lookup on an SSH workspace uses a POSIX path, whatever the host OS."""
    from gateway.platforms.api_server_fs import _remote_path

    assert _remote_path("/srv/report.html").name == "report.html"
    assert os.name in {"posix", "nt"}
