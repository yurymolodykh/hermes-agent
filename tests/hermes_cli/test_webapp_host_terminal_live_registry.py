"""Acceptance: the terminal gate resolves the SAME registry key the policy is written for.

The previous iteration's test (``test_webapp_host_terminal_real_dial.py``) mocked
``hermes_acl_identity.resolve_subject`` to return ``person/<login>`` and put ``"slug": "person/maria"``
in a fixture registry — so ``allows`` received a key the LIVE path never produces. On the real
deployment the ticket carries ``{user_id: "person/maria.safronova", provider: "local"}``
(``web_server_chat._consume_ws_ticket``), which ``gateway.owner_identity.principal_subject`` names
``auth:v1:["local","","person/maria.safronova"]``; ``hermes_acl_bindings.person_of`` matched that
form EXACTLY and answered ``None``, so Maria's own profile was refused while the green tests said
otherwise.

This module pins the real path: the ticket is minted the way the real dial mints it, the registry is
the LIVE ``profiles/acl/profile-bindings.json`` document (copied into a temp home, byte for byte — no
hand-written fixture, no ``resolve_subject`` mock), and every assertion prints the subject it acted
on and the record it resolved to, so a broken key can never pass silently again.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_profiles as prof_acl

# Module-level on purpose: see the note in ``test_webapp_host_terminal_real_dial`` — a shell spawn
# reaches ``gateway.run``, whose import settles a pending update against the real home AFTER the
# autouse HomeIOGuard is installed.
import gateway.run
import hermes_cli.web_server
import tui_gateway.ws

MARIA = "01-maria-safronova"
KSENIA = "01-ksenia-kozlovskaya"
ORCH = "01-orchestrator"
PERSONAL = "16-personal"
SHARED = "03-software-engineer"

#: The live registry this deployment serves (``hermes_acl_bindings.bindings_path()``).
LIVE_REGISTRY = Path("/home/hermes/.hermes/acl/profile-bindings.json")
#: The live users file: what makes Maria a NON-owner password login (``person/<login>``) for real.
LIVE_USERS = Path("/home/hermes/.hermes/dashboard-users.jsonl")

#: The identity the REAL terminal dial's ticket carries (``web_server_chat._consume_ws_ticket``
#: stamps ``{"user_id": info["user_id"], "provider": info["provider"]}`` — no issuer).
MARIA_TICKET_IDENTITY = {"user_id": "person/maria.safronova", "provider": "local"}
KSENIA_TICKET_IDENTITY = {"user_id": "person/ksenia.kozlovskaya", "provider": "local"}
OWNER_TICKET_IDENTITY = {"user_id": "uid:1001", "provider": "local"}

pytestmark = pytest.mark.skipif(
    not (LIVE_REGISTRY.is_file() and LIVE_USERS.is_file()),
    reason="needs the live profile-bindings.json and dashboard-users.jsonl of this deployment",
)


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    """A Webapp over ``<tmp>/.hermes`` that mirrors the LIVE acl + users files, unchanged.

    ``config.yaml`` carries the deployment's real switches (``acl.multi_user: true``,
    ``dashboard.basic_auth.users_file``) so ``multi_user_enabled`` and ``record_for`` answer from
    files rather than from a monkeypatch — the production rule, not a stand-in for it.
    """
    from hermes_cli import web_host_terminal, web_server, web_server_chat
    from hermes_cli.web_host_terminal_sessions import host_terminal_lifespan
    from hermes_cli.web_routers import webapp as _webapp_module

    home = tmp_path / ".hermes"
    (home / "acl").mkdir(parents=True)
    (home / "profiles").mkdir()
    shutil.copy2(LIVE_REGISTRY, home / "acl" / "profile-bindings.json")
    users_copy = home / "dashboard-users.jsonl"
    shutil.copy2(LIVE_USERS, users_copy)
    users_copy.chmod(0o600)  # the live file's mode; ``load_users_file`` refuses loose bits
    for name in (MARIA, KSENIA, ORCH, PERSONAL, SHARED):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "acl:\n  enabled: true\n  multi_user: true\n"
        f"dashboard:\n  basic_auth:\n    users_file: {users_copy}\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(web_server.app.state, "ui_surface", "webapp", raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "localhost", raising=False)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "d" * 43)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(web_host_terminal, "shell_spec", lambda: (["/bin/sh", "-i"], "sh"))
    shells: list = []

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
    from tools.environments import local as _local_env
    from tools.environments import local_pythonpath as _local_pp

    monkeypatch.setattr(_local_env, "_resolve_hermes_bin_dir", lambda: None)
    monkeypatch.setattr(_local_env, "_managed_runtime_path_entries", list)
    monkeypatch.setattr(_local_pp, "_get_hermes_site_packages", lambda _env: [])
    hb.reset_cache()

    host = FastAPI(lifespan=host_terminal_lifespan)
    host.include_router(_webapp_module.router)
    yield SimpleNamespace(home=home, host=host, shells=shells)
    hb.reset_cache()


@pytest.fixture(autouse=True)
def _clean_caches():
    hb.reset_cache()
    yield
    hb.reset_cache()


def _mint(identity: dict) -> str:
    """The ticket the real terminal dial mints: ``socket=host-terminal``, NO profile."""
    from hermes_cli.web_routers.webapp import mint_socket_ticket

    return mint_socket_ticket(identity, "host-terminal")


def _dial(host: FastAPI, ticket: str, profile: str | None) -> int | None:
    """Dial ``/api/host-terminal``; return the close code, or None when the shell was accepted."""
    url = f"ws://localhost/api/host-terminal?ticket={ticket}"
    if profile is not None:
        url += f"&profile={profile}"
    client = TestClient(host, base_url="http://localhost")
    with client.websocket_connect(url) as ws:
        payload = json.loads(ws.receive_text())
        if payload.get("type") == "error" and payload.get("code") in (4401, 4403):
            return payload["code"]
        return None


def _grade(identity: dict, profile: str) -> tuple[bool, str]:
    """``(allowed, diagnostic)`` from the SAME code the socket gate runs, with its resolved key.

    Diagnostics are the point: the previous green test could not see that ``allows`` was handed
    ``auth:v1:[...]`` while the registry keys ``person/<login>``.
    """
    from hermes_acl_scope import request_identity_subject
    from hermes_cli.web_routers import webapp as _webapp_module

    ws = SimpleNamespace(query_params={"profile": profile}, _hermes_auth_identity=identity)
    allowed = _webapp_module._ticket_principal_may_open(ws, profile)
    request = SimpleNamespace(state=SimpleNamespace(
        session=SimpleNamespace(user_id=identity["user_id"], provider=identity["provider"],
                                issuer=identity.get("issuer", "") or ""),
        native_http_principal=None))
    subject = request_identity_subject(request)
    slug = prof_acl.registry_slug(subject)
    diag = (f"identity={identity!r} subject={subject!r} registry_slug={slug!r} "
            f"person_of(subject)={hb.load_cached().person_of(subject)!r} profile={profile!r}")
    return allowed, diag


# ── acceptance 1: Maria opens her OWN profile on the live registry ───────────────────────────────

def test_maria_opens_her_own_profile_on_the_live_registry(live_home):
    allowed, diag = _grade(MARIA_TICKET_IDENTITY, MARIA)
    assert allowed is True, f"Maria must open her own profile; {diag}"


def test_maria_own_profile_is_accepted_on_the_real_socket(live_home):
    code = _dial(live_home.host, _mint(MARIA_TICKET_IDENTITY), MARIA)
    assert code is None, "the real host-terminal socket must accept Maria's own profile"
    assert len(live_home.shells) == 1


# ── acceptance 2: a foreign personal profile closes 4403 BEFORE accept() and before the PTY ─────

@pytest.mark.parametrize("foreign", [ORCH, KSENIA, PERSONAL])
def test_a_foreign_personal_profile_is_refused_before_any_shell(live_home, foreign):
    allowed, diag = _grade(MARIA_TICKET_IDENTITY, foreign)
    assert allowed is False, f"Maria must not open {foreign!r}; {diag}"
    code = _dial(live_home.host, _mint(MARIA_TICKET_IDENTITY), foreign)
    assert code == 4403, f"expected 4403 for {foreign!r}, got {code!r}"
    assert live_home.shells == [], "no PTY may exist for a refused profile"


def test_a_shared_profile_opens_for_a_named_person(live_home):
    allowed, diag = _grade(MARIA_TICKET_IDENTITY, SHARED)
    assert allowed is True, f"a shared profile is every person's; {diag}"
    assert _dial(live_home.host, _mint(MARIA_TICKET_IDENTITY), SHARED) is None


# ── acceptance 3: owner any, anonymous none ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [MARIA, KSENIA, ORCH, PERSONAL, SHARED])
def test_the_owner_opens_any_profile(live_home, name):
    allowed, diag = _grade(OWNER_TICKET_IDENTITY, name)
    assert allowed is True, f"the host owner opens {name!r}; {diag}"


def test_an_anonymous_dial_opens_nothing(live_home):
    ws = SimpleNamespace(query_params={"profile": MARIA})
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    assert _host_terminal_profile_matches(ws) is False


def test_a_launch_home_dial_belongs_to_the_owner_alone(live_home):
    from hermes_cli.web_routers.webapp import _host_terminal_profile_matches

    maria = SimpleNamespace(query_params={}, _hermes_auth_identity=MARIA_TICKET_IDENTITY)
    assert _host_terminal_profile_matches(maria) is False
    owner = SimpleNamespace(query_params={}, _hermes_auth_identity=OWNER_TICKET_IDENTITY)
    assert _host_terminal_profile_matches(owner) is True


# ── the real login shape: a ``basic`` session mints ``person/<login>`` through the LIVE users file ─

def test_a_password_login_resolves_to_the_same_registry_person(live_home):
    """``principal_subject`` for Maria's real login is ``person/maria.safronova`` — unmocked.

    This is the assertion the previous test could not make: ``resolve_subject`` was replaced by
    ``person/<login>``, hiding that the registry is keyed the same way only AFTER expansion.
    """
    from gateway.owner_identity import principal_subject

    subject = principal_subject({"user_id": "maria.safronova", "provider": "basic"})
    assert subject == "person/maria.safronova", f"live users file must name Maria's person; got {subject!r}"
    assert prof_acl.registry_slug(subject) == "person/maria.safronova"
    assert prof_acl.allows(subject, MARIA) is True
    assert prof_acl.allows(subject, ORCH) is False


def test_the_minted_subject_resolves_through_the_acls_own_expansion(live_home):
    """The exact defect: ``person_of`` on the minted subject is None, expansion recovers the key."""
    registry = hb.load_cached()
    minted = 'auth:v1:["local","","person/maria.safronova"]'
    assert registry.person_of(minted) is None, "exact match is intentional — do not weaken person_of"
    assert prof_acl.registry_slug(minted) == "person/maria.safronova", (
        f"registry_slug must expand the minted subject; got {prof_acl.registry_slug(minted)!r}")


# ── no widening: an unregistered or spoofed principal reaches no personal profile ──────────────

@pytest.mark.parametrize("identity", [
    {"user_id": "999999999", "provider": "telegram"},
    {"user_id": "person/yury2", "provider": "local"},
    {"user_id": "nobody.at.all", "provider": "federated-unknown"},
])
def test_an_unknown_principal_gains_no_personal_profile(live_home, identity):
    for name in (MARIA, KSENIA, ORCH, PERSONAL):
        allowed, diag = _grade(identity, name)
        assert allowed is False, f"unknown principal must not open {name!r}; {diag}"
