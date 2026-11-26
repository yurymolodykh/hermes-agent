"""The REAL client pair through the REAL mint route: no ``profile`` on the mint, ``?profile=`` on the dial.

``apps/desktop/src/lib/browser-transport.ts`` mints ``POST /api/webapp/socket-ticket?socket=host-terminal``
with ``profile: gateway ? profile : undefined`` — NO profile — and then dials
``/api/host-terminal?ticket=…&profile=<name>``. The ticket therefore carries no home, and the gate has
to decide from the ticket's own principal (``hermes_acl_profiles``), never from the query string.

``test_webapp_host_terminal_real_dial.py`` covers that pair with ``mint_socket_ticket`` called
directly. This file pins the same pair one step further out, at the route the browser actually calls:
``issue_socket_ticket`` is what reads ``?profile=`` and decides whether ``profile_home`` rides on the
ticket (``webapp.py``: ``if socket in (GATEWAY_SOCKET, HOST_TERMINAL_SOCKET) and profile``), so a mint
that a caller can only perform through the route is the shape that regressed.

A person's own ``?profile=`` opens their shell; another person's personal profile closes 4403 before
``ws.accept()``, and the host owner keeps every profile.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import gateway.run
import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_profiles as prof_acl
import hermes_cli.web_server
import tui_gateway.ws

PERSON = "01-person-a"
UNOWNED = "01-unowned"
_TOKEN = "e" * 43


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A Webapp surface hosting the REAL ``webapp.router``: mint and dial both go through it."""
    from hermes_cli import web_host_terminal, web_server, web_server_chat
    from hermes_cli.web_host_terminal_sessions import host_terminal_lifespan
    from hermes_cli.web_routers import webapp as _webapp_module

    assert _webapp_module.router

    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    for name in (PERSON, UNOWNED):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(web_server.app.state, "ui_surface", "webapp", raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "localhost", raising=False)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", _TOKEN)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(web_host_terminal, "shell_spec", lambda: (["/bin/sh", "-i"], "sh"))
    shells = []

    class IdleBridge:
        def read(self, _timeout):
            return None

        def write(self, _data, **_kw):
            return True

        def resize(self, _cols, _rows):
            return None

        def close(self):
            return None

        def is_alive(self):
            return False

    def spawn(*_args, **_kwargs):
        shells.append(IdleBridge())
        return shells[-1]

    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", spawn)
    # The shell-env builders stat the checkout's manifest.json (a real-home read the guard refuses);
    # the PATH of an idle shell is irrelevant to this test.
    from tools.environments import local as _local_env
    from tools.environments import local_pythonpath as _local_pp

    monkeypatch.setattr(_local_env, "_resolve_hermes_bin_dir", lambda: None)
    monkeypatch.setattr(_local_env, "_managed_runtime_path_entries", list)
    monkeypatch.setattr(_local_pp, "_get_hermes_site_packages", lambda _env: [])

    path = home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [{
            "slug": "person/person-a", "display_name": "Person A", "shared": False,
            "profiles": [PERSON], "subjects": [], "owner": "person/person-a", "source": "admin"}],
    }), encoding="utf-8")
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_identity, "credential_marker", lambda _login: (False, None))

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        return f"person/{login}" if login else fallback

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", _resolve)
    hb.reset_cache()

    def session_middleware(request, call_next):
        # A signed-in browser's session, as the Webapp's gate puts it on the request. The socket's
        # identity comes from the redeemed TICKET (``web_server_chat._consume_ws_ticket``), not from
        # here, so only the mint reads this. A request presenting the loopback session token is that
        # token's principal (the host owner), exactly as ``_ticket_principal`` resolves it.
        if (request.url.path == "/api/webapp/socket-ticket"
                and not request.headers.get("X-Hermes-Session-Token")):
            request.state.session = SimpleNamespace(
                user_id="person-a", provider="basic", issuer="")
        return call_next(request)

    host = FastAPI(lifespan=host_terminal_lifespan)
    # The policy reads ``request.app.state``: this app serves the real router, so it must carry the
    # Webapp surface the production app has.
    host.state.ui_surface = "webapp"
    host.state.bound_host = "localhost"
    host.state.auth_required = False
    host.middleware("http")(session_middleware)
    host.include_router(_webapp_module.router)
    yield SimpleNamespace(home=home, host=host, shells=shells)
    hb.reset_cache()


def _mint_through_the_route(host) -> str:
    """The browser's own call: ``socket=host-terminal`` and NO ``profile``."""
    client = TestClient(host, base_url="http://localhost")
    response = client.post("/api/webapp/socket-ticket", params={"socket": "host-terminal"})
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    from hermes_cli.dashboard_auth.ws_tickets import consume_ticket

    # One-use: the assertion spends a second ticket, never the one the dial will redeem.
    assert "profile_home" not in consume_ticket(ticket)  # exactly the shape the browser sends
    second = client.post("/api/webapp/socket-ticket", params={"socket": "host-terminal"})
    assert second.status_code == 200, second.text
    return second.json()["ticket"]


def _metadata(ws):
    from hermes_cli import web_host_terminal

    text = ws.receive_text()
    assert text.startswith(web_host_terminal.META_PREFIX)
    return json.loads(text.removeprefix(web_host_terminal.META_PREFIX))


def test_the_real_mint_route_then_its_own_profile_opens(served):
    ticket = _mint_through_the_route(served.host)
    with TestClient(served.host) as shells:
        with shells.websocket_connect(
                f"ws://localhost/api/host-terminal?ticket={ticket}&profile={PERSON}") as ws:
            assert _metadata(ws)["terminalId"]
    assert len(served.shells) == 1


def test_the_real_mint_route_then_a_foreign_profile_closes_4403(served):
    ticket = _mint_through_the_route(served.host)
    with TestClient(served.host) as shells:
        with pytest.raises(WebSocketDisconnect) as closed:
            with shells.websocket_connect(
                    f"ws://localhost/api/host-terminal?ticket={ticket}&profile={UNOWNED}") as ws:
                ws.receive_text()
    assert closed.value.code == 4403
    assert served.shells == []


def test_the_owner_mints_through_the_route_and_opens_any_profile(served):
    """The loopback session token is the host owner: the same pair, no ``profile`` on the mint."""
    client = TestClient(served.host, base_url="http://localhost")
    response = client.post("/api/webapp/socket-ticket", params={"socket": "host-terminal"},
                           headers={"X-Hermes-Session-Token": _TOKEN})
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    with TestClient(served.host) as shells:
        with shells.websocket_connect(
                f"ws://localhost/api/host-terminal?ticket={ticket}&profile={UNOWNED}") as ws:
            assert _metadata(ws)["terminalId"]
    assert len(served.shells) == 1
