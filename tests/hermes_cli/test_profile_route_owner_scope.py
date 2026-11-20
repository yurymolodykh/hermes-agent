"""A person's own and shared profiles — the МПД Т2/Т3/Т4 rule (step 5).

The hole: ``GET /api/profiles`` listed every profile directory, ``POST /api/profiles`` created
any name, and every ``?profile=`` fan-out (sessions list, sidebar, projects tree, pull requests)
plus every ``/api/profiles/{name}/…`` route answered for ANY name. One login therefore
enumerated the whole roster, created a profile inside somebody else's namespace and read another
person's orchestrator state.

Two layers are pinned here, and they are deliberately separate:

* :mod:`hermes_acl_profiles` — WHICH profiles a subject may see, use, modify and create. Its unit
  tests need no HTTP at all.
* the dashboard routes — the same rule applied at each seam, with the refusal shape a client can
  rely on: a foreign profile answers **404**, byte-identical to an absent one (a 403 would confirm
  the name exists and turn the roster into an enumeration oracle); a host-wide destructive
  operation answers **403**.

``acl.multi_user`` off (the shipped default) changes NOTHING — every test that asserts the legacy
answer for the owner is also an assertion that a single-user install keeps its dashboard.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_acl_identity
import hermes_acl_profiles as prof_acl
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
# fixtures: identity mode, registry, profile directories
# ---------------------------------------------------------------------------


class _Session:
    """The verified dashboard login (``request.state.session``) the auth gate attaches."""

    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test owns its HERMES_HOME; nothing here may touch the operator's real registry."""
    import hermes_acl_bindings as hb

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
    import hermes_acl_bindings as hb

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
    """``acl.multi_user=true`` with the users-file rule stubbed (no users file needed).

    ``resolve_subject`` is the step-3 rule: a ``role: owner`` login keeps the host owner's
    subject, everybody else becomes ``person/<login>``.
    """
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_scope, "identity_trust", lambda: "untrusted")
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: True)

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
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: False)
    return False


@pytest.fixture
def self_service(monkeypatch):
    """``acl.profile_self_service=true`` — a person may create their own ``01-<login>``."""
    monkeypatch.setattr(prof_acl, "self_service_enabled", lambda: True)
    return True


# ---------------------------------------------------------------------------
# hermes_acl_profiles — the rule itself
# ---------------------------------------------------------------------------


ALL_NAMES = ["default", ALICE_ROOT, BOB_ROOT, *SHARED, "04-web-researcher"]


class TestVisibleProfiles:
    def test_scoping_off_returns_every_name(self, legacy):
        assert list(prof_acl.visible_profiles(ALICE, ALL_NAMES)) == ALL_NAMES

    def test_scoping_off_grants_any_profile(self, legacy):
        assert prof_acl.allows(ALICE, BOB_ROOT) is True

    def test_owner_keeps_the_whole_roster(self, multi_user, registry):
        assert list(prof_acl.visible_profiles(host_owner_subject(), ALL_NAMES)) == ALL_NAMES

    def test_owner_needs_no_registry_record(self, multi_user):
        assert list(prof_acl.visible_profiles(host_owner_subject(), ALL_NAMES)) == ALL_NAMES

    def test_person_sees_own_and_shared_only(self, multi_user, registry):
        assert list(prof_acl.visible_profiles(ALICE, ALL_NAMES)) == [ALICE_ROOT, *SHARED]

    def test_second_person_sees_a_symmetric_set(self, multi_user, registry):
        assert list(prof_acl.visible_profiles(BOB, ALL_NAMES)) == [BOB_ROOT, *SHARED]

    def test_a_person_never_sees_anothers_root(self, multi_user, registry):
        assert BOB_ROOT not in prof_acl.visible_profiles(ALICE, ALL_NAMES)

    def test_unbound_shared_profiles_are_not_visible(self, multi_user, registry):
        # 04-web-researcher exists on disk but no registry record names it.
        assert "04-web-researcher" not in prof_acl.visible_profiles(ALICE, ALL_NAMES)

    def test_absent_registry_grants_nothing(self, multi_user):
        assert prof_acl.visible_profiles(ALICE, ALL_NAMES) == ()

    def test_corrupt_registry_grants_nothing(self, multi_user, _isolated_home):
        path = _isolated_home / "acl" / "profile-bindings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert prof_acl.visible_profiles(ALICE, ALL_NAMES) == ()

    def test_broken_registry_still_serves_the_owner(self, multi_user, _isolated_home):
        path = _isolated_home / "acl" / "profile-bindings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert list(prof_acl.visible_profiles(host_owner_subject(), ALL_NAMES)) == ALL_NAMES

    def test_identityless_subject_sees_only_public_profiles(self, multi_user, registry):
        # No usable subject means no personal namespace — but a SHARED profile carries no personal
        # data by construction, so it stays reachable. The dashboard's auth gate refuses a request
        # with no session long before this, so the answer only has to stay non-widening.
        assert list(prof_acl.visible_profiles("", ALL_NAMES)) == list(SHARED)

    def test_empty_name_list_stays_empty(self, multi_user, registry):
        assert prof_acl.visible_profiles(ALICE, []) == ()

    def test_allows_own_profile(self, multi_user, registry):
        assert prof_acl.allows(ALICE, ALICE_ROOT) is True

    def test_allows_shared_profile(self, multi_user, registry):
        assert prof_acl.allows(ALICE, SHARED[0]) is True

    def test_refuses_foreign_profile(self, multi_user, registry):
        assert prof_acl.allows(ALICE, BOB_ROOT) is False

    def test_refuses_empty_name(self, multi_user, registry):
        assert prof_acl.allows(ALICE, "") is False


class TestNames:
    @pytest.mark.parametrize("subject,expected", [
        (ALICE, "alice"),
        (BOB, "bob"),
        (host_owner_subject(), ""),
        ("", ""),
        ("uid:1001", ""),
    ])
    def test_login_of(self, subject, expected):
        assert prof_acl.login_of(subject) == expected

    @pytest.mark.parametrize("login,expected", [
        ("alice", "01-alice"),
        ("Alice", "01-alice"),
        ("a.b@example.com", "01-a.b-example.com"),
        ("", ""),
    ])
    def test_personal_root(self, login, expected):
        assert prof_acl.personal_root(login) == expected

    @pytest.mark.parametrize("profile,expected", [
        ("01-alice", "alice"),
        ("02-hermes-platform", ""),
        ("default", ""),
        ("", ""),
    ])
    def test_root_login(self, profile, expected):
        assert prof_acl.root_login(profile) == expected

    def test_shared_candidates_picks_the_mission_range(self):
        names = ["01-orchestrator", "02-a", "15-b", "16-c", "default", "x-y", "03-software-engineer"]
        assert list(prof_acl.shared_candidates(names)) == ["02-a", "15-b", "03-software-engineer"]


# ---------------------------------------------------------------------------
# the dashboard routes — the rule at each seam
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_homes(_isolated_home, monkeypatch):
    """A real profile set on disk: default + 01-alice + 01-bob + two shared profiles."""
    from hermes_cli import profiles as profiles_mod
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    root = default_home / "profiles"
    homes = {"default": default_home}
    for name in (ALICE_ROOT, BOB_ROOT, *SHARED):
        home = root / name
        home.mkdir(parents=True, exist_ok=True)
        # config.yaml is the identity marker every profile enumerator looks for.
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        homes[name] = home
    (default_home / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: root)
    return homes


@pytest.fixture
def app_client(profile_homes):
    """The profiles router alone, so no dashboard auth gate stands between the test and it."""
    from hermes_cli.web_routers.profiles import router, sessions_router

    app = FastAPI()
    app.include_router(sessions_router)
    app.include_router(router)
    return TestClient(app)


def _client_as(_unused, login):
    """A FRESH client whose requests carry *login* as the verified dashboard session.

    A new app per call: ``TestClient`` cannot grow middleware once it has started, and one test
    here asks two different subjects the same question.
    """
    from hermes_cli.web_routers.profiles import router, sessions_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = _Session(login, "basic", "")
        return await call_next(request)

    app.include_router(sessions_router)
    app.include_router(router)
    return TestClient(app)


def _names(payload):
    return [row["name"] for row in payload["profiles"]]


class TestProfilesListing:
    def test_a_person_gets_own_and_shared_only(self, multi_user, registry, app_client):
        # Criterion 2: no 01-B in a subject-A answer.
        body = _client_as(app_client, "alice").get("/api/profiles").json()
        assert _names(body) == [ALICE_ROOT, *SHARED]
        assert BOB_ROOT not in _names(body)

    def test_the_second_person_gets_a_symmetric_answer(self, multi_user, registry, app_client):
        body = _client_as(app_client, "bob").get("/api/profiles").json()
        assert _names(body) == [BOB_ROOT, *SHARED]

    def test_shared_profiles_are_visible_to_both(self, multi_user, registry, app_client):
        # Criterion 4: 02..15 visible to two different subjects.
        alice = _names(_client_as(app_client, "alice").get("/api/profiles").json())
        bob = _names(_client_as(app_client, "bob").get("/api/profiles").json())
        assert set(SHARED) <= set(alice) and set(SHARED) <= set(bob)

    def test_the_owner_keeps_the_whole_roster(self, multi_user, registry, app_client):
        # Criterion 5: no loss of admin visibility.
        body = _client_as(app_client, OWNER_LOGIN).get("/api/profiles").json()
        assert set(_names(body)) == {"default", ALICE_ROOT, BOB_ROOT, *SHARED}

    def test_acl_off_lists_everything(self, legacy, app_client):
        body = _client_as(app_client, "alice").get("/api/profiles").json()
        assert set(_names(body)) == {"default", ALICE_ROOT, BOB_ROOT, *SHARED}

    def test_acl_off_with_no_identity_lists_everything(self, legacy, app_client):
        # The pre-МПД dashboard had no per-user identity at all; it must not start hiding rows.
        body = app_client.get("/api/profiles").json()
        assert set(_names(body)) == {"default", ALICE_ROOT, BOB_ROOT, *SHARED}


class TestProfileRead:
    def test_a_person_reads_their_own_soul(self, multi_user, registry, app_client, profile_homes):
        (profile_homes[ALICE_ROOT] / "SOUL.md").write_text("# alice\n", encoding="utf-8")
        r = _client_as(app_client, "alice").get(f"/api/profiles/{ALICE_ROOT}/soul")
        assert r.status_code == 200 and r.json()["content"] == "# alice\n"

    def test_a_foreign_soul_is_404_identical_to_an_absent_one(
            self, multi_user, registry, app_client, profile_homes):
        (profile_homes[BOB_ROOT] / "SOUL.md").write_text("# bob secret\n", encoding="utf-8")
        cli = _client_as(app_client, "alice")
        foreign = cli.get(f"/api/profiles/{BOB_ROOT}/soul")
        absent = cli.get("/api/profiles/99-nobody/soul")
        assert foreign.status_code == absent.status_code == 404
        assert "bob secret" not in foreign.text
        assert foreign.json() == {"detail": f"Profile '{BOB_ROOT}' does not exist."}

    def test_a_shared_profile_is_readable(self, multi_user, registry, app_client, profile_homes):
        (profile_homes[SHARED[0]] / "SOUL.md").write_text("# shared\n", encoding="utf-8")
        r = _client_as(app_client, "alice").get(f"/api/profiles/{SHARED[0]}/soul")
        assert r.status_code == 200

    def test_the_owner_reads_any_soul(self, multi_user, registry, app_client, profile_homes):
        (profile_homes[BOB_ROOT] / "SOUL.md").write_text("# bob\n", encoding="utf-8")
        r = _client_as(app_client, OWNER_LOGIN).get(f"/api/profiles/{BOB_ROOT}/soul")
        assert r.status_code == 200 and r.json()["content"] == "# bob\n"

    def test_setup_command_for_a_foreign_profile_is_404(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").get(f"/api/profiles/{BOB_ROOT}/setup-command")
        assert r.status_code == 404

    def test_desktop_overlay_for_a_foreign_profile_is_404(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").get(f"/api/profiles/{BOB_ROOT}/desktop-overlay")
        assert r.status_code == 404


class TestProfileWrites:
    def test_writing_a_foreign_soul_is_404(self, multi_user, registry, app_client, profile_homes):
        (profile_homes[BOB_ROOT] / "SOUL.md").write_text("# bob\n", encoding="utf-8")
        r = _client_as(app_client, "alice").put(
            f"/api/profiles/{BOB_ROOT}/soul", json={"content": "# taken over\n"})
        assert r.status_code == 404
        # And nothing was written.
        assert (profile_homes[BOB_ROOT] / "SOUL.md").read_text(encoding="utf-8") == "# bob\n"

    def test_writing_a_shared_profile_is_refused(self, multi_user, registry, app_client, profile_homes):
        """A shared profile is a runtime several people use — not one person's to reconfigure."""
        (profile_homes[SHARED[0]] / "SOUL.md").write_text("# shared\n", encoding="utf-8")
        r = _client_as(app_client, "alice").put(
            f"/api/profiles/{SHARED[0]}/soul", json={"content": "# mine now\n"})
        assert r.status_code == 403
        assert (profile_homes[SHARED[0]] / "SOUL.md").read_text(encoding="utf-8") == "# shared\n"

    def test_writing_own_soul_works(self, multi_user, registry, app_client, profile_homes):
        r = _client_as(app_client, "alice").put(
            f"/api/profiles/{ALICE_ROOT}/soul", json={"content": "# alice\n"})
        assert r.status_code == 200
        assert (profile_homes[ALICE_ROOT] / "SOUL.md").read_text(encoding="utf-8") == "# alice\n"

    def test_the_owner_writes_a_shared_profile(self, multi_user, registry, app_client, profile_homes):
        r = _client_as(app_client, OWNER_LOGIN).put(
            f"/api/profiles/{SHARED[0]}/soul", json={"content": "# by owner\n"})
        assert r.status_code == 200

    def test_acl_off_keeps_every_write(self, legacy, app_client, profile_homes):
        r = _client_as(app_client, "alice").put(
            f"/api/profiles/{BOB_ROOT}/soul", json={"content": "# legacy\n"})
        assert r.status_code == 200


class TestProfileCreate:
    def test_creating_a_foreign_root_is_refused(self, multi_user, registry, app_client):
        # Criterion 3: a foreign name is an HTTP refusal, not 200.
        r = _client_as(app_client, "alice").post("/api/profiles", json={"name": BOB_ROOT})
        assert r.status_code == 403
        assert r.json()["detail"] == "profile_not_allowed"

    def test_creating_an_arbitrary_name_is_refused(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").post("/api/profiles", json={"name": "07-sneaky"})
        assert r.status_code == 403

    def test_default_policy_is_admin_only(self, multi_user, registry, app_client):
        """§6.2 default: a person creates NOTHING, not even their own root."""
        r = _client_as(app_client, "alice").post("/api/profiles", json={"name": ALICE_ROOT})
        assert r.status_code == 403

    def test_self_service_allows_the_callers_own_root(
            self, multi_user, self_service, registry, app_client, profile_homes):
        from hermes_cli import profiles as profiles_mod

        r = _client_as(app_client, "carol").post("/api/profiles", json={"name": "01-carol"})
        assert r.status_code == 200, r.text
        assert profiles_mod.get_profile_dir("01-carol").is_dir()

    def test_self_service_still_refuses_a_foreign_root(
            self, multi_user, self_service, registry, app_client):
        r = _client_as(app_client, "carol").post("/api/profiles", json={"name": BOB_ROOT})
        assert r.status_code == 403

    def test_self_service_refuses_a_name_bound_to_someone_else(
            self, multi_user, self_service, registry, app_client):
        """``01-alice`` is already in alice's record: carol cannot take the name over."""
        r = _client_as(app_client, "alice").post("/api/profiles", json={"name": "01-carol"})
        assert r.status_code == 403

    def test_the_owner_creates_anything(self, multi_user, registry, app_client, profile_homes):
        r = _client_as(app_client, OWNER_LOGIN).post("/api/profiles", json={"name": "07-extra"})
        assert r.status_code == 200, r.text

    def test_acl_off_creates_anything(self, legacy, app_client, profile_homes):
        r = _client_as(app_client, "alice").post("/api/profiles", json={"name": "07-extra"})
        assert r.status_code == 200, r.text


class TestActiveProfile:
    def test_a_foreign_sticky_profile_is_not_named(self, multi_user, registry, app_client,
                                                   profile_homes, monkeypatch):
        """``active`` is host-wide; naming another person's orchestrator is a leak by itself."""
        from hermes_cli import profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "get_active_profile", lambda: BOB_ROOT)
        monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: BOB_ROOT)
        body = _client_as(app_client, "alice").get("/api/profiles/active").json()
        assert body["active"] == ALICE_ROOT
        assert BOB_ROOT not in (body["active"], body["current"])

    def test_the_owner_sees_the_real_sticky_profile(self, multi_user, registry, app_client,
                                                    profile_homes, monkeypatch):
        from hermes_cli import profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "get_active_profile", lambda: BOB_ROOT)
        monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: BOB_ROOT)
        body = _client_as(app_client, OWNER_LOGIN).get("/api/profiles/active").json()
        assert body["active"] == BOB_ROOT

    def test_a_person_cannot_set_the_host_wide_active_profile(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").post("/api/profiles/active", json={"name": ALICE_ROOT})
        assert r.status_code == 403

    def test_the_owner_sets_it(self, multi_user, registry, app_client, profile_homes):
        r = _client_as(app_client, OWNER_LOGIN).post("/api/profiles/active", json={"name": SHARED[0]})
        assert r.status_code == 200, r.text

    def test_acl_off_sets_it(self, legacy, app_client, profile_homes):
        r = _client_as(app_client, "alice").post("/api/profiles/active", json={"name": SHARED[0]})
        assert r.status_code == 200, r.text


class TestOwnerOnlyOperations:
    def test_delete_refuses_a_non_owner(self, multi_user, registry, app_client, profile_homes):
        """Deleting removes a profile for everyone who used it."""
        r = _client_as(app_client, "alice").delete(f"/api/profiles/{ALICE_ROOT}")
        assert r.status_code == 403
        assert profile_homes[ALICE_ROOT].is_dir()

    def test_export_refuses_a_non_owner(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").post(
            f"/api/profiles/{ALICE_ROOT}/export", json={"output": "/tmp/x.tar.gz"})
        assert r.status_code == 403

    def test_import_refuses_a_non_owner(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").post(
            "/api/profiles/import", json={"archive": "/tmp/x.tar.gz"})
        assert r.status_code == 403

    def test_rename_refuses_a_foreign_target(self, multi_user, registry, app_client, profile_homes):
        r = _client_as(app_client, "alice").patch(
            f"/api/profiles/{BOB_ROOT}", json={"new_name": "01-alice"})
        assert r.status_code == 404

    def test_rename_refuses_landing_in_a_foreign_namespace(self, multi_user, registry, app_client,
                                                           profile_homes):
        r = _client_as(app_client, "alice").patch(
            f"/api/profiles/{ALICE_ROOT}", json={"new_name": BOB_ROOT})
        assert r.status_code == 403


class TestFanOutScope:
    """``?profile=`` selectors and the all-profile fan-out over other people's state.db."""

    def test_foreign_profile_sessions_are_404(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").get(
            "/api/profiles/sessions", params={"profile": BOB_ROOT})
        assert r.status_code == 404

    def test_own_profile_sessions_are_200(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").get(
            "/api/profiles/sessions", params={"profile": ALICE_ROOT})
        assert r.status_code == 200

    def test_all_sessions_never_open_a_foreign_store(self, multi_user, registry, app_client,
                                                     profile_homes, monkeypatch):
        """The fan-out itself, not just the ``?profile=`` selector: ``all`` must open only the
        caller's profiles — a widened selector would be a roster leak nothing else catches."""
        from hermes_cli.web_routers import profiles as routes
        opened = []
        real = routes._read_profile_db

        def _spy(name, home, errors, read):
            opened.append(name)
            return real(name, home, errors, read)

        monkeypatch.setattr(routes, "_read_profile_db", _spy)
        _client_as(app_client, "alice").get("/api/profiles/sessions", params={"profile": "all"})
        assert opened, "the fan-out opened no store at all"
        assert BOB_ROOT not in opened
        assert set(opened) <= {ALICE_ROOT, *SHARED}

    def test_sidebar_foreign_scope_is_404(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").get(
            "/api/profiles/sessions/sidebar", params={"recents_profile": BOB_ROOT})
        assert r.status_code == 404

    def test_projects_tree_foreign_scope_is_404(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").get(
            "/api/profiles/projects/tree", params={"profile": BOB_ROOT})
        assert r.status_code == 404

    def test_pull_requests_foreign_scope_is_404(self, multi_user, registry, app_client):
        r = _client_as(app_client, "alice").post(
            "/api/profiles/sessions/pull-requests",
            params={"profile": BOB_ROOT}, json={"ids": ["some-session"]})
        assert r.status_code == 404

    def test_acl_off_keeps_every_scope(self, legacy, app_client):
        # projects/tree is left out: importing ``tui_gateway.server`` here would touch the real
        # checkout through ``hermes_bootstrap`` (tests/home_io_guard); its legacy behaviour is
        # pinned by ``test_profiles_sidebar_scope.py`` against the real app.
        for path, params in (("/api/profiles/sessions", {"profile": BOB_ROOT}),
                             ("/api/profiles/sessions/sidebar", {"recents_profile": BOB_ROOT})):
            assert app_client.get(path, params=params).status_code == 200, path
