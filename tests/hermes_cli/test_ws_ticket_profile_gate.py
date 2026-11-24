"""The /api/ws socket refuses to be rebound to a profile its subject may not use.

``gateway_ws`` overwrites ``identity['profile_id'] = served.profile_id`` with the profile the ticket
names. ``gateway/session_authority.authorize`` then compares ``actor.profile_id`` with that same
overwritten value, so the check could never fail: a tab signed in as ``person/alice`` presenting a
ticket for Bob's profile was served Bob's sessions. The gate now runs BEFORE the rebinding, asking
the same ``hermes_acl_profiles.allows`` the REST ``?profile=`` routes ask, and closes **4403**.

The ticket is minted directly through ``mint_socket_ticket``: the REST route refuses a foreign name
already (``tests/hermes_cli/test_webapp_ticket_profile_gate.py``), so this pins the socket-side gate
that covers a ticket minted before the policy changed, or by another route.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import hermes_acl_identity
import hermes_acl_profiles as prof_acl

_TOKEN = "d" * 43
ALICE = "person/alice"
ALICE_HOME = "01-alice"
FOREIGN_HOME = "01-bob"


@pytest.fixture
def serving(monkeypatch, tmp_path):
    """A multiplexed gateway serving ``<root>`` and ``profiles/01-bob``, with alice owning ``01-alice``."""
    import hermes_acl_bindings as hb
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority
    from hermes_cli import web_server as server
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    root = tmp_path / "hr"  # not `.hermes`: the state-db guard treats that as the live root
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "profiles").mkdir()
    homes = {}
    for name in (ALICE_HOME, FOREIGN_HOME):
        home = root / "profiles" / name
        home.mkdir()
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        homes[name] = home
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))

    registry = SessionAuthorities(root, multiplexed=True)
    runner = SimpleNamespace(_draining=False, session_authorities=registry)
    authorities, dbs = {}, {}
    for name, home in (("default", root), (ALICE_HOME, homes[ALICE_HOME]), (FOREIGN_HOME, homes[FOREIGN_HOME])):
        db = dbs[name] = SessionDB(home / "state.db")
        authorities[name] = SessionAuthority(
            runner, profile_id=str(home), instance_id="ws-profile-gate", db=db,
            epoch=begin_runtime_epoch(db, instance_id="ws-profile-gate"))
        registry.add(home, authorities[name], name=name)
    runner.session_authority = authorities["default"]
    monkeypatch.setattr(server.app.state, "session_authority", authorities["default"], raising=False)
    monkeypatch.setattr(server.app.state, "ui_surface", "webapp", raising=False)
    monkeypatch.setattr(server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(server, "_SESSION_TOKEN", _TOKEN)
    monkeypatch.setattr(server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)

    path = root / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [{"slug": ALICE, "display_name": "Alice", "shared": False,
                     "profiles": [ALICE_HOME], "subjects": [], "owner": ALICE, "source": "admin"}],
    }), encoding="utf-8")
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: True)
    hb.reset_cache()
    try:
        yield SimpleNamespace(client=TestClient(server.app, base_url="http://127.0.0.1"),
                              homes=homes, root=root)
    finally:
        hb.reset_cache()
        for db in dbs.values():
            db.close()


def _open(client, identity, profile_home):
    """Dial ``/api/ws`` with a ticket minted for *identity* naming *profile_home*."""
    from hermes_cli.web_routers.webapp import mint_socket_ticket
    from hermes_cli.web_session_sockets import GATEWAY_SOCKET

    ticket = mint_socket_ticket(identity, GATEWAY_SOCKET, profile_home=str(profile_home))
    return client.websocket_connect(f"/api/ws?native_dial=browser&ticket={ticket}",
                                    headers={"host": "127.0.0.1"})


def _closed_code(client, identity, profile_home):
    with pytest.raises(WebSocketDisconnect) as closed:
        with _open(client, identity, profile_home) as ws:
            ws.receive_json()
    return closed.value.code


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_a_foreign_profile_ticket_closes_the_socket(serving):
    """Alice's login + a ticket for ``01-bob`` → 4403, never Bob's sessions."""
    code = _closed_code(serving.client, {"user_id": "alice", "provider": "basic"}, serving.homes[FOREIGN_HOME])
    assert code == 4403


def test_own_profile_ticket_still_serves(serving):
    from hermes_cli.web_session_sockets import GATEWAY_SOCKET  # noqa: F401

    with _open(serving.client, {"user_id": "alice", "provider": "basic"},
               serving.homes[ALICE_HOME]) as ws:
        ws.send_json({"jsonrpc": "2.0", "id": "ask", "method": "runtime.describe", "params": {}})
        while True:
            frame = ws.receive_json()
            if frame.get("id") == "ask":
                break
    assert Path(frame["result"]["profile_id"]) == serving.homes[ALICE_HOME]


def test_the_loopback_owner_keeps_every_profile(serving):
    """``session-token`` is the host owner: a ticket for another profile is still served."""
    with _open(serving.client, {"user_id": "legacy-token-owner", "provider": "session-token"},
               serving.homes[FOREIGN_HOME]) as ws:
        ws.send_json({"jsonrpc": "2.0", "id": "ask", "method": "runtime.describe", "params": {}})
        while True:
            frame = ws.receive_json()
            if frame.get("id") == "ask":
                break
    assert Path(frame["result"]["profile_id"]) == serving.homes[FOREIGN_HOME]


def test_multi_user_off_keeps_the_old_socket_behaviour(serving, monkeypatch):
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: False)
    with _open(serving.client, {"user_id": "alice", "provider": "basic"},
               serving.homes[FOREIGN_HOME]) as ws:
        ws.send_json({"jsonrpc": "2.0", "id": "ask", "method": "runtime.describe", "params": {}})
        while True:
            frame = ws.receive_json()
            if frame.get("id") == "ask":
                break
    assert Path(frame["result"]["profile_id"]) == serving.homes[FOREIGN_HOME]
