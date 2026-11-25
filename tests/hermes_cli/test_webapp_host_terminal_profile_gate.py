"""A ``host-terminal`` socket ticket names a profile its principal may SEE, and the socket opens that one.

Two holes are pinned here, both the same shape as the ``gateway`` ticket's:

* ``SOCKET_TICKETS[HOST_TERMINAL_SOCKET]`` used to be ``("host_terminal", False)``, so the mint
  route skipped ``_require_profile_for_principal`` and ``profile_home`` was never carried: one
  login's tab could mint a terminal ticket for another person's profile and
  ``web_host_terminal_sessions._request_home('?profile=')`` resolved ANY profile by name.
* the socket itself took the profile from the query string only. Now the redeemed ticket supplies
  the home (``_ticket_home``), and a ``?profile=`` that names something else is refused before any
  shell is spawned (``webapp.host_terminal_ws`` -> ``_host_terminal_profile_matches``).

The host owner is not locked out: ``hermes_acl_profiles.require_visible`` lets the owner see every
profile, exactly as the REST routes do.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_profiles as prof_acl

_TOKEN = "c" * 43
ALICE = "person/alice"
OWN_ROOT = "01-alice"
FOREIGN = "01-bob"


@pytest.fixture
def webapp_home(monkeypatch, tmp_path):
    import hermes_acl_bindings as bindings
    from hermes_cli import web_server as server
    from hermes_cli.web_routers.webapp import router  # noqa: F401 — imports the module under test

    root = tmp_path / "hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    for name in (OWN_ROOT, FOREIGN):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(server.app.state, "ui_surface", "webapp", raising=False)
    monkeypatch.setattr(server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(server.app.state, "auth_required", False, raising=False)
    # ``ui_surface='webapp'`` above already selects the Webapp policy, which serves host-terminal.
    monkeypatch.setattr(server, "_SESSION_TOKEN", _TOKEN)
    bindings.reset_cache()
    yield root
    bindings.reset_cache()


@pytest.fixture
def multi_user(webapp_home, monkeypatch):
    path = webapp_home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [{
            "slug": ALICE, "display_name": "Alice", "shared": False,
            "profiles": [OWN_ROOT], "subjects": [], "owner": ALICE, "source": "admin"}],
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


def _request(app, *, session=None):
    state = SimpleNamespace(native_http_principal=None)
    if session is not None:
        state.session = session
    return SimpleNamespace(
        app=SimpleNamespace(state=app.state), state=state, headers={},
        url=SimpleNamespace(path="/api/webapp/socket-ticket"))


def _issue(app, profile, *, session=None):
    from hermes_cli.web_routers.webapp import issue_socket_ticket
    from hermes_cli.web_session_sockets import HOST_TERMINAL_SOCKET

    return asyncio.run(issue_socket_ticket(
        _request(app, session=session), socket=HOST_TERMINAL_SOCKET, profile=profile))


def _ticket_payload(ticket):
    """The ticket's own claims, read the way the WS gate reads them."""
    from hermes_cli.dashboard_auth.ws_tickets import consume_ticket

    return consume_ticket(ticket)


def test_an_employee_may_not_mint_a_terminal_ticket_for_a_foreign_profile(multi_user):
    """Alice's login asking for ``01-bob`` gets the unknown-name 404, like the REST routes."""
    from hermes_cli import web_server as server

    session = SimpleNamespace(user_id="alice", provider="basic", issuer="")
    with pytest.raises(HTTPException) as refused:
        _issue(server.app, FOREIGN, session=session)
    assert refused.value.status_code == 404


def test_the_owner_mints_a_ticket_that_carries_the_profiles_home(multi_user, webapp_home):
    """The loopback token is the host owner: 200, and the ticket names the profile's home."""
    from fastapi.testclient import TestClient

    from hermes_cli import web_server as server

    client = TestClient(server.app, base_url="http://127.0.0.1")
    response = client.post("/api/webapp/socket-ticket",
                           params={"socket": "host-terminal", "profile": FOREIGN},
                           headers={"X-Hermes-Session-Token": _TOKEN})
    assert response.status_code == 200, response.text
    profile_home = _ticket_payload(response.json()["ticket"]).get("profile_home")
    assert profile_home is not None
    assert Path(profile_home).resolve() == (webapp_home / "profiles" / FOREIGN).resolve()


class _WS:
    """The slice of a WebSocket ``_host_terminal_profile_matches`` reads."""

    def __init__(self, query: dict, ticket_home: str | None):
        self.query_params = query
        if ticket_home is not None:
            self._hermes_ticket_profile_home = ticket_home


def test_a_request_naming_another_profile_than_its_ticket_is_refused(webapp_home):
    """The socket's home comes from the ticket; a differing ``?profile=`` never matches."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    alice_home = str((webapp_home / "profiles" / OWN_ROOT).resolve())
    ws = _WS({"profile": FOREIGN}, alice_home)
    assert _host_terminal_profile_matches(ws) is False


def test_a_request_naming_its_own_ticket_profile_is_allowed(webapp_home):
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    alice_home = str((webapp_home / "profiles" / OWN_ROOT).resolve())
    ws = _WS({"profile": OWN_ROOT}, alice_home)
    assert _host_terminal_profile_matches(ws) is True


def test_a_ticket_that_names_no_home_opens_only_the_launch_profile(webapp_home):
    """A ticket minted before the gate existed cannot name a profile, so it opens none."""
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    assert _host_terminal_profile_matches(_WS({}, None)) is True
    assert _host_terminal_profile_matches(_WS({"profile": FOREIGN}, None)) is False


def test_the_shell_opens_the_ticket_home_not_the_query_string(webapp_home):
    """``web_host_terminal_sessions._shell_home`` prefers the ticket over ``?profile=``."""
    from hermes_cli.web_host_terminal_sessions import _shell_home

    alice_home = str((webapp_home / "profiles" / OWN_ROOT).resolve())
    assert _shell_home(_WS({"profile": FOREIGN}, alice_home)) == Path(alice_home)
    assert _shell_home(_WS({"profile": OWN_ROOT}, None)) == Path(alice_home)
