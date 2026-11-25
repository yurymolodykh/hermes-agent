"""A host-terminal shell opens only in a profile its principal may SEE.

The hole (P0, card t_7e82fbdc): ``POST /api/webapp/socket-ticket?socket=host-terminal`` was
registered with ``checks_profile=False``, so the mint never asked whether the caller may use the
profile it named, and ``HostTerminalRegistry.create`` resolved ``?profile=`` with no ACL either.
An employee's tab therefore minted a ticket for ``01-orchestrator`` and got a PTY whose
``$HERMES_HOME`` was that profile — readable foreign conversations.

Three directions are pinned:

* the mint refuses a foreign profile with the same **404** an unknown name gets (and no ticket);
* the socket itself refuses a foreign ``profile`` with **4403 before accept**, so the fix also
  holds for a ticket minted before the policy changed;
* the host owner is never locked out (any profile mints and dials), and the caller's own profile
  keeps working.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import hermes_acl_identity
import hermes_acl_profiles as prof_acl

_TOKEN = "c" * 43
OWN = "01-alice"
FOREIGN = "01-bob"


@pytest.fixture
def webapp_home(monkeypatch, tmp_path):
    import hermes_acl_bindings as hb
    from hermes_cli import web_server as server
    from hermes_cli.web_routers.webapp import router  # noqa: F401  (imports the module under test)

    root = tmp_path / "hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    for name in (OWN, FOREIGN):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(server.app.state, "ui_surface", "webapp", raising=False)
    monkeypatch.setattr(server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(server, "_SESSION_TOKEN", _TOKEN)
    hb.reset_cache()
    yield root
    hb.reset_cache()


@pytest.fixture
def multi_user(webapp_home, monkeypatch):
    import hermes_acl_bindings as hb

    path = webapp_home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [{
            "slug": "person/alice", "display_name": "Alice", "shared": False,
            "profiles": [OWN], "subjects": [], "owner": "person/alice", "source": "admin"}],
    }), encoding="utf-8")
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_identity, "resolve_subject",
                        lambda value, fallback="": (f"person/{value}" if str(value or "").strip() else fallback))
    hb.reset_cache()
    yield path
    hb.reset_cache()


def _request(app, *, session=None):
    state = SimpleNamespace(native_http_principal=None)
    if session is not None:
        state.session = session
    return SimpleNamespace(app=SimpleNamespace(state=app.state), state=state, headers={},
                           url=SimpleNamespace(path="/api/webapp/socket-ticket"))


def _issue(app, profile, *, session=None, socket="host-terminal"):
    from hermes_cli.web_routers.webapp import issue_socket_ticket

    return asyncio.run(issue_socket_ticket(_request(app, session=session), socket=socket, profile=profile))


def _ws(profile, *, identity):
    """The WebSocket shape ``_terminal_profile_refused`` reads."""
    return SimpleNamespace(_hermes_auth_identity=identity,
                           query_params={} if profile is None else {"profile": profile})


# ── the mint ─────────────────────────────────────────────────────────────────


def test_a_foreign_profile_is_refused_on_the_mint_with_the_unknown_name_404(multi_user):
    from hermes_cli import web_server as server

    session = SimpleNamespace(user_id="alice", provider="basic", issuer="")
    with pytest.raises(HTTPException) as refused:
        _issue(server.app, FOREIGN, session=session)
    assert refused.value.status_code == 404
    assert "does not exist" in refused.value.detail


def test_the_callers_own_profile_mints(multi_user):
    from hermes_cli import web_server as server

    session = SimpleNamespace(user_id="alice", provider="basic", issuer="")
    response = _issue(server.app, OWN, session=session)
    assert json.loads(response.body)["ticket"]


def test_the_host_owner_mints_a_shell_on_any_profile(multi_user):
    """``SESSION_TOKEN_OWNER`` (provider ``session-token``) is the host owner: no lockout.

    End to end over the real route with the loopback session token, so the gate reads the
    identity the ticket CARRIES (the request itself has no session) — the shape that would
    lock the owner out if the gate read the bare request.
    """
    from fastapi.testclient import TestClient

    from hermes_cli import web_server as server

    client = TestClient(server.app, base_url="http://127.0.0.1")
    for profile in (OWN, FOREIGN):
        response = client.post("/api/webapp/socket-ticket",
                               params={"socket": "host-terminal", "profile": profile},
                               headers={"X-Hermes-Session-Token": _TOKEN})
        assert response.status_code == 200, (profile, response.text)
        assert response.json()["ticket"]


# ── the socket (holds for a ticket minted before the policy changed) ─────────


def test_the_socket_refuses_a_foreign_profile_before_accept(multi_user):
    from hermes_cli.web_routers.webapp import _terminal_profile_refused

    assert _terminal_profile_refused(_ws(FOREIGN, identity={"user_id": "alice", "provider": "basic"}))
    assert not _terminal_profile_refused(_ws(OWN, identity={"user_id": "alice", "provider": "basic"}))


def test_the_socket_refuses_the_launch_home_to_a_non_owner(multi_user, webapp_home):
    """No ``profile`` means the gateway's launch home — scoped like any other name."""
    from hermes_cli.web_routers.webapp import _terminal_profile_refused

    assert _terminal_profile_refused(_ws(None, identity={"user_id": "alice", "provider": "basic"}))


def test_an_owner_identity_passes_the_socket_gate(multi_user):
    from hermes_cli.web_routers.webapp import _terminal_profile_refused
    from gateway.owner_identity import host_owner_subject

    owner = {"user_id": host_owner_subject(), "provider": "session-token"}
    for profile in (OWN, FOREIGN, None):
        assert not _terminal_profile_refused(_ws(profile, identity=owner))


def test_a_credential_less_dial_is_left_to_the_upgrade_gate(multi_user):
    from hermes_cli.web_routers.webapp import _terminal_profile_refused

    assert not _terminal_profile_refused(_ws(FOREIGN, identity=None))
