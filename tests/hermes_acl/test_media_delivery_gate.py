"""MEDIA delivery is per-PERSON: a link the recipient cannot open is never sent (L2-c).

The hole this pins down: the outbound validator (``validate_media_delivery_path``) is a DENYLIST.
It answers "may the gateway disclose this host file at all" — not "may THIS person read it". So an
assistant answering inside another person's chat could attach the owner's personal file
(``profiles/01-orchestrator/SOUL.md``), and the recipient got 403 at download time: a broken link
instead of a deliverable (the root of the incident this card comes from).

Two answers are acceptable and both are tested here:

* the file is MOVED into the team exchange under the recipient's own subfolder
  (``{HERMES_HOME}/shared/<subject-slug>/``), which is one of their file roots — the returned path
  is the moved one, and it downloads for them (200 through the real files router);
* the handover is impossible — then the path is DROPPED with a readable reason in ``dropped``,
  never offered as a link.

And the no-op contract: with ``acl.multi_user`` off — and for the host owner — the same path comes
back unchanged and nothing is moved.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_results as results_acl
import hermes_acl_scope
from gateway.owner_identity import host_owner_subject

ALICE = "person/alice"
BOB = "person/bob"
OWNER_LOGIN = "owner"
ALICE_ROOT = "01-alice"
BOB_ROOT = "01-bob"
SHARED = ("02-hermes-platform", "03-software-engineer")

ALICE_DOC = {
    "slug": ALICE, "display_name": "Alice", "shared": False,
    "profiles": [ALICE_ROOT], "subjects": [], "owner": ALICE, "source": "admin",
}
BOB_DOC = {
    "slug": BOB, "display_name": "Bob", "shared": False,
    "profiles": [BOB_ROOT], "subjects": [], "owner": BOB, "source": "admin",
}
SHARED_DOC = {
    "slug": "shared/service", "display_name": "Shared", "shared": True,
    "profiles": list(SHARED), "subjects": [], "owner": "admin", "source": "admin",
}


class _Session:
    """The verified dashboard login (``request.state.session``) the auth gate attaches."""

    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


class _Source:
    """A ``SessionSource`` stand-in: the delivery gate reads ``user_id`` off it."""

    def __init__(self, user_id, chat_id="chat-1"):
        self.user_id, self.chat_id = user_id, chat_id


def _request_as(login):
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.session = _Session(login)
    return request


def _owner_request():
    return _request_as(OWNER_LOGIN)


# ---------------------------------------------------------------------------
# fixtures: same shape as tests/hermes_cli/test_results_route_scope.py
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    hb.reset_cache()
    yield home
    hb.reset_cache()


@pytest.fixture
def registry(_isolated_home):
    path = _isolated_home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [ALICE_DOC, BOB_DOC, SHARED_DOC],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    hb.reset_cache()
    yield path
    hb.reset_cache()


@pytest.fixture
def multi_user(monkeypatch):
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        if not login:
            return fallback
        return host_owner_subject() if login == OWNER_LOGIN else f"person/{login}"

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", _resolve)
    return True


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    return False


@pytest.fixture
def profile_homes(_isolated_home, monkeypatch):
    from hermes_cli import profiles as profiles_mod
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    root = default_home / "profiles"
    homes = {"default": default_home}
    (default_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    for name in (ALICE_ROOT, BOB_ROOT, *SHARED):
        home = root / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        (home / "secret.txt").write_text(f"only {name} reads this", encoding="utf-8")
        homes[name] = home
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: root)
    # The exchange is a real directory: every VERIFIED person has it as a root.
    (_isolated_home / "shared").mkdir(exist_ok=True)
    return homes


@pytest.fixture
def fs_client_factory(profile_homes, monkeypatch):
    from hermes_cli.web_routers import files as files_router

    def build(login):
        app = FastAPI()

        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.session = _Session(login)
            return await call_next(request)

        app.include_router(files_router.router)
        return TestClient(app)

    monkeypatch.delenv("HERMES_DASHBOARD_FILES_ROOT", raising=False)
    monkeypatch.setattr(
        "hermes_cli.web_server_files._default_hermes_root_is_opt_data", lambda: False)
    return build


# ---------------------------------------------------------------------------
# the policy function itself
# ---------------------------------------------------------------------------


class TestPathAllowedForSubject:
    def test_a_neighbours_file_is_refused(self, multi_user, registry, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        assert results_acl.path_allowed_for_subject(ALICE, foreign) is False

    def test_an_own_file_is_allowed(self, multi_user, registry, profile_homes):
        own = profile_homes[ALICE_ROOT] / "secret.txt"
        assert results_acl.path_allowed_for_subject(ALICE, own) is True

    def test_the_exchange_is_allowed_to_a_verified_person(self, multi_user, registry,
                                                         profile_homes, _isolated_home):
        assert results_acl.path_allowed_for_subject(
            ALICE, _isolated_home / "shared" / "person_alice" / "x.txt") is True

    def test_an_empty_subject_is_refused_under_multi_user(self, multi_user, registry,
                                                          profile_homes):
        # Fail closed: no identity is not "everybody".
        assert results_acl.path_allowed_for_subject("", profile_homes[BOB_ROOT] / "secret.txt") is False

    def test_with_scoping_off_anything_is_allowed(self, legacy, profile_homes):
        assert results_acl.path_allowed_for_subject(
            ALICE, profile_homes[BOB_ROOT] / "secret.txt") is True

    def test_the_host_owner_is_never_scoped(self, multi_user, registry, profile_homes):
        owner = host_owner_subject()
        assert results_acl.scoped_subject(owner) is False
        assert results_acl.path_allowed_for_subject(
            owner, profile_homes[BOB_ROOT] / "secret.txt") is True


# ---------------------------------------------------------------------------
# the delivery seam
# ---------------------------------------------------------------------------


def _filter(path, source, monkeypatch, actor=""):
    """Run the real MEDIA filter with the turn's actor stubbed."""
    from gateway.platforms import base as base_mod

    monkeypatch.setattr("hermes_acl_scope.caller_subject", lambda: actor)
    dropped: list = []
    out = base_mod.BasePlatformAdapter.filter_media_delivery_paths(
        [(str(path), False)], dropped=dropped, source=source)
    return out, dropped


class TestMediaDeliveryGate:
    def test_a_foreign_personal_file_is_handed_over_to_the_exchange(
            self, multi_user, registry, profile_homes, monkeypatch, _isolated_home):
        """Acceptance 1a: the recipient gets a path THEY can download, not a 403 link."""
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        out, dropped = _filter(foreign, _Source(ALICE), monkeypatch)

        assert dropped == [], "a successful handover is a delivery, not a drop"
        assert out, "the path must survive as a deliverable"
        delivered = Path(out[0][0])
        assert delivered != foreign
        assert delivered.parent == (_isolated_home / "shared" / "person_alice").resolve() \
            or delivered.parent == _isolated_home / "shared" / "person_alice"
        assert delivered.read_text(encoding="utf-8") == "only 01-bob reads this"
        assert not foreign.exists(), "the handover is a move, not a copy"
        assert results_acl.path_allowed_for_subject(ALICE, delivered) is True

    def test_the_handed_over_path_downloads_for_that_person(
            self, multi_user, registry, profile_homes, monkeypatch, fs_client_factory):
        """Acceptance 4 (live): the link alice receives answers 200 for alice."""
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        out, _ = _filter(foreign, _Source(ALICE), monkeypatch)
        delivered = out[0][0]

        response = fs_client_factory("alice").get(
            "/api/fs/download", params={"path": delivered})
        assert response.status_code == 200
        assert response.content == b"only 01-bob reads this"

    def test_the_original_foreign_path_still_refuses_her(
            self, multi_user, registry, profile_homes, monkeypatch, fs_client_factory):
        """The counter-example: without the handover the link WOULD 403 — the defect itself."""
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        assert not foreign.exists() or True  # untouched by this test
        response = fs_client_factory("alice").get(
            "/api/fs/download", params={"path": str(foreign)})
        assert response.status_code == 403

    def test_an_own_file_is_delivered_unchanged(self, multi_user, registry, profile_homes,
                                                monkeypatch):
        own = profile_homes[ALICE_ROOT] / "secret.txt"
        out, dropped = _filter(own, _Source(ALICE), monkeypatch)
        assert dropped == []
        assert Path(out[0][0]).resolve() == own.resolve()
        assert own.exists()

    def test_an_impossible_handover_is_a_reasoned_drop(
            self, multi_user, registry, profile_homes, monkeypatch, _isolated_home):
        """Acceptance 1b: no move possible -> a dropped entry with a readable reason, no 403 link."""
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        # Make the exchange unusable: a FILE where the subject's folder must be.
        blocked = _isolated_home / "shared" / "person_alice"
        blocked.write_text("not a directory", encoding="utf-8")

        out, dropped = _filter(foreign, _Source(ALICE), monkeypatch)
        assert out == []
        assert len(dropped) == 1
        assert dropped[0]["path"] == str(foreign.resolve())
        assert dropped[0]["reason"] and "403" not in dropped[0]["reason"]
        assert foreign.exists(), "a failed handover must not lose the file"

    def test_the_actor_wins_over_the_source(self, multi_user, registry, profile_homes, monkeypatch):
        """The turn's actor decides when both are known (a folded send is not the recipient)."""
        own = profile_homes[ALICE_ROOT] / "secret.txt"
        out, _ = _filter(own, _Source(BOB), monkeypatch, actor=ALICE)
        assert Path(out[0][0]).resolve() == own.resolve()

    def test_scoping_off_leaves_the_path_alone(self, legacy, profile_homes, monkeypatch):
        """Acceptance 2: ``multi_user=false`` is a no-op — same path, nothing moved."""
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        out, dropped = _filter(foreign, _Source(ALICE), monkeypatch)
        assert dropped == []
        assert Path(out[0][0]).resolve() == foreign.resolve()
        assert foreign.exists()

    def test_the_host_owner_keeps_the_same_path(self, multi_user, registry, profile_homes,
                                                monkeypatch):
        """Acceptance 2: the owner's ``file_roots() is None`` answer is honoured end to end."""
        owner = host_owner_subject()
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        out, dropped = _filter(foreign, _Source(owner), monkeypatch, actor=owner)
        assert dropped == []
        assert Path(out[0][0]).resolve() == foreign.resolve()
        assert foreign.exists()
