"""The REAL terminal dial: a ticket minted WITHOUT a profile, a socket dialled WITH ``?profile=``.

``apps/desktop/src/lib/browser-transport.ts`` mints the ``host-terminal`` ticket through
``browserApi(..., {profile: gateway ? profile : undefined})`` — i.e. NO profile — and then dials
``websocketUrl(..., profile)``. The gate therefore cannot learn the profile from the ticket; it has
to ask the ticket's own principal, exactly as the REST routes do (``hermes_acl_profiles``).

Pinned here through the REAL route (``/api/host-terminal`` on a live host app whose ticket is
redeemed by ``web_server_chat._consume_ws_ticket``), never by handing the matcher a hand-built
socket: one login's own profile opens, another person's personal profile closes 4403 BEFORE
``ws.accept()`` and before any PTY exists, and the host owner opens any profile by name.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_profiles as prof_acl

# Imported at module level on purpose: a shell spawn reaches ``resolve_argv`` -> ``gateway.run``,
# whose import runs ``hermes_bootstrap._settle_interrupted_update``. Done from inside a test body it
# happens AFTER the autouse HomeIOGuard is installed, and its ``interrupted_pull_marker`` stat
# resolves the worktree's git dir under the real home (TEST BUG, not a code defect). Module-level
# imports happen during collection, before the guard.
import gateway.run  # noqa: F401
import hermes_cli.web_server  # noqa: F401
import tui_gateway.ws  # noqa: F401

MARIA = "01-maria-safronova"
ORCH = "01-orchestrator"


@pytest.fixture
def webapp_home(tmp_path, monkeypatch):
    """A Webapp over ``<tmp>/.hermes`` with Maria's root and the orchestrator's beside it."""
    from hermes_cli import web_host_terminal, web_server, web_server_chat
    from hermes_cli.web_host_terminal_sessions import host_terminal_lifespan
    from hermes_cli.web_routers import webapp as _webapp_module

    assert _webapp_module.router  # the module under test is imported

    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
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
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "d" * 43)
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
    # The shell env builder prepends the install dir to PATH, which stats the repo's
    # ``manifest.json`` — a real-home read the guard (rightly) refuses. The shell's PATH does not
    # matter to this test, so keep the resolver from touching the checkout.
    from tools.environments import local as _local_env

    monkeypatch.setattr(_local_env, "_resolve_hermes_bin_dir", lambda: None)
    hb.reset_cache()

    host = FastAPI(lifespan=host_terminal_lifespan)
    host.include_router(_webapp_module.router)
    yield SimpleNamespace(home=home, host=host, shells=shells)
    hb.reset_cache()


@pytest.fixture
def multi_user(webapp_home, monkeypatch):
    """Maria is a PERSON with her own ``01-...`` root; ``01-orchestrator`` belongs to nobody."""
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

    # A "basic" ticket is checked against the users file; it holds no record for this login, so
    # the credential check answers "nothing to revoke" — the real mint does the same on a host
    # with no users file, which is this fixture.
    monkeypatch.setattr(hermes_acl_identity, "credential_marker", lambda _login: (False, None))
    hb.reset_cache()
    yield path
    hb.reset_cache()


def _mint_without_profile(identity: dict) -> str:
    """The ticket the real terminal dial mints: ``socket=host-terminal``, NO profile.

    ``webapp.issue_socket_ticket`` passes ``profile_home`` only ``if profile`` — so a client that
    mints without one carries no home, which is exactly the shape under test. Minted through the
    same ``mint_socket_ticket`` the route calls, with Maria's own login as the identity.
    """
    from hermes_cli.dashboard_auth.ws_tickets import consume_ticket
    from hermes_cli.web_routers.webapp import mint_socket_ticket
    from hermes_cli.web_session_sockets import HOST_TERMINAL_SOCKET

    ticket = mint_socket_ticket(identity, HOST_TERMINAL_SOCKET)
    claims = consume_ticket(ticket)
    assert claims["socket"] == HOST_TERMINAL_SOCKET
    assert "profile_home" not in claims, claims  # the mint named no home: the bug's real shape
    return mint_socket_ticket(identity, HOST_TERMINAL_SOCKET)


def _metadata(ws):
    from hermes_cli import web_host_terminal

    text = ws.receive_text()
    assert text.startswith(web_host_terminal.META_PREFIX)
    return json.loads(text.removeprefix(web_host_terminal.META_PREFIX))


def _dial_and_expect_refusal(host, ticket: str, profile: str):
    with TestClient(host) as shells:
        with pytest.raises(WebSocketDisconnect) as closed:
            with shells.websocket_connect(
                    f"ws://localhost/api/host-terminal?ticket={ticket}&profile={profile}") as ws:
                ws.receive_text()
    return closed.value.code


def test_own_profile_opens_through_the_real_pair(webapp_home, multi_user):
    """Maria mints with no profile and dials ``?profile=01-maria-safronova``: the shell opens."""
    ticket = _mint_without_profile({"user_id": "maria", "provider": "basic"})
    with TestClient(webapp_home.host) as shells:
        with shells.websocket_connect(
                f"ws://localhost/api/host-terminal?ticket={ticket}&profile={MARIA}") as ws:
            assert _metadata(ws)["terminalId"]
    assert len(webapp_home.shells) == 1


def test_foreign_personal_profile_is_refused_before_accept_and_before_pty(webapp_home, multi_user):
    """Same pair for ``01-orchestrator``: 4403, no ``ws.accept()``, no PTY."""
    ticket = _mint_without_profile({"user_id": "maria", "provider": "basic"})
    assert _dial_and_expect_refusal(webapp_home.host, ticket, ORCH) == 4403
    assert webapp_home.shells == []


def test_owner_opens_any_profile_through_the_real_pair(webapp_home, multi_user):
    """The host owner sees every profile, so either name opens a shell."""
    # The loopback session token's principal is the host owner on this host.
    identity = {"user_id": "legacy-token-owner", "provider": "session-token"}
    for name in (MARIA, ORCH):
        assert _mint_without_profile(identity)  # minted with no home, as the dial does
        ticket = _mint_without_profile(identity)
        with TestClient(webapp_home.host) as shells:
            with shells.websocket_connect(
                    f"ws://localhost/api/host-terminal?ticket={ticket}&profile={name}") as ws:
                assert _metadata(ws)["terminalId"]
    assert len(webapp_home.shells) == 2


def test_an_unknown_profile_name_is_refused_not_spawned(webapp_home, multi_user):
    ticket = _mint_without_profile({"user_id": "maria", "provider": "basic"})
    assert _dial_and_expect_refusal(webapp_home.host, ticket, "01-nobody-here") == 4403
    assert webapp_home.shells == []


def test_an_identity_less_socket_opens_no_named_profile(webapp_home, multi_user):
    """Fail closed: a socket the gate stamped no identity onto opens nothing by name."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    ws = SimpleNamespace(query_params={"profile": MARIA})
    assert _host_terminal_profile_matches(ws) is False


def test_the_launch_home_is_the_owners_alone_under_multi_user(webapp_home, multi_user):
    """No ``?profile=`` at all: a person's tab gets nothing, the owner keeps the launch shell."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    maria = SimpleNamespace(query_params={},
                            _hermes_auth_identity={"user_id": "maria", "provider": "basic"})
    assert _host_terminal_profile_matches(maria) is False
    owner = SimpleNamespace(query_params={},
                            _hermes_auth_identity={"user_id": "legacy-token-owner",
                                                   "provider": "session-token"})
    assert _host_terminal_profile_matches(owner) is True


def test_single_user_keeps_the_launch_home(webapp_home, monkeypatch):
    """With ``acl.multi_user`` off the route behaves exactly as before: the launch home opens."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: False)
    ws = SimpleNamespace(query_params={}, _hermes_auth_identity={"user_id": "anyone",
                                                                "provider": "basic"})
    assert _host_terminal_profile_matches(ws) is True


def test_a_ticket_that_names_its_home_still_binds_the_socket(webapp_home, multi_user):
    """The minted-home shape is unchanged: a differing ``?profile=`` never matches."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    maria_home = str((webapp_home.home / "profiles" / MARIA).resolve())
    identity = {"user_id": "maria", "provider": "basic"}
    ws = SimpleNamespace(query_params={"profile": ORCH}, _hermes_ticket_profile_home=maria_home,
                         _hermes_auth_identity=identity)
    assert _host_terminal_profile_matches(ws) is False
    ws_own = SimpleNamespace(query_params={"profile": MARIA}, _hermes_ticket_profile_home=maria_home,
                             _hermes_auth_identity=identity)
    assert _host_terminal_profile_matches(ws_own) is True
    ws_none = SimpleNamespace(query_params={}, _hermes_ticket_profile_home=maria_home,
                              _hermes_auth_identity=identity)
    assert _host_terminal_profile_matches(ws_none) is True
