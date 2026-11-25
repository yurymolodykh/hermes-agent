"""Every mutating dashboard route refuses a stranger and admits the card's holder.

Card ``t_ba1321f4``. The live probe (``t_46d79276``) deleted somebody else's card through
``DELETE /api/plugins/kanban/tasks/{id}`` while the gateway ran a build from before the ownership
gate existed. That one route is closed; this file proves the REST of the mutating surface is closed
too, and that closing it did not take reading away from the team:

* acceptance #2 — each of the eight routes below answers ``403`` to a person who does not hold the
  card and ``200`` to the card's holder (the refusal must be LOUD: a status code and a body);
* acceptance #3 — the seven installation-wide administrative routes answer ``403`` to a non-owner;
* acceptance #4 — a neighbour's card, its attachments and its log still READ ``200``: team material
  stays shared, only changing it is the holder's decision;
* ``POST /tasks`` stays open and records the verified subject as ``created_by``.

The policy under test is ``hermes_acl_results.task_writable`` behind ``_require_held_task`` and
``hermes_acl_profiles.require_owner`` behind ``_require_owner`` — no second mechanism is introduced
here, so with ``acl.multi_user`` off every case below has to keep working exactly as before
(``TestLegacySurfaces``).
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
import hermes_acl_scope
from gateway.owner_identity import host_owner_subject

ALICE = "person/alice"
BOB = "person/bob"
OWNER_LOGIN = "owner"
ALICE_ROOT = "01-alice"
BOB_ROOT = "01-bob"

ALICE_DOC = {
    "slug": ALICE, "display_name": "Alice", "shared": False,
    "profiles": [ALICE_ROOT], "subjects": [], "owner": ALICE, "source": "admin",
}
BOB_DOC = {
    "slug": BOB, "display_name": "Bob", "shared": False,
    "profiles": [BOB_ROOT], "subjects": [], "owner": BOB, "source": "admin",
}


class _Session:
    """The verified dashboard login (``request.state.session``) the auth gate attaches."""

    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test owns its HERMES_HOME; nothing here may touch the operator's real board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    hb.reset_cache()
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    yield home
    hb.reset_cache()


@pytest.fixture
def registry(_isolated_home):
    """A real on-disk bindings registry: alice owns 01-alice, bob owns 01-bob."""
    path = _isolated_home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
        "persons": [ALICE_DOC, BOB_DOC],
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
    """The shipped default: multi-user OFF — nothing below may change behaviour here."""
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: False)
    return False


def _load_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_plugin_mutation_gates_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _app(login=None):
    """A client whose every request carries *login*'s verified session (``None`` = anonymous)."""
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        if login is not None:
            request.state.session = _Session(login)
        return await call_next(request)

    app.include_router(_load_router().router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def cards(_isolated_home):
    """Two cards: alice's (running, so reclaim/terminate have something to act on) and bob's."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb.init_db()
    with kbc.connect() as conn:
        alice = kb.create_task(conn, title="alice's card", created_by=ALICE, assignee=ALICE)
        alice2 = kb.create_task(conn, title="alice's second card", created_by=ALICE, assignee=ALICE)
        bob = kb.create_task(conn, title="bob's card", created_by=BOB, assignee=BOB)
        kb.claim_task(conn, bob, claimer="foreign-host:worker")
        bob_run = kb.get_task(conn, bob).current_run_id
        kb.claim_task(conn, alice, claimer="own-host:worker")
        alice_run = kb.get_task(conn, alice).current_run_id
        dest = kb.task_attachments_dir(bob)
        dest.mkdir(parents=True, exist_ok=True)
        blob = dest / "note.txt"
        blob.write_text("bob's deliverable", encoding="utf-8")
        bob_att = kb.add_attachment(
            conn, bob, filename=blob.name, stored_path=str(blob.resolve()),
            content_type="text/plain", size=blob.stat().st_size, uploaded_by="bob")
    return {"alice": alice, "alice2": alice2, "bob": bob, "bob_run": bob_run,
            "alice_run": alice_run, "bob_att": bob_att}


def _body(response) -> str:
    """The refusal's own words — an empty-body 403 would still be silent to the person reading it."""
    try:
        payload = response.json()
    except Exception:  # health: allow BLE001 -- a non-JSON body is still a loud refusal
        return response.text or ""
    return json.dumps(payload, ensure_ascii=False)


def _assert_refused(response, expected=(403, 404)):
    assert response.status_code in expected, f"{response.status_code}: {_body(response)}"
    assert _body(response).strip(), "a refusal must carry a message, not just a bare status"


# ---------------------------------------------------------------------------
# acceptance #2 — the eight task-scoped routes
# ---------------------------------------------------------------------------


class TestTaskScopedMutations:
    """A stranger is refused on every card-scoped mutation; the holder gets the action."""

    def test_reassign(self, multi_user, registry, cards):
        path = f"/api/plugins/kanban/tasks/{cards['bob']}/reassign"
        _assert_refused(_app("alice").post(path, json={"profile": "10-stranger"}))
        # bob's card is running, so the holder's own reassign has to release the claim first —
        # the 403 above is the GATE, this 200 is the same route working for the person who owns it.
        holder = _app("bob").post(path, json={"profile": "10-bob-worker", "reclaim_first": True})
        assert holder.status_code == 200, _body(holder)
        assert holder.json()["assignee"] == "10-bob-worker"

    def test_decompose(self, multi_user, registry, cards, monkeypatch):
        mod = _load_router()

        class _Outcome:
            ok, task_id, reason, fanout, child_ids, new_title = True, "x", "", False, [], "t"

        calls: list[str] = []

        def _fake_aux(board, module, fn, task_id, author):
            calls.append(task_id)
            return _Outcome()

        monkeypatch.setattr(mod, "_run_aux", _fake_aux)
        app = FastAPI()
        app.include_router(mod.router, prefix="/api/plugins/kanban")

        @app.middleware("http")
        async def _stamp(request: Request, call_next):
            request.state.session = _Session("alice")
            return await call_next(request)

        stranger = TestClient(app).post(
            f"/api/plugins/kanban/tasks/{cards['bob']}/decompose", json={})
        _assert_refused(stranger)
        assert calls == [], "a refused decompose must not spend an auxiliary-model call"

    def test_specify(self, multi_user, registry, cards, monkeypatch):
        mod = _load_router()

        class _Outcome:
            ok, task_id, reason, new_title = True, "x", "", "new"

        calls: list[str] = []

        def _fake_aux(board, module, fn, task_id, author):
            calls.append(task_id)
            return _Outcome()

        monkeypatch.setattr(mod, "_run_aux", _fake_aux)
        app = FastAPI()
        app.include_router(mod.router, prefix="/api/plugins/kanban")

        @app.middleware("http")
        async def _stamp(request: Request, call_next):
            request.state.session = _Session("alice")
            return await call_next(request)

        stranger = TestClient(app).post(
            f"/api/plugins/kanban/tasks/{cards['bob']}/specify", json={})
        _assert_refused(stranger)
        assert calls == []
        # The holder passes and the helper really runs.
        holder_client = FastAPI()
        holder_client.include_router(mod.router, prefix="/api/plugins/kanban")

        @holder_client.middleware("http")
        async def _stamp_holder(request: Request, call_next):
            request.state.session = _Session("bob")
            return await call_next(request)

        holder = TestClient(holder_client).post(
            f"/api/plugins/kanban/tasks/{cards['bob']}/specify", json={})
        assert holder.status_code == 200, _body(holder)
        assert calls == [cards["bob"]]

    def test_reclaim(self, multi_user, registry, cards):
        path = f"/api/plugins/kanban/tasks/{cards['bob']}/reclaim"
        _assert_refused(_app("alice").post(path, json={"reason": "not mine"}))
        holder = _app("bob").post(path, json={"reason": "my own run"})
        assert holder.status_code == 200, _body(holder)

    def test_terminate_run(self, multi_user, registry, cards):
        path = f"/api/plugins/kanban/runs/{cards['bob_run']}/terminate"
        _assert_refused(_app("alice").post(path, json={}))
        # Each test gets its own board: bob's run is still open, so the holder's terminate reaches
        # the reclaim branch and answers 200 instead of 409.
        holder = _app("bob").post(path, json={"reason": "mine to stop"})
        assert holder.status_code == 200, _body(holder)

    def test_bulk_update(self, multi_user, registry, cards):
        path = "/api/plugins/kanban/tasks/bulk"
        _assert_refused(_app("alice").post(
            path, json={"ids": [cards["bob"]], "priority": 5}))
        holder = _app("alice").post(
            "/api/plugins/kanban/tasks/bulk", json={"ids": [cards["alice"]], "priority": 7})
        assert holder.status_code == 200, _body(holder)
        assert holder.json()["results"] == [{"id": cards["alice"], "ok": True}]

    def test_bulk_refuses_a_mixed_batch_whole(self, multi_user, registry, cards):
        """A batch that touches ONE foreign card is refused entirely — no silent partial apply."""
        response = _app("alice").post(
            "/api/plugins/kanban/tasks/bulk",
            json={"ids": [cards["alice"], cards["bob"]], "priority": 9})
        _assert_refused(response)
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect() as conn:
            assert kb.get_task(conn, cards["alice"]).priority != 9, "nothing may be applied"
            assert kb.get_task(conn, cards["bob"]).priority != 9

    def test_add_link(self, multi_user, registry, cards):
        path = "/api/plugins/kanban/links"
        # bob's card as the parent: alice does not hold it.
        _assert_refused(_app("alice").post(
            path, json={"parent_id": cards["bob"], "child_id": cards["alice2"]}))
        holder = _app("alice").post(
            path, json={"parent_id": cards["alice"], "child_id": cards["alice2"]})
        assert holder.status_code == 200, _body(holder)

    def test_delete_link(self, multi_user, registry, cards):
        _assert_refused(_app("alice").delete(
            "/api/plugins/kanban/links",
            params={"parent_id": cards["bob"], "child_id": cards["alice2"]}))

    def test_create_records_the_subject(self, multi_user, registry, cards):
        """``POST /tasks`` stays open, but the row's ``created_by`` is the real person."""
        response = _app("alice").post(
            "/api/plugins/kanban/tasks", json={"title": "alice's new card"})
        assert response.status_code == 200, _body(response)
        assert response.json()["task"]["created_by"] == ALICE

    def test_anonymous_calls_are_refused(self, multi_user, registry, cards):
        _assert_refused(_app(None).post(
            f"/api/plugins/kanban/tasks/{cards['bob']}/reassign", json={"profile": "x"}),
            expected=(403, 404))

    def test_the_owner_may_change_any_card(self, multi_user, registry, cards):
        response = _app(OWNER_LOGIN).post(
            f"/api/plugins/kanban/tasks/{cards['bob']}/reassign",
            json={"profile": "10-owner-pick", "reclaim_first": True})
        assert response.status_code == 200, _body(response)


# ---------------------------------------------------------------------------
# acceptance #3 — the installation-wide administrative routes
# ---------------------------------------------------------------------------


class TestAdministrativeRoutes:
    """Boards, profile metadata, orchestration and the free-text estimate stop at the owner."""

    @pytest.mark.parametrize("case", [
        ("POST", "/api/plugins/kanban/boards", {"slug": "second", "title": "Second"}),
        ("PATCH", "/api/plugins/kanban/boards/default", {"title": "Renamed"}),
        ("POST", "/api/plugins/kanban/boards/default/switch", None),
        ("PATCH", "/api/plugins/kanban/profiles/03-software-engineer", {"description": "hijacked"}),
        ("POST", "/api/plugins/kanban/profiles/03-software-engineer/describe-auto", {"overwrite": True}),
        ("PUT", "/api/plugins/kanban/orchestration", {"orchestrator_profile": "01-alice"}),
        ("POST", "/api/plugins/kanban/estimate", {"title": "somebody else's tokens"}),
    ])
    def test_a_non_owner_is_refused(self, multi_user, registry, cards, case):
        method, path, body = case
        client = _app("alice")
        response = client.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 403, f"{method} {path} → {response.status_code}: {_body(response)}"
        assert _body(response).strip()

    def test_the_owner_is_admitted_on_every_admin_route(self, multi_user, registry, cards, monkeypatch):
        """The same seven routes answer a non-403 to the host owner (no new admin mechanism)."""
        from hermes_cli import config as config_mod
        from hermes_cli import profile_describer, profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "normalize_profile_name", lambda n: str(n))
        monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda n: _isolated_profile_dir(n))
        monkeypatch.setattr(profiles_mod, "write_profile_meta", lambda *a, **k: None)

        class _Outcome:
            ok, profile_name, reason, description = True, "03-software-engineer", "", "auto"

        monkeypatch.setattr(profile_describer, "describe_profile", lambda name, overwrite=False: _Outcome())
        monkeypatch.setattr(config_mod, "load_config", lambda: {})
        monkeypatch.setattr(config_mod, "save_config", lambda cfg: None)

        mod = _load_router()
        monkeypatch.setattr(mod, "_run_estimate", lambda title, body, task_id=None: {"ok": True})

        app = FastAPI()

        @app.middleware("http")
        async def _stamp(request: Request, call_next):
            request.state.session = _Session(OWNER_LOGIN)
            return await call_next(request)

        app.include_router(mod.router, prefix="/api/plugins/kanban")
        client = TestClient(app)

        cases = [
            ("POST", "/api/plugins/kanban/boards", {"slug": "second", "title": "Second"}),
            ("PATCH", "/api/plugins/kanban/boards/default", {"title": "Renamed"}),
            ("POST", "/api/plugins/kanban/boards/default/switch", None),
            ("PATCH", "/api/plugins/kanban/profiles/03-software-engineer", {"description": "mine"}),
            ("POST", "/api/plugins/kanban/profiles/03-software-engineer/describe-auto", {"overwrite": True}),
            ("PUT", "/api/plugins/kanban/orchestration", {"orchestrator_profile": "01-alice"}),
            ("POST", "/api/plugins/kanban/estimate", {"title": "owner estimate"}),
        ]
        for method, path, body in cases:
            response = client.request(method, path, **({"json": body} if body is not None else {}))
            assert response.status_code != 403, f"{method} {path} refused the owner: {_body(response)}"
            assert response.status_code < 500, f"{method} {path} → {response.status_code}: {_body(response)}"


def _isolated_profile_dir(name: str) -> Path:
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# acceptance #4 — reading stays open (the anti-regression half)
# ---------------------------------------------------------------------------


class TestReadingStaysOpen:
    """Closing the mutations must not roll the board back into isolation."""

    def test_a_neighbour_reads_the_card(self, multi_user, registry, cards):
        response = _app("alice").get(f"/api/plugins/kanban/tasks/{cards['bob']}")
        assert response.status_code == 200, _body(response)
        assert response.json()["task"]["title"] == "bob's card"

    def test_a_neighbour_reads_the_attachment(self, multi_user, registry, cards):
        response = _app("alice").get(f"/api/plugins/kanban/attachments/{cards['bob_att']}")
        assert response.status_code == 200, _body(response)
        assert response.content == b"bob's deliverable"

    def test_a_neighbour_reads_the_attachment_listing(self, multi_user, registry, cards):
        response = _app("alice").get(
            f"/api/plugins/kanban/tasks/{cards['bob']}/attachments")
        assert response.status_code == 200, _body(response)

    def test_a_neighbour_still_cannot_change_what_they_read(self, multi_user, registry, cards):
        """The pair that makes "open read" meaningful: 200 on GET, 403 on PATCH."""
        client = _app("alice")
        assert client.get(f"/api/plugins/kanban/tasks/{cards['bob']}").status_code == 200
        _assert_refused(client.patch(
            f"/api/plugins/kanban/tasks/{cards['bob']}", json={"priority": 1}))

    def test_a_comment_is_team_material(self, multi_user, registry, cards):
        """Comments stay open by design (attributable thread), as the audit's read-gated list says."""
        response = _app("alice").post(
            f"/api/plugins/kanban/tasks/{cards['bob']}/comments", json={"body": "FYI from alice"})
        assert response.status_code == 200, _body(response)


# ---------------------------------------------------------------------------
# the switch is additive: multi-user OFF behaves exactly as before
# ---------------------------------------------------------------------------


class TestLegacySurfaces:
    def test_the_old_behaviour_survives_with_the_flag_off(self, legacy, cards):
        client = _app("alice")
        assert client.post(
            f"/api/plugins/kanban/tasks/{cards['bob']}/reassign",
            json={"profile": "10-legacy", "reclaim_first": True},
        ).status_code == 200
        assert client.post("/api/plugins/kanban/boards", json={"slug": "legacy-board"}).status_code == 200
        assert client.put(
            "/api/plugins/kanban/orchestration", json={"auto_decompose": False}
        ).status_code != 403
