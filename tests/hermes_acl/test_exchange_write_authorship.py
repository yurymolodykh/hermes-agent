"""The team exchange is writable only inside your own folder (L2-c, part B).

The hole this pins down: ``{HERMES_HOME}/shared`` has no owner the OS can tell apart — one Unix
account, one group, ``g+w``. The upload handler decided on the request's ``overwrite`` flag alone,
so one employee could silently replace a deliverable prepared for somebody else (the reviewer's
probe10: ``ALLOW`` on rewriting ``shared/team.docx`` by any verified subject).

Three rules are pinned here, at the seam where the bytes are actually written:

1. a scoped person's upload lands in ``shared/<subject-slug>/`` — never the exchange root, never a
   neighbour's subfolder;
2. replacing a file ANOTHER subject created is refused (409), while rewriting your own succeeds;
   the authorship journal (``shared/.authorship.jsonl``) is the backstop for files the layout did
   not place;
3. with ``acl.multi_user`` off — and for the host owner — the path is exactly the one asked for:
   the exchange root still takes a write, as before this change.
"""
from __future__ import annotations

import base64
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


def _request_as(login):
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
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
    # The exchange exists on disk: every VERIFIED person has it as a root.
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


def _data_url(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode("utf-8")).decode("ascii")


def _upload(client, path: Path, text: str, overwrite: bool = True):
    return client.post("/api/files/upload", json={
        "path": str(path), "data_url": _data_url(text), "overwrite": overwrite,
    })


# ---------------------------------------------------------------------------
# 1. a scoped write lands in the caller's own subfolder
# ---------------------------------------------------------------------------


class TestScopedWriteLandsInOwnFolder:
    def test_a_root_level_upload_is_redirected_into_the_callers_folder(
            self, multi_user, registry, fs_client_factory, _isolated_home):
        """Acceptance 1: the byte goes to ``shared/person_alice/``, not the exchange root."""
        exchange = _isolated_home / "shared"
        response = _upload(fs_client_factory("alice"), exchange / "team.docx", "alice's draft")

        assert response.status_code == 200, response.text
        own = exchange / "person_alice" / "team.docx"
        assert own.read_text(encoding="utf-8") == "alice's draft"
        assert not (exchange / "team.docx").exists(), "the exchange root must stay untouched"
        assert Path(response.json()["path"]) == own.resolve()

    def test_an_explicit_own_subfolder_is_kept(self, multi_user, registry, fs_client_factory,
                                               _isolated_home):
        exchange = _isolated_home / "shared"
        target = exchange / "person_alice" / "notes" / "daily.txt"
        response = _upload(fs_client_factory("alice"), target, "own notes")

        assert response.status_code == 200, response.text
        assert target.read_text(encoding="utf-8") == "own notes"

    def test_a_neighbours_folder_is_refused(self, multi_user, registry, fs_client_factory,
                                            _isolated_home, profile_homes):
        """Writing into bob's folder is not a handover: refused, and nothing is created."""
        foreign = _isolated_home / "shared" / "person_bob" / "planted.txt"
        response = _upload(fs_client_factory("alice"), foreign, "planted")

        assert response.status_code == 403, response.text
        assert not foreign.exists()

    def test_two_people_writing_the_same_name_do_not_collide(self, multi_user, registry,
                                                             fs_client_factory, _isolated_home):
        """Probe10's defect in the positive: the same relpath is two distinct files."""
        exchange = _isolated_home / "shared"
        draft = exchange / "brief.md"
        assert _upload(fs_client_factory("alice"), draft, "alice's brief").status_code == 200
        assert _upload(fs_client_factory("bob"), draft, "bob's brief").status_code == 200

        assert (exchange / "person_alice" / "brief.md").read_text(encoding="utf-8") == "alice's brief"
        assert (exchange / "person_bob" / "brief.md").read_text(encoding="utf-8") == "bob's brief"
        assert not draft.exists()


# ---------------------------------------------------------------------------
# 2. authorship: a foreign file is not overwritten, your own is
# ---------------------------------------------------------------------------


class TestAuthorshipBackstop:
    def test_a_root_level_file_another_subject_created_is_not_touched(
            self, multi_user, registry, fs_client_factory, _isolated_home):
        """Acceptance 2a (probe10): bob's root deliverable survives alice's upload byte for byte.

        The redirect answers this even better than a refusal would: alice's bytes land in her own
        folder and ``shared/team.docx`` is never opened for writing.
        """
        exchange = _isolated_home / "shared"
        legacy_file = exchange / "team.docx"
        legacy_file.write_text("bob's prepared deliverable", encoding="utf-8")
        results_acl.record_exchange_authorship(_request_as("bob"), legacy_file)

        response = _upload(fs_client_factory("alice"), legacy_file, "clobbered")

        assert response.status_code == 200, response.text
        assert legacy_file.read_text(encoding="utf-8") == "bob's prepared deliverable"
        assert (exchange / "person_alice" / "team.docx").read_text(encoding="utf-8") == "clobbered"

    def test_overwriting_a_foreign_authored_file_inside_a_folder_is_refused(
            self, multi_user, registry, fs_client_factory, _isolated_home):
        """Acceptance 2a (backstop): what the layout does not separate, the journal does.

        A file created by another subject that already sits in the caller's own folder (the state a
        migration from the flat layout can leave behind) is a 409 instead of a silent replacement.
        """
        exchange = _isolated_home / "shared"
        owned_folder = exchange / "person_alice"
        owned_folder.mkdir(parents=True, exist_ok=True)
        planted = owned_folder / "prepared.docx"
        planted.write_text("bob's prepared deliverable", encoding="utf-8")
        results_acl.record_exchange_authorship(_request_as("bob"), planted)

        response = _upload(fs_client_factory("alice"), planted, "clobbered")

        assert response.status_code == 409, response.text
        assert planted.read_text(encoding="utf-8") == "bob's prepared deliverable"

    def test_rewriting_your_own_file_succeeds(self, multi_user, registry, fs_client_factory,
                                              _isolated_home):
        """Acceptance 2b: ``overwrite`` is exactly for the author's own rewrite."""
        exchange = _isolated_home / "shared"
        own = exchange / "person_alice" / "team.docx"
        assert _upload(fs_client_factory("alice"), own, "v1").status_code == 200

        # The first upload itself journalled the authorship: no hand-written record needed.
        assert results_acl.exchange_author(own) == ALICE
        assert _upload(fs_client_factory("alice"), own, "v2").status_code == 200
        assert own.read_text(encoding="utf-8") == "v2"

    def test_an_unrecorded_file_is_not_refused(self, multi_user, registry, fs_client_factory,
                                               _isolated_home):
        """"Cannot tell who wrote it" must not become "not yours".

        A file written before this change carries no journal row. Refusing it would lock a person
        out of their own folder; the layout (rule 1) is the boundary, the journal only the backstop.
        """
        exchange = _isolated_home / "shared"
        own = exchange / "person_alice" / "predates.txt"
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("old", encoding="utf-8")

        assert _upload(fs_client_factory("alice"), own, "new").status_code == 200
        assert own.read_text(encoding="utf-8") == "new"

    def test_the_journal_is_jsonl_of_path_to_subject(self, multi_user, registry,
                                                     fs_client_factory, _isolated_home):
        exchange = _isolated_home / "shared"
        assert _upload(fs_client_factory("alice"), exchange / "x.txt", "x").status_code == 200

        journal = exchange / ".authorship.jsonl"
        assert journal.is_file()
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line]
        assert rows and rows[-1]["subject"] == ALICE
        assert Path(rows[-1]["path"]) == (exchange / "person_alice" / "x.txt").resolve()
        assert rows[-1]["at"]


# ---------------------------------------------------------------------------
# 3. no-op contract: multi_user off and the host owner keep the old behaviour
# ---------------------------------------------------------------------------


class TestNoOpContract:
    def test_with_scoping_off_the_exchange_root_takes_the_write(
            self, legacy, fs_client_factory, _isolated_home):
        """Acceptance 3a: the shipped default writes where it always wrote."""
        exchange = _isolated_home / "shared"
        target = exchange / "team.docx"
        response = _upload(fs_client_factory("alice"), target, "as before")

        assert response.status_code == 200, response.text
        assert target.read_text(encoding="utf-8") == "as before"
        assert not (exchange / "person_alice").exists()
        assert not (exchange / ".authorship.jsonl").exists()

    def test_the_host_owner_keeps_the_exchange_root(self, multi_user, registry,
                                                    fs_client_factory, _isolated_home):
        """Acceptance 3b: the owner is never scoped — no redirect, no journal."""
        exchange = _isolated_home / "shared"
        target = exchange / "team.docx"
        response = _upload(fs_client_factory(OWNER_LOGIN), target, "owner's file")

        assert response.status_code == 200, response.text
        assert target.read_text(encoding="utf-8") == "owner's file"
        assert results_acl.exchange_author(target) == ""

    def test_policy_functions_are_no_ops_when_unscoped(self, legacy, profile_homes,
                                                       _isolated_home):
        """The seam itself, without HTTP: unscoped answers are identity."""
        exchange = _isolated_home / "shared"
        target = exchange / "team.docx"
        request = _request_as("alice")
        assert results_acl.exchange_write_target(request, target) == target.resolve()
        results_acl.require_exchange_authorship(request, target)  # must not raise
