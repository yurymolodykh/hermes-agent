"""A person sees only their OWN results — files, skills, memory and card attachments (МПД Т6).

The hole this pins down: steps 3–5 gave every person an identity, their own sessions and their own
orchestrator, but the surfaces that answer "show me this path / this skill / this memory / this
attachment" still answered for the WHOLE host.

* ``/api/files*`` and ``/api/fs/*`` resolved any path the process could read, so ``GET
  /api/files?path=/home/someone-else`` listed a neighbour's home (and ``..`` was only refused
  syntactically, never after resolution).
* ``/api/skills``, ``/api/skills/content`` and ``/api/memory`` fell back to the LAUNCH profile when
  no ``?profile=`` was named — on a shared host, the owner's.
* A card attachment was fetched, listed and DELETED by its own integer id with no ownership check,
  so a signed-in neighbour could walk the sequence.

Three layers are pinned, and they are deliberately separate:

* :mod:`hermes_acl_results` — the policy: the caller's file roots, the effective profile, the
  memory bucket, the card ownership. Unit tests, no HTTP.
* the dashboard routes — the same policy at each seam, with the refusal shape a client can rely on
  (403 for a path outside the root; the 404 an absent id gets for a foreign card, so ids stay
  unenumerable).
* the legacy answer — with ``acl.multi_user`` off (the shipped default) NOTHING changes, and the
  positive cases below prove the owner still reads their own files, skills, memory and attachments.
"""
from __future__ import annotations

import importlib.util
import json
import sys
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


# ---------------------------------------------------------------------------
# fixtures: identity mode, registry, profile homes, board
# ---------------------------------------------------------------------------


class _Session:
    """The verified dashboard login (``request.state.session``) the auth gate attaches."""

    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test owns its HERMES_HOME; nothing here may touch the operator's real registry."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hb.reset_cache()
    yield home
    hb.reset_cache()


@pytest.fixture
def registry(_isolated_home):
    """A real on-disk registry: alice → 01-alice, bob → 01-bob, 02/03 shared to everyone."""
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
    """``acl.multi_user=true``: a login becomes ``person/<login>``, ``owner`` stays the host owner."""
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_scope, "identity_trust", lambda: "untrusted")
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
    """The shipped default: multi-user OFF."""
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    return False


@pytest.fixture
def profile_homes(_isolated_home, monkeypatch):
    """Real profile directories: default + 01-alice + 01-bob + the two shared profiles.

    Each person's home holds one file only they may read: ``secret.txt``.
    """
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
    return homes


def _request_as(login):
    """A bare ``Request`` whose verified session is *login* (the auth gate's own attachment)."""
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    request = Request(scope)
    request.state.session = _Session(login)
    return request


def _owner_request():
    return _request_as(OWNER_LOGIN)


def _anonymous_request():
    """A request the auth gate attached NOTHING to (no session, no native grant).

    ``_request_as("")`` is not this: an empty ``user_id`` still mints the
    ``auth:v1:[…,"unbound"]`` subject the legacy owner rules recognise.
    """
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


# ---------------------------------------------------------------------------
# hermes_acl_results — the rule itself
# ---------------------------------------------------------------------------


class TestScopingSwitch:
    def test_scoping_is_off_by_default(self, legacy):
        assert results_acl.scoped(_request_as("alice")) is False

    def test_scoping_turns_on_for_a_person(self, multi_user, registry):
        assert results_acl.scoped(_request_as("alice")) is True

    def test_the_host_owner_is_never_scoped(self, multi_user, registry):
        assert results_acl.scoped(_owner_request()) is False

    def test_a_request_without_identity_is_scoped(self, multi_user, registry):
        # No verified subject and multi-user on: the fail-closed answer is "scoped, with no roots".
        assert results_acl.scoped(_anonymous_request()) is True

    def test_no_request_at_all_is_scoped(self, multi_user, registry):
        assert results_acl.scoped(None) is True

    def test_reading_the_flag_never_creates_a_hermes_home(self, tmp_path, monkeypatch):
        """A file listing must not materialise ``~/.hermes`` as a side effect.

        The flag lives in ``config.yaml`` and the usual reader is ``load_config()``, which ENSURES
        the home skeleton. ``/api/files`` asks this policy on every listing, so on a tree with no
        home yet the answer has to come without the loader — otherwise browsing a directory
        creates the operator's home (and every "did this run write anything?" check turns noisy).
        """
        absent = tmp_path / "no-home-yet"
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: absent)
        assert results_acl.scoped(_request_as("alice")) is False
        assert not absent.exists()


class TestFileRoots:
    def test_unscoped_returns_none(self, legacy, profile_homes):
        assert results_acl.file_roots(_request_as("alice")) is None

    def test_person_gets_exactly_their_own_home(self, multi_user, registry, profile_homes):
        roots = results_acl.file_roots(_request_as("alice"))
        assert roots == (profile_homes[ALICE_ROOT].resolve(),)

    def test_second_person_gets_a_different_root(self, multi_user, registry, profile_homes):
        alice = results_acl.file_roots(_request_as("alice"))
        bob = results_acl.file_roots(_request_as("bob"))
        assert alice and bob and alice != bob

    def test_owner_keeps_the_host_view(self, multi_user, registry, profile_homes):
        assert results_acl.file_roots(_owner_request()) is None

    def test_absent_registry_fails_closed(self, multi_user, profile_homes):
        assert results_acl.file_roots(_request_as("alice")) == ()

    def test_corrupt_registry_fails_closed(self, multi_user, _isolated_home, profile_homes):
        path = _isolated_home / "acl" / "profile-bindings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        hb.reset_cache()
        assert results_acl.file_roots(_request_as("alice")) == ()

    def test_identityless_request_gets_no_root(self, multi_user, registry, profile_homes):
        assert results_acl.file_roots(_anonymous_request()) == ()


class TestTeamWorkDirs:
    """Acceptance #6: the team's OWN working trees — not just card folders — are shared.

    A producer's script lands in its profile's ``terminal.cwd`` (for 03, a real directory off the
    kanban tree), which no card folder contains. Without this root "the team's code is shared"
    stops at card attachments.
    """

    def _give_shared_profile_a_workdir(self, profile_homes, tmp_path):
        """Point a SHARED profile's ``terminal.cwd`` at a real directory holding one file."""
        shared_home = profile_homes[SHARED[1]]
        work = tmp_path / "workspaces" / SHARED[1]
        work.mkdir(parents=True)
        script = work / "harvest.py"
        script.write_text("print('team material')\n", encoding="utf-8")
        (shared_home / "config.yaml").write_text(
            f"terminal:\n  cwd: {work}\n", encoding="utf-8")
        return work, script

    def test_a_shared_profiles_work_dir_is_a_team_root(self, multi_user, registry,
                                                       profile_homes, tmp_path):
        work, script = self._give_shared_profile_a_workdir(profile_homes, tmp_path)
        roots = results_acl.file_roots(_request_as("alice"))
        assert work.resolve() in roots, "03's working tree must be in every person's root"
        assert results_acl.path_allowed(_request_as("alice"), script) is True

    def test_a_personal_profiles_work_dir_is_never_a_team_root(self, multi_user, registry,
                                                               profile_homes, tmp_path):
        # A personal profile is not in shared_profiles(): its work dir must not leak in even
        # when it names one, so the boundary is the registry, not the presence of a config.
        bob_home = profile_homes[BOB_ROOT]
        work = tmp_path / "workspaces" / BOB_ROOT
        work.mkdir(parents=True)
        (work / "private.py").write_text("x", encoding="utf-8")
        (bob_home / "config.yaml").write_text(f"terminal:\n  cwd: {work}\n", encoding="utf-8")
        assert results_acl.path_allowed(_request_as("alice"), work / "private.py") is False

    def test_a_shared_profiles_home_files_stay_out_even_with_a_work_dir(self, multi_user, registry,
                                                                       profile_homes, tmp_path):
        # The work tree is shared; the profile's own home (SOUL, .env) is not a work product.
        self._give_shared_profile_a_workdir(profile_homes, tmp_path)
        private = profile_homes[SHARED[1]] / "secret.txt"
        assert results_acl.path_allowed(_request_as("alice"), private) is False

    def test_a_relative_or_missing_work_dir_grants_nothing(self, multi_user, registry,
                                                           profile_homes):
        # "." is the LAUNCH profile's cwd, not this profile's: an unset work dir is no root.
        (profile_homes[SHARED[1]] / "config.yaml").write_text(
            "terminal:\n  cwd: .\n", encoding="utf-8")
        assert results_acl._shared_work_dirs() == [] or all(
            p != Path.cwd().resolve() for p in results_acl._shared_work_dirs())


class TestPathContainment:
    def test_own_file_is_allowed(self, multi_user, registry, profile_homes):
        own = profile_homes[ALICE_ROOT] / "secret.txt"
        assert results_acl.path_allowed(_request_as("alice"), own) is True

    def test_own_root_itself_is_allowed(self, multi_user, registry, profile_homes):
        assert results_acl.path_allowed(_request_as("alice"), profile_homes[ALICE_ROOT]) is True

    def test_a_neighbours_file_is_refused(self, multi_user, registry, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        assert results_acl.path_allowed(_request_as("alice"), foreign) is False

    def test_a_shared_profile_is_not_a_personal_root(self, multi_user, registry, profile_homes):
        # Shared profiles are usable, not OWNED: a person's file root stays their own home.
        foreign = profile_homes[SHARED[0]] / "secret.txt"
        assert results_acl.path_allowed(_request_as("alice"), foreign) is False

    def test_traversal_out_of_the_root_is_refused(self, multi_user, registry, profile_homes):
        escape = profile_homes[ALICE_ROOT] / ".." / BOB_ROOT / "secret.txt"
        assert results_acl.path_allowed(_request_as("alice"), escape) is False

    def test_a_symlink_pointing_out_is_refused(self, multi_user, registry, profile_homes):
        link = profile_homes[ALICE_ROOT] / "link"
        try:
            link.symlink_to(profile_homes[BOB_ROOT] / "secret.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this host")
        assert results_acl.path_allowed(_request_as("alice"), link) is False

    def test_unscoped_allows_anything(self, legacy, profile_homes, tmp_path):
        assert results_acl.path_allowed(_request_as("alice"), tmp_path / "whatever") is True

    def test_require_path_raises_403_for_a_foreign_path(self, multi_user, registry, profile_homes):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            results_acl.require_path(_request_as("alice"), profile_homes[BOB_ROOT] / "secret.txt")
        assert caught.value.status_code == 403

    def test_require_path_is_silent_for_an_own_path(self, multi_user, registry, profile_homes):
        results_acl.require_path(_request_as("alice"), profile_homes[ALICE_ROOT] / "secret.txt")


class TestResolveProfile:
    def test_unscoped_keeps_the_requested_name(self, legacy):
        assert results_acl.resolve_profile(_request_as("alice"), BOB_ROOT) == BOB_ROOT

    def test_unscoped_keeps_an_omitted_name_omitted(self, legacy):
        assert results_acl.resolve_profile(_request_as("alice"), None) is None

    def test_person_without_a_name_gets_their_own_root(self, multi_user, registry):
        # The leak: "no profile" used to mean the LAUNCH profile (the owner's on a shared host).
        assert results_acl.resolve_profile(_request_as("alice"), None) == ALICE_ROOT

    def test_person_naming_their_own_root_is_allowed(self, multi_user, registry):
        assert results_acl.resolve_profile(_request_as("alice"), ALICE_ROOT) == ALICE_ROOT

    def test_person_naming_a_shared_profile_is_allowed(self, multi_user, registry):
        assert results_acl.resolve_profile(_request_as("alice"), SHARED[0]) == SHARED[0]

    def test_person_naming_a_foreign_profile_gets_the_absent_name_miss(self, multi_user, registry):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            results_acl.resolve_profile(_request_as("alice"), BOB_ROOT)
        assert caught.value.status_code == 404

    def test_owner_keeps_any_name(self, multi_user, registry):
        assert results_acl.resolve_profile(_owner_request(), BOB_ROOT) == BOB_ROOT


class TestMemoryBuckets:
    def _dirs(self, home):
        base = home / "memories"
        return [base, base / "u" / "person_alice", base / "u" / "person_bob"]

    def test_unscoped_returns_every_bucket(self, legacy, _isolated_home):
        dirs = self._dirs(_isolated_home)
        assert results_acl.memory_dirs_for(_request_as("alice"), dirs) == dirs

    def test_person_sees_only_their_own_bucket(self, multi_user, registry, _isolated_home):
        dirs = self._dirs(_isolated_home)
        kept = results_acl.memory_dirs_for(_request_as("alice"), dirs)
        assert [p.name for p in kept] == ["person_alice"]

    def test_the_legacy_base_is_not_offered_to_a_person(self, multi_user, registry, _isolated_home):
        # The base holds the notes written before per-subject memory — the host owner's.
        dirs = self._dirs(_isolated_home)
        base = dirs[0]
        assert base not in results_acl.memory_dirs_for(_request_as("alice"), dirs)

    def test_identityless_request_sees_no_bucket(self, multi_user, registry, _isolated_home):
        assert results_acl.memory_dirs_for(_anonymous_request(), self._dirs(_isolated_home)) == []

    def test_owner_sees_every_bucket(self, multi_user, registry, _isolated_home):
        dirs = self._dirs(_isolated_home)
        assert results_acl.memory_dirs_for(_owner_request(), dirs) == dirs


# ---------------------------------------------------------------------------
# Kanban attachments — ownership by CARD, never by attachment id
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(_isolated_home, monkeypatch):
    """A real board in the isolated home."""
    from hermes_cli import kanban_db as kb

    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_ATTACHMENTS_ROOT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    kb.init_db()
    return _isolated_home


@pytest.fixture
def board(kanban_home):
    """Two cards: alice's and bob's, each with one attachment on disk."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(db_path=kb.kanban_db_path())
    try:
        alice_task = kb.create_task(conn, title="alice's card", created_by=ALICE, assignee=ALICE)
        bob_task = kb.create_task(conn, title="bob's card", created_by=BOB, assignee=BOB)
        out = {}
        for label, task_id in (("alice", alice_task), ("bob", bob_task)):
            dest = kb.task_attachments_dir(task_id)
            dest.mkdir(parents=True, exist_ok=True)
            blob = dest / "note.txt"
            blob.write_text(f"{label}'s private notes", encoding="utf-8")
            att_id = kb.add_attachment(
                conn, task_id, filename=blob.name, stored_path=str(blob.resolve()),
                content_type="text/plain", size=blob.stat().st_size, uploaded_by=label)
            out[label] = {"task": task_id, "attachment": att_id, "path": blob}
        return out
    finally:
        conn.close()


class TestCardOwnership:
    def test_unscoped_every_card_is_visible(self, legacy, kanban_home, board):
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            bob_task = kb.get_task(conn, board["bob"]["task"])
        finally:
            conn.close()
        assert results_acl.task_visible(_request_as("alice"), bob_task) is True

    def test_a_person_sees_their_own_card(self, multi_user, registry, kanban_home, board):
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            own = kb.get_task(conn, board["alice"]["task"])
        finally:
            conn.close()
        assert results_acl.task_visible(_request_as("alice"), own) is True

    def test_a_neighbours_card_is_visible_but_not_writable(self, multi_user, registry, kanban_home, board):
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            foreign = kb.get_task(conn, board["bob"]["task"])
        finally:
            conn.close()
        # Reading is TEAM: a neighbour's card and its materials are visible to any verified caller.
        assert results_acl.task_visible(_request_as("alice"), foreign) is True
        # Changing it is not: alice does not hold bob's work order.
        assert results_acl.task_writable(_request_as("alice"), foreign) is False

    def test_a_personal_contour_card_stays_private(self, multi_user, registry, kanban_home, board):
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            private = kb.get_task(conn, board["bob"]["task"])
        finally:
            conn.close()
        assert private is not None
        private.tenant = f"{results_acl.PERSONAL_TENANT_PREFIX}{BOB}"
        assert results_acl.task_visible(_request_as("alice"), private) is False
        assert results_acl.task_visible(_request_as("bob"), private) is True

    def test_require_task_refuses_a_change_to_a_neighbours_card(
            self, multi_user, registry, kanban_home, board):
        from fastapi import HTTPException

        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            foreign = kb.get_task(conn, board["bob"]["task"])
        finally:
            conn.close()
        results_acl.require_task(_request_as("alice"), foreign, board["bob"]["task"])  # read: allowed
        with pytest.raises(HTTPException) as caught:
            results_acl.require_task_write(_request_as("alice"), foreign, board["bob"]["task"])
        assert caught.value.status_code == 403

    def test_the_owner_sees_every_card(self, multi_user, registry, kanban_home, board):
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            foreign = kb.get_task(conn, board["bob"]["task"])
        finally:
            conn.close()
        assert results_acl.task_visible(_owner_request(), foreign) is True

    def test_card_folders_are_readable_team_material(self, multi_user, registry, kanban_home, board, profile_homes):
        """A card's workspace/attachments are TEAM material: a neighbour's folders are in the
        root too, because the deliverable has to be openable by whoever needs it. The private
        island is a PERSONAL-contour card, asserted separately below."""
        roots = results_acl.file_roots(_request_as("alice"))
        assert board["alice"]["path"].parent.resolve() in roots
        assert board["bob"]["path"].parent.resolve() in roots


# ---------------------------------------------------------------------------
# the Kanban dashboard routes — attachments by id are refused across people
# ---------------------------------------------------------------------------


def _load_kanban_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_plugin_results_scope_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


def _kanban_app(login):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = _Session(login)
        return await call_next(request)

    app.include_router(_load_kanban_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


class TestKanbanAttachmentRoutes:
    def test_own_attachment_downloads(self, multi_user, registry, kanban_home, board):
        response = _kanban_app("alice").get(
            f"/api/plugins/kanban/attachments/{board['alice']['attachment']}")
        assert response.status_code == 200
        assert response.content == b"alice's private notes"

    def test_a_neighbours_attachment_by_id_is_shared(self, multi_user, registry, kanban_home, board):
        # TEAM material: the file a producer made on bob's card must reach the person who needs
        # it, so a verified non-owner downloads it (this is the owner's headline requirement).
        response = _kanban_app("alice").get(
            f"/api/plugins/kanban/attachments/{board['bob']['attachment']}")
        assert response.status_code == 200
        assert response.content == b"bob's private notes"

    def test_an_anonymous_attachment_fetch_is_refused(self, multi_user, registry, kanban_home, board):
        # No verified identity at all (no session stamped, no native grant): the read boundary
        # refuses with the absent-id miss. A *verified* caller, by contrast, is admitted above.
        app = FastAPI()
        app.include_router(_load_kanban_router(), prefix="/api/plugins/kanban")
        response = TestClient(app).get(
            f"/api/plugins/kanban/attachments/{board['bob']['attachment']}")
        assert response.status_code == 404

    def test_a_neighbours_attachment_listing_is_shared(self, multi_user, registry, kanban_home, board):
        response = _kanban_app("alice").get(
            f"/api/plugins/kanban/tasks/{board['bob']['task']}/attachments")
        assert response.status_code == 200
        assert [a["filename"] for a in response.json()["attachments"]] == ["note.txt"]

    def test_own_attachment_listing_works(self, multi_user, registry, kanban_home, board):
        response = _kanban_app("alice").get(
            f"/api/plugins/kanban/tasks/{board['alice']['task']}/attachments")
        assert response.status_code == 200
        assert [a["filename"] for a in response.json()["attachments"]] == ["note.txt"]

    def test_deleting_a_neighbours_attachment_is_refused_and_the_blob_survives(
            self, multi_user, registry, kanban_home, board):
        # Reading is shared; deleting destroys the owner's deliverable, so it stays with the holder.
        response = _kanban_app("alice").delete(
            f"/api/plugins/kanban/attachments/{board['bob']['attachment']}")
        assert response.status_code == 404
        assert board["bob"]["path"].is_file()

    def test_the_owner_reads_any_attachment(self, multi_user, registry, kanban_home, board):
        response = _kanban_app(OWNER_LOGIN).get(
            f"/api/plugins/kanban/attachments/{board['bob']['attachment']}")
        assert response.status_code == 200

    def test_with_scoping_off_everything_works_as_before(self, legacy, kanban_home, board):
        response = _kanban_app("alice").get(
            f"/api/plugins/kanban/attachments/{board['bob']['attachment']}")
        assert response.status_code == 200


class TestTeamMaterialStaysOpen:
    """The owner's rule: what the team produced belongs to the team; only people stay separate."""

    def test_private_conversation_stays_closed_while_cards_open(
            self, multi_user, registry, kanban_home, board):
        """Acceptance #2: opening the board must not have opened anybody's dialog.

        The kanban change is measured first — a neighbour's card and its file ARE visible — and
        then the session gate is asked the same question and still says no. Without this pair the
        "we only opened materials" claim has no counter-example.
        """
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli.web_routers import _session_acl as session_acl

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            foreign_card = kb.get_task(conn, board["bob"]["task"])
        finally:
            conn.close()
        assert results_acl.task_visible(_request_as("alice"), foreign_card) is True

        # A session row owned by bob: alice's scope must not match it.
        alice_scope = session_acl.session_scope(_request_as("alice"))
        assert alice_scope is not None, "multi-user mode must scope the session surface"
        bob_row = {"user_id": f"person/bob", "session_id": "20261125_000000_bob"}
        assert session_acl.row_outside(bob_row, alice_scope) is True
        with pytest.raises(Exception) as caught:
            session_acl.require_row(bob_row, alice_scope)
        assert getattr(caught.value, "status_code", None) == 404, "a foreign dialog is a 404, not a 403"

    def test_the_board_is_not_narrowed_by_the_requesting_person(
            self, multi_user, registry, kanban_home, board):
        """A colleague's board must show the same cards as the owner's — measured, not assumed."""
        alice = _kanban_app("alice").get("/api/plugins/kanban/board").json()
        owner = _kanban_app(OWNER_LOGIN).get("/api/plugins/kanban/board").json()

        def count(payload):
            return sum(len(column["tasks"]) for column in payload["columns"])

        assert count(alice) == count(owner) == 2

    def test_a_created_card_records_the_real_requester(self, multi_user, registry, kanban_home, board):
        """``created_by`` is the person, never the constant "dashboard": it is the delivery address."""
        response = _kanban_app("alice").post(
            "/api/plugins/kanban/tasks", json={"title": "ordered by alice"})
        assert response.status_code == 200
        task_id = response.json()["task"]["id"]
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
        try:
            stored = kb.get_task(conn, task_id)
        finally:
            conn.close()
        assert stored is not None
        assert stored.created_by == ALICE

    def test_delivery_targets_reach_the_person_and_their_login(
            self, multi_user, registry, kanban_home):
        """The subscription address list starts with the subject and carries their stamps."""
        targets = results_acl.delivery_targets(ALICE)
        assert targets[0] == ALICE
        assert ALICE_ROOT in targets


# ---------------------------------------------------------------------------
# the dashboard file routes — /api/fs/* and /api/files*
# ---------------------------------------------------------------------------


@pytest.fixture
def fs_client_factory(profile_homes, monkeypatch):
    """Build a client over the REAL files router with a stamped session, no auth gate."""
    from hermes_cli.web_routers import files as files_router

    def build(login):
        app = FastAPI()

        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.session = _Session(login)
            return await call_next(request)

        app.include_router(files_router.router)
        return TestClient(app)

    # A plain host (no container, no forced root) so paths resolve as the caller typed them.
    monkeypatch.delenv("HERMES_DASHBOARD_FILES_ROOT", raising=False)
    monkeypatch.setattr(
        "hermes_cli.web_server_files._default_hermes_root_is_opt_data", lambda: False)
    return build


class TestFsRoutes:
    def test_own_file_reads(self, multi_user, registry, fs_client_factory, profile_homes):
        own = profile_homes[ALICE_ROOT] / "secret.txt"
        response = fs_client_factory("alice").get("/api/fs/read-text", params={"path": str(own)})
        assert response.status_code == 200
        assert "01-alice" in response.text

    def test_a_neighbours_file_is_refused(self, multi_user, registry, fs_client_factory, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        response = fs_client_factory("alice").get("/api/fs/read-text", params={"path": str(foreign)})
        assert response.status_code == 403
        assert "01-bob" not in response.text

    def test_traversal_out_of_the_root_is_refused(self, multi_user, registry, fs_client_factory, profile_homes):
        escape = f"{profile_homes[ALICE_ROOT]}/../{BOB_ROOT}/secret.txt"
        response = fs_client_factory("alice").get("/api/fs/read-text", params={"path": escape})
        assert response.status_code in {400, 403}

    def test_a_neighbours_directory_cannot_be_listed(self, multi_user, registry, fs_client_factory, profile_homes):
        response = fs_client_factory("alice").get(
            "/api/fs/list", params={"path": str(profile_homes[BOB_ROOT])})
        assert response.status_code == 403

    def test_own_directory_lists(self, multi_user, registry, fs_client_factory, profile_homes):
        response = fs_client_factory("alice").get(
            "/api/fs/list", params={"path": str(profile_homes[ALICE_ROOT])})
        assert response.status_code == 200
        assert "secret.txt" in [e["name"] for e in response.json()["entries"]]

    def test_writing_a_neighbours_file_is_refused(self, multi_user, registry, fs_client_factory, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "planted.txt"
        response = fs_client_factory("alice").post(
            "/api/fs/write-text", json={"path": str(foreign), "content": "pwned"})
        assert response.status_code == 403
        assert not foreign.exists()

    def test_writing_an_own_file_works(self, multi_user, registry, fs_client_factory, profile_homes):
        own = profile_homes[ALICE_ROOT] / "note.txt"
        response = fs_client_factory("alice").post(
            "/api/fs/write-text", json={"path": str(own), "content": "mine"})
        assert response.status_code == 200
        assert own.read_text(encoding="utf-8") == "mine"

    def test_the_owner_reads_a_neighbours_file(self, multi_user, registry, fs_client_factory, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        response = fs_client_factory(OWNER_LOGIN).get(
            "/api/fs/read-text", params={"path": str(foreign)})
        assert response.status_code == 200

    def test_with_scoping_off_a_path_is_served_as_before(self, legacy, fs_client_factory, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        response = fs_client_factory("alice").get(
            "/api/fs/read-text", params={"path": str(foreign)})
        assert response.status_code == 200


class TestManagedFilesRoutes:
    def test_a_neighbours_directory_cannot_be_listed(self, multi_user, registry, fs_client_factory, profile_homes):
        response = fs_client_factory("alice").get(
            "/api/files", params={"path": str(profile_homes[BOB_ROOT])})
        assert response.status_code == 403

    def test_own_directory_lists(self, multi_user, registry, fs_client_factory, profile_homes):
        response = fs_client_factory("alice").get(
            "/api/files", params={"path": str(profile_homes[ALICE_ROOT])})
        assert response.status_code == 200
        assert "secret.txt" in [e["name"] for e in response.json()["entries"]]

    def test_a_neighbours_file_cannot_be_read(self, multi_user, registry, fs_client_factory, profile_homes):
        response = fs_client_factory("alice").get(
            "/api/files/read", params={"path": str(profile_homes[BOB_ROOT] / "secret.txt")})
        assert response.status_code == 403

    def test_own_file_reads(self, multi_user, registry, fs_client_factory, profile_homes):
        response = fs_client_factory("alice").get(
            "/api/files/read", params={"path": str(profile_homes[ALICE_ROOT] / "secret.txt")})
        assert response.status_code == 200

    def test_deleting_a_neighbours_file_is_refused(self, multi_user, registry, fs_client_factory, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "secret.txt"
        response = fs_client_factory("alice").request(
            "DELETE", "/api/files", json={"path": str(foreign), "recursive": False})
        assert response.status_code == 403
        assert foreign.is_file()

    def test_mkdir_outside_the_root_is_refused(self, multi_user, registry, fs_client_factory, profile_homes):
        foreign = profile_homes[BOB_ROOT] / "planted"
        response = fs_client_factory("alice").post(
            "/api/files/mkdir", json={"path": str(foreign)})
        assert response.status_code == 403
        assert not foreign.exists()

    def test_with_scoping_off_a_foreign_file_still_reads(self, legacy, fs_client_factory, profile_homes):
        response = fs_client_factory("alice").get(
            "/api/files/read", params={"path": str(profile_homes[BOB_ROOT] / "secret.txt")})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# the dashboard skills and memory routes — the effective profile is the caller's
# ---------------------------------------------------------------------------


def _write_skill(skills_dir: Path, name: str, description: str = "test skill") -> None:
    target = skills_dir / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8")


@pytest.fixture
def skill_homes(profile_homes):
    """One skill per person's own home: ``alice-skill`` and ``bob-skill``."""
    for login, profile, skill in (
            ("alice", ALICE_ROOT, "alice-skill"), ("bob", BOB_ROOT, "bob-skill")):
        skills_dir = profile_homes[profile] / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        _write_skill(skills_dir, skill)
    return profile_homes


def _dash_app(router_modules, login):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = _Session(login)
        return await call_next(request)

    for module in router_modules:
        for name in ("hub_router", "router"):
            sub = getattr(module, name, None)
            if sub is not None:
                app.include_router(sub)
    return TestClient(app)


@pytest.fixture
def skills_client(skill_homes):
    from hermes_cli.web_routers import skills as skills_router

    def build(login):
        return _dash_app([skills_router], login)

    return build


class TestSkillsRoutes:
    def test_a_person_sees_only_their_own_skills(self, multi_user, registry, skills_client):
        body = skills_client("alice").get("/api/skills").json()
        names = [s["name"] for s in body]
        assert "alice-skill" in names
        assert "bob-skill" not in names

    def test_the_second_person_sees_the_symmetric_set(self, multi_user, registry, skills_client):
        names = [s["name"] for s in skills_client("bob").get("/api/skills").json()]
        assert "bob-skill" in names
        assert "alice-skill" not in names

    def test_naming_a_neighbours_profile_is_refused(self, multi_user, registry, skills_client):
        response = skills_client("alice").get("/api/skills", params={"profile": BOB_ROOT})
        assert response.status_code == 404

    def test_reading_a_neighbours_skill_content_is_refused(self, multi_user, registry, skills_client):
        response = skills_client("alice").get("/api/skills/content", params={"name": "bob-skill"})
        assert response.status_code in {404, 400}

    def test_own_skill_content_reads(self, multi_user, registry, skills_client):
        response = skills_client("alice").get("/api/skills/content", params={"name": "alice-skill"})
        assert response.status_code == 200

    def test_toggling_a_neighbours_profile_is_refused(self, multi_user, registry, skills_client):
        response = skills_client("alice").put(
            "/api/skills/toggle",
            json={"name": "bob-skill", "enabled": False, "profile": BOB_ROOT})
        assert response.status_code == 404

    def test_a_shared_profiles_skills_stay_available(self, multi_user, registry, skills_client, profile_homes):
        """Step 5's rule: 02–15 are shared — a person USES them, they are just nobody's own."""
        shared_dir = profile_homes[SHARED[0]] / "skills"
        shared_dir.mkdir(parents=True, exist_ok=True)
        _write_skill(shared_dir, "shared-skill")
        response = skills_client("alice").get("/api/skills", params={"profile": SHARED[0]})
        assert response.status_code == 200
        assert "shared-skill" in [s["name"] for s in response.json()]

    def test_the_owner_still_reads_every_profile(self, multi_user, registry, skills_client):
        response = skills_client(OWNER_LOGIN).get("/api/skills", params={"profile": BOB_ROOT})
        assert response.status_code == 200
        assert "bob-skill" in [s["name"] for s in response.json()]

    def test_with_scoping_off_the_requested_profile_is_honoured(self, legacy, skills_client):
        response = skills_client("alice").get("/api/skills", params={"profile": BOB_ROOT})
        assert response.status_code == 200
        assert "bob-skill" in [s["name"] for s in response.json()]


ALICE_NOTE = "alice's own note"
BOB_NOTE = "bob's own note"
OWNER_NOTE = "owner notes"


@pytest.fixture
def memory_client(profile_homes):
    """The memory routes over real homes: each profile carries its own ``memories`` tree.

    A profile's memory lives in THAT profile's home (``<profile home>/memories``), and the
    per-subject buckets sit under ``memories/u/<subject>`` — the layout ``memory_dirs_all``
    enumerates. Every profile here has one bucket plus the legacy base.
    """
    from hermes_cli.web_routers import ops as ops_router

    def _bucket(home, subject, note):
        bucket = home / "memories" / "u" / subject.replace("/", "_")
        bucket.mkdir(parents=True, exist_ok=True)
        (bucket / "MEMORY.md").write_text(note, encoding="utf-8")

    _bucket(profile_homes[ALICE_ROOT], ALICE, ALICE_NOTE)
    _bucket(profile_homes[BOB_ROOT], BOB, BOB_NOTE)
    (profile_homes[ALICE_ROOT] / "memories" / "MEMORY.md").write_text(
        "alice base", encoding="utf-8")
    (profile_homes["default"] / "memories").mkdir(parents=True, exist_ok=True)
    (profile_homes["default"] / "memories" / "MEMORY.md").write_text(
        OWNER_NOTE, encoding="utf-8")

    def build(login):
        return _dash_app([ops_router], login)

    return build


class TestMemoryRoutes:
    _ALICE_BYTES = len(ALICE_NOTE)
    _BOB_BYTES = len(BOB_NOTE)

    def test_a_person_counts_only_their_own_bucket(self, multi_user, registry, memory_client):
        body = memory_client("alice").get("/api/memory").json()
        assert body["builtin_files"]["memory"] == self._ALICE_BYTES

    def test_the_second_person_counts_only_theirs(self, multi_user, registry, memory_client):
        body = memory_client("bob").get("/api/memory").json()
        assert body["builtin_files"]["memory"] == self._BOB_BYTES

    def test_a_reset_erases_only_the_callers_bucket(self, multi_user, registry, memory_client, profile_homes):
        response = memory_client("alice").post("/api/memory/reset", json={"target": "all"})
        assert response.status_code == 200
        assert not (profile_homes[ALICE_ROOT] / "memories" / "u" / "person_alice" / "MEMORY.md").exists()
        assert (profile_homes[BOB_ROOT] / "memories" / "u" / "person_bob" / "MEMORY.md").is_file()

    def test_a_person_cannot_reset_a_neighbours_profile(self, multi_user, registry, memory_client, profile_homes):
        response = memory_client("alice").post(
            "/api/memory/reset", params={"profile": BOB_ROOT}, json={"target": "all"})
        assert response.status_code == 404
        assert (profile_homes[BOB_ROOT] / "memories" / "u" / "person_bob" / "MEMORY.md").is_file()

    def test_naming_a_neighbours_profile_is_refused(self, multi_user, registry, memory_client):
        response = memory_client("alice").get("/api/memory", params={"profile": BOB_ROOT})
        assert response.status_code == 404

    def test_the_owner_counts_a_named_profiles_every_bucket(self, multi_user, registry, memory_client):
        body = memory_client(OWNER_LOGIN).get(
            "/api/memory", params={"profile": BOB_ROOT}).json()
        # The owner is unscoped: the base AND the bucket, exactly as before.
        assert body["builtin_files"]["memory"] == self._BOB_BYTES

    def test_with_scoping_off_the_panel_counts_a_named_profiles_buckets(
            self, legacy, memory_client):
        body = memory_client("alice").get(
            "/api/memory", params={"profile": BOB_ROOT}).json()
        assert body["builtin_files"]["memory"] == self._BOB_BYTES
