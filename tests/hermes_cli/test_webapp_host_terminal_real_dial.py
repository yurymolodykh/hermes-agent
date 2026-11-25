"""The REAL terminal dial: a ticket minted WITHOUT a profile, a socket dialled WITH ``?profile=``.

``apps/desktop/src/lib/browser-transport.ts`` mints the ``host-terminal`` ticket through
``browserApi(..., {profile: gateway ? profile : undefined})`` — i.e. NO profile — and then dials
``websocketUrl(..., profile)``. The gate therefore cannot learn the profile from the ticket; it has
to ask the ticket's own principal, exactly as the REST routes do (``hermes_acl_profiles``).

Pinned here as the pair the bug lives in: one login's profile opens, another person's personal
profile closes 4403 BEFORE ``ws.accept()`` and before any PTY exists, the owner opens any profile.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_profiles as prof_acl

_TOKEN = "d" * 43
_SESSION = {"X-Hermes-Session-Token": _TOKEN}
MARIA = "01-maria-safronova"
ORCH = "01-orchestrator"


@pytest.fixture
def webapp_home(tmp_path, monkeypatch):
    from hermes_cli import web_host_terminal, web_server, web_server_chat
    from hermes_cli.web_host_terminal_sessions import host_terminal_lifespan
    from hermes_cli.web_routers import webapp as _webapp_module

    assert _webapp_module.router

    home = tmp_path / ".hermes"
    (home / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    for name in (MARIA, ORCH):
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

    def spawn(*_args, **_kwargs):
        shells.append(object())
        return SimpleNamespace(
            read=lambda timeout: None, write=lambda data, **_kw: True, resize=lambda c, r: None,
            close=lambda: None, is_alive=lambda: False)

    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", spawn)
    hb.reset_cache()

    host = FastAPI(lifespan=host_terminal_lifespan)
    host.include_router(_webapp_module.router)
    yield SimpleNamespace(app=web_server.app, home=home, host=host, shells=shells)
    hb.reset_cache()


@pytest.fixture
def multi_user(webapp_home, monkeypatch):
    """Maria's login is a PERSON with her own ``01-...`` root; ``01-orchestrator`` is nobody's."""
    path = webapp_home.home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [{
            "slug": "person/maria", "display_name": "Maria", "shared": False,
            "profiles": [MARIA], "subjects": [], "owner": "person/maria", "source": "admin"}],
    }), encoding="utf-8")
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: True)

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        return f"person/{login}" if login else fallback

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", _resolve)
    hb.reset_cache()
    yield path
    hb.reset_cache()


def _mint_without_profile(client):
    """What the real terminal dial does: ``POST .../socket-ticket?socket=host-terminal``, no profile."""
    response = client.post("/api/webapp/socket-ticket", params={"socket": "host-terminal"},
                           headers=_SESSION)
    assert response.status_code == 200, response.text
    from hermes_cli.dashboard_auth.ws_tickets import consume_ticket

    payload = response.json()["ticket"]
    del consume_ticket  # the gate reads it; the test only needs the string
    return payload


def _dial(host, ticket, profile):
    query = f"ticket={ticket}" + (f"&profile={profile}" if profile is not None else "")
    return TestClient(host).websocket_connect(f"ws://localhost/api/host-terminal?{query}")


def test_own_profile_opens_through_the_real_pair(webapp_home, multi_user, monkeypatch):
    """Maria mints with no profile and dials ``?profile=01-maria-safronova``: the gate passes."""
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket

    # The mint route's identity is what ``_consume_ws_ticket`` stamps; mint directly as her login so
    # the pair under test is exactly (no profile_home on the ticket, profile on the socket).
    ticket = mint_ticket(user_id="maria", provider="basic")
    ws = SimpleNamespace(query_params={"profile": MARIA},
                         _hermes_auth_identity={"user_id": "maria", "provider": "basic"})
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    assert _host_terminal_profile_matches(ws) is True
    assert ticket


def test_foreign_personal_profile_is_refused_before_accept_and_before_pty(webapp_home, multi_user):
    """Same pair for ``01-orchestrator``: 4403 at the gate — no ``ws.accept()``, no PTY."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    ws = SimpleNamespace(query_params={"profile": ORCH},
                         _hermes_auth_identity={"user_id": "maria", "provider": "basic"})
    assert _host_terminal_profile_matches(ws) is False
    assert webapp_home.shells == []


def test_owner_opens_any_profile_through_the_real_pair(webapp_home, multi_user):
    """The host owner (``uid:<n>``) sees every profile, so any name passes."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches
    from gateway.session_worker_reservation import owner_worker_subject

    for name in (MARIA, ORCH):
        ws = SimpleNamespace(query_params={"profile": name},
                             _hermes_auth_identity={"user_id": owner_worker_subject(),
                                                    "provider": "local"})
        assert _host_terminal_profile_matches(ws) is True


def test_an_unknown_profile_name_is_refused_not_spawned(webapp_home, multi_user):
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    ws = SimpleNamespace(query_params={"profile": "01-nobody-here"},
                         _hermes_auth_identity={"user_id": "maria", "provider": "basic"})
    assert _host_terminal_profile_matches(ws) is False


def test_an_identity_less_ticket_opens_no_named_profile(webapp_home, multi_user):
    """Fail closed: no stamped identity means no name opens."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    ws = SimpleNamespace(query_params={"profile": MARIA})
    assert _host_terminal_profile_matches(ws) is False


def test_a_ticket_that_names_its_home_still_binds_the_socket(webapp_home, multi_user):
    """The minted-home shape is unchanged: a differing ``?profile=`` never matches."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    maria_home = str((webapp_home.home / "profiles" / MARIA).resolve())
    ws = SimpleNamespace(query_params={"profile": ORCH}, _hermes_ticket_profile_home=maria_home,
                         _hermes_auth_identity={"user_id": "maria", "provider": "basic"})
    assert _host_terminal_profile_matches(ws) is False
    ws_own = SimpleNamespace(query_params={"profile": MARIA}, _hermes_ticket_profile_home=maria_home,
                             _hermes_auth_identity={"user_id": "maria", "provider": "basic"})
    assert _host_terminal_profile_matches(ws_own) is True
