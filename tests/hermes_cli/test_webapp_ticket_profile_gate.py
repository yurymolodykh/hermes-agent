"""A socket ticket names only a profile its principal may SEE.

The hole: ``POST /api/webapp/socket-ticket?socket=gateway&profile=<name>`` minted a ticket for ANY
profile whose directory exists, and ``chat_ws.gateway_ws`` then bound the socket to that profile's
authority. One login's tab could therefore open another person's profile: the REST routes refuse a
foreign ``?profile=`` with a 404 (``hermes_acl_profiles.require_visible``), the ticket route did not.

Two directions are pinned, and the second one is the one that matters most:

* a principal that may not see the named profile is refused **404**, byte-identical to an unknown
  name (a 403 would confirm the roster);
* the loopback session token's principal — ``SESSION_TOKEN_OWNER``, provider ``session-token`` —
  is the HOST OWNER and stays allowed. A gate reading the bare request instead of the identity the
  ticket carries would see an anonymous caller and lock the owner out of their own tab.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import hermes_acl_identity
import hermes_acl_profiles as prof_acl
from gateway.owner_identity import host_owner_subject

_TOKEN = "c" * 43
ALICE = "person/alice"
FOREIGN = "01-bob"      # exists on disk, belongs to nobody in the registry
OWN_ROOT = "01-alice"   # exists on disk, is alice's


@pytest.fixture
def webapp_home(monkeypatch, tmp_path):
    """A loopback Webapp over ``<root>`` with two named profiles behind it."""
    import hermes_acl_bindings as hb
    from hermes_cli import web_server as server
    from hermes_cli.web_routers.webapp import router  # imports the module under test

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
    monkeypatch.setattr(server, "_SESSION_TOKEN", _TOKEN)
    hb.reset_cache()
    yield root
    hb.reset_cache()


@pytest.fixture
def multi_user(webapp_home, monkeypatch):
    """``acl.multi_user`` ON, alice owning ``01-alice`` and nothing shared."""
    import hermes_acl_bindings as hb

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

    # The step-3 login→subject rule, stubbed so no users file is needed (same seam
    # ``tests/hermes_cli/test_profile_route_owner_scope.py`` uses).
    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        return f"person/{login}" if login else fallback

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", _resolve)
    hb.reset_cache()
    yield path
    hb.reset_cache()


def _request(app, *, session=None):
    """The Request shape the route reads: ``app.state`` for the surface policy, ``state.session``
    for a signed-in principal (``None`` on the loopback-token path)."""
    state = SimpleNamespace(native_http_principal=None)
    if session is not None:
        state.session = session
    return SimpleNamespace(
        app=SimpleNamespace(state=app.state),
        state=state,
        headers={},
        url=SimpleNamespace(path="/api/webapp/socket-ticket"))


def _issue(app, profile, *, session=None, socket="gateway"):
    from hermes_cli.web_routers.webapp import issue_socket_ticket

    return asyncio.run(issue_socket_ticket(_request(app, session=session), socket=socket, profile=profile))


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_a_foreign_profile_is_refused_with_the_unknown_name_404(multi_user):
    """Alice's login may not mint a ticket for ``01-bob``."""
    from hermes_cli import web_server as server

    session = SimpleNamespace(user_id="alice", provider="basic", issuer="")
    with pytest.raises(HTTPException) as refused:
        _issue(server.app, FOREIGN, session=session)
    assert refused.value.status_code == 404
    assert refused.value.detail == f"Profile '{FOREIGN}' does not exist."


def test_the_same_refusal_shape_as_a_name_that_is_not_there_at_all(multi_user):
    from hermes_cli import web_server as server

    session = SimpleNamespace(user_id="alice", provider="basic", issuer="")
    with pytest.raises(HTTPException) as refused:
        _issue(server.app, "99-nobody", session=session)
    assert refused.value.status_code == 404


def test_a_login_may_mint_for_its_own_profile(multi_user):
    from hermes_cli import web_server as server

    session = SimpleNamespace(user_id="alice", provider="basic", issuer="")
    minted = _issue(server.app, OWN_ROOT, session=session)
    assert minted.status_code == 200
    assert set(json.loads(bytes(minted.body))) == {"ticket", "ttl_seconds"}


def test_the_loopback_owner_keeps_its_ticket(multi_user, webapp_home):
    """``SESSION_TOKEN_OWNER`` (provider ``session-token``) is the host owner: allowed, no lockout.

    End to end over the real route: the loopback bind presents only the session token, so the
    principal exists nowhere on the request and the gate must read the identity the ticket carries.
    """
    from hermes_cli import web_server as server

    assert host_owner_subject()  # the owner has an identity on this host
    client = TestClient(server.app, base_url="http://127.0.0.1")
    response = client.post("/api/webapp/socket-ticket",
                           params={"socket": "gateway", "profile": OWN_ROOT},
                           headers={"X-Hermes-Session-Token": _TOKEN})
    assert response.status_code == 200, response.text
    assert response.json()["ticket"]


def test_with_multi_user_off_every_existing_profile_still_mints(webapp_home, monkeypatch):
    """The shipped default must not change: no registry, no gate, the old answer."""
    from hermes_cli import web_server as server

    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: False)
    client = TestClient(server.app, base_url="http://127.0.0.1")
    response = client.post("/api/webapp/socket-ticket",
                           params={"socket": "gateway", "profile": FOREIGN},
                           headers={"X-Hermes-Session-Token": _TOKEN})
    assert response.status_code == 200, response.text


def test_a_missing_profile_is_still_a_404_and_mints_nothing(multi_user):
    from hermes_cli import web_server as server

    session = SimpleNamespace(user_id="alice", provider="basic", issuer="")
    with pytest.raises(HTTPException) as refused:
        _issue(server.app, "01-absent", session=session)
    assert refused.value.status_code == 404
