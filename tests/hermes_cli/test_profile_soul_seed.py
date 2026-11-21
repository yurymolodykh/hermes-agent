"""Personal SOUL seed (``01-<login>``) and the personalization questionnaire.

МПД: a fresh named profile was seeded with ``DEFAULT_SOUL_MD`` — one sentence of generic text —
and ``--clone-config`` copied the orchestrator's whole SOUL, board protocol included. A person's
own root must instead open with a role and working rules, carry the questionnaire it can fill in,
and never contain instructions it must not execute.

Pinned here:
* the generated text (role, size, no service jargon, no leaked profile paths);
* what create does (personal root seeded, other names untouched, questionnaire placed);
* the questionnaire apply rule (empty answer = section unchanged; byte-identical otherwise);
* the ACL edges (foreign namespace 403, shared profile read-only).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import hermes_acl_soul as soul


#: Служебный жаргон оркестратора. Ни одного вхождения в SOUL пользователя.
FORBIDDEN = ("01-orchestrator", "Kanban", "карточк", "РГЗ", "upgreat", "needs_source", "profiles/")


# ---------------------------------------------------------------------------
# The generated document
# ---------------------------------------------------------------------------


class TestGeneratedSoul:
    def test_role_names_the_login(self):
        text = soul.build_user_soul("testuser")
        assert "Ты — персональный ассистент testuser" in text

    def test_size_is_inside_the_agreed_range(self):
        lines = soul.build_user_soul("testuser").splitlines()
        assert 40 <= len(lines) <= 60, f"{len(lines)} строк"

    @pytest.mark.parametrize("marker", FORBIDDEN)
    def test_no_service_jargon(self, marker):
        assert marker not in soul.build_user_soul("testuser")

    def test_keeps_the_shared_specialists_section(self):
        """02–14 are shared runtimes: a person may use them, so the text must say so."""
        text = soul.build_user_soul("testuser")
        assert "## Общие профили-специалисты" in text
        assert "общий ресурс" in text

    def test_drops_the_orchestrator_sections(self):
        text = soul.build_user_soul("testuser")
        for title in ("## Маршрутизация", "## Матрица проверки", "## Режимы работы", "## Выбор режима"):
            assert title not in text

    def test_has_the_questionnaire_sections(self):
        text = soul.build_user_soul("alice")
        for title in ("## Роль", "## Границы", "## Как вести работу", "## Ответ пользователю"):
            assert title in text

    def test_logins_are_isolated(self):
        assert "ассистент bob-smith" in soul.build_user_soul("bob-smith")
        assert "bob-smith" not in soul.build_user_soul("alice")

    @pytest.mark.parametrize("bad", ["..", ".hidden", "has space", "a/b", "x" * 65])
    def test_garbage_logins_stay_out_of_the_namespace(self, bad):
        assert soul.personal_soul_for(f"01-{bad}") is None

    @pytest.mark.parametrize("name", ["alice", "02-hermes-platform", "default", "01-", "16-personal"])
    def test_only_personal_roots_get_a_soul(self, name):
        assert soul.personal_soul_for(name) is None

    def test_bundled_source_works_without_an_orchestrator(self, monkeypatch):
        """A machine with no 01-orchestrator profile still gets a real document."""
        monkeypatch.setenv("HERMES_ORCHESTRATOR_SOUL", "/nonexistent/SOUL.md")
        text = soul.build_user_soul("carol")
        assert "персональный ассистент carol" in text
        assert len(text.splitlines()) >= 20


# ---------------------------------------------------------------------------
# An isolated home for every test here (the suite's own sandbox, but named locally so the
# profile-creation tests can point at it explicitly).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    import hermes_acl_bindings as hb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hb.reset_cache()
    yield home
    hb.reset_cache()


# ---------------------------------------------------------------------------
# Creating a profile
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(_isolated_home):
    return _isolated_home


class TestCreateProfileSeeds:
    def test_personal_root_gets_the_clone(self, home):
        from hermes_cli.profiles import create_profile

        path = create_profile("01-testuser", no_alias=True, no_skills=True)
        soul_md = (path / "SOUL.md").read_text(encoding="utf-8")
        assert "персональный ассистент testuser" in soul_md
        for marker in FORBIDDEN:
            assert marker not in soul_md

    def test_personal_root_gets_the_questionnaire(self, home):
        from hermes_cli.profiles import create_profile

        path = create_profile("01-testuser", no_alias=True, no_skills=True)
        questionnaire = path / soul.QUESTIONNAIRE_FILENAME
        assert questionnaire.is_file()
        assert "пустой ответ" in questionnaire.read_text(encoding="utf-8").lower()

    def test_other_profiles_keep_the_default_soul(self, home):
        from hermes_cli.default_soul import DEFAULT_SOUL_MD
        from hermes_cli.profiles import create_profile

        path = create_profile("17-plain", no_alias=True, no_skills=True)
        assert (path / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD
        assert not (path / soul.QUESTIONNAIRE_FILENAME).exists()

    def test_explicit_soul_wins(self, home):
        from hermes_cli.profiles import create_profile

        path = create_profile("01-testuser", no_alias=True, no_skills=True, soul="# мой SOUL\n")
        assert (path / "SOUL.md").read_text(encoding="utf-8") == "# мой SOUL\n"

    def test_clone_of_a_service_profile_does_not_carry_its_protocol(self, home):
        """``--clone-config`` from a service profile must not hand over its SOUL verbatim."""
        from hermes_cli.profiles import create_profile

        source = create_profile("02-hermes-platform", no_alias=True, no_skills=True)
        (source / "SOUL.md").write_text("# SOUL — сервис\n\n## Маршрутизация\n\n- Kanban\n", encoding="utf-8")
        path = create_profile("01-testuser", clone_from="02-hermes-platform", no_alias=True)
        text = (path / "SOUL.md").read_text(encoding="utf-8")
        assert "Kanban" not in text
        assert "персональный ассистент testuser" in text


# ---------------------------------------------------------------------------
# Applying the questionnaire
# ---------------------------------------------------------------------------


BASE = soul.build_user_soul("testuser")


class TestApplyQuestionnaire:
    def test_empty_answers_leave_the_document_byte_identical(self):
        after, report = soul.apply_questionnaire(BASE, {})
        assert after == BASE
        assert hashlib.sha256(after.encode()).digest() == hashlib.sha256(BASE.encode()).digest()
        assert report["changed_sections"] == []

    def test_address_form_goes_to_the_role(self):
        after, report = soul.apply_questionnaire(BASE, {"address_form": "ты"})
        assert "Обращение к пользователю: на «ты»." in after
        assert "Роль" in report["changed_sections"]

    def test_answer_length_goes_to_the_reply_section(self):
        after, report = soul.apply_questionnaire(BASE, {"answer_length": "кратко"})
        assert "- Длина ответа: кратко." in after
        assert "Ответ пользователю" in report["changed_sections"]

    def test_boundaries_are_only_what_was_ticked(self):
        after, _ = soul.apply_questionnaire(BASE, {"boundaries": ["no_external_uploads"]})
        assert "Не отправляй файлы и данные пользователя на внешние сервисы" in after
        assert "Перед удалением" not in after

    def test_work_section_follows_the_answers(self):
        after, _ = soul.apply_questionnaire(BASE, {"planning": "сразу"})
        assert "Начинай работу сразу" in after

    def test_untouched_sections_stay_byte_identical(self):
        after, _ = soul.apply_questionnaire(BASE, {"address_form": "ты"})
        body_before = BASE.split("## Инварианты", 1)[1]
        body_after = after.split("## Инварианты", 1)[1]
        assert body_before == body_after

    def test_applying_twice_is_idempotent(self):
        answers = {"address_form": "ты", "answer_length": "кратко", "display_name": "Алиса"}
        once, _ = soul.apply_questionnaire(BASE, answers)
        twice, _ = soul.apply_questionnaire(once, answers)
        assert once == twice

    def test_second_answer_replaces_the_first_instead_of_stacking(self):
        first, _ = soul.apply_questionnaire(BASE, {"answer_length": "кратко"})
        second, _ = soul.apply_questionnaire(first, {"answer_length": "подробно"})
        assert "- Длина ответа: подробно." in second
        assert "- Длина ответа: кратко." not in second

    def test_facts_5_6_become_memory_entries(self):
        entries = soul.questionnaire_memory_entries({
            "area": "судостроение", "terms": "ГСУ = гибридная силовая установка",
            "trusted_sources": "регламенты РФ", "extra": "любит короткие отчёты",
        })
        assert "Область работы: судостроение" in entries
        assert any("ГСУ" in e for e in entries)
        assert any("регламенты РФ" in e for e in entries)
        assert any("короткие отчёты" in e for e in entries)

    def test_empty_facts_produce_no_memory_entries(self):
        assert soul.questionnaire_memory_entries({}) == []


# ---------------------------------------------------------------------------
# HTTP: questionnaire path in the create answer, and the apply endpoint
# ---------------------------------------------------------------------------


pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_acl_identity
import hermes_acl_profiles as prof_acl
import hermes_acl_scope

ALICE, BOB = "person/alice", "person/bob"
ALICE_ROOT, BOB_ROOT = "01-alice", "01-bob"
SHARED = "02-hermes-platform"


@pytest.fixture()
def registry(_isolated_home):
    """A real on-disk registry: alice → 01-alice, bob → 01-bob, 02 shared."""
    import hermes_acl_bindings as hb

    path = _isolated_home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-21T00:00:00+00:00", "written_by": "admin",
        "persons": [
            {"slug": ALICE, "display_name": "Alice", "shared": False, "profiles": [ALICE_ROOT],
             "subjects": [], "owner": ALICE, "source": "admin"},
            {"slug": BOB, "display_name": "Bob", "shared": False, "profiles": [BOB_ROOT],
             "subjects": [], "owner": BOB, "source": "admin"},
            {"slug": "shared/service", "display_name": "Shared", "shared": True,
             "profiles": [SHARED], "subjects": [], "owner": "admin", "source": "admin"},
        ],
    }), encoding="utf-8")
    hb.reset_cache()
    yield path
    hb.reset_cache()


def _client(login: str) -> TestClient:
    """The profiles router alone, with *login* as the verified dashboard session."""
    from hermes_cli.web_routers.profiles import router, sessions_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = _Session(login)
        return await call_next(request)

    app.include_router(sessions_router)
    app.include_router(router)
    return TestClient(app)


class _Session:
    def __init__(self, user_id: str, provider: str = "basic", issuer: str = "") -> None:
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


@pytest.fixture()
def multi_user(monkeypatch):
    """``acl.multi_user=true`` with the users-file rule stubbed (no users file needed)."""
    monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
    monkeypatch.setattr(hermes_acl_identity, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(prof_acl, "multi_user_enabled", lambda: True)

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        return f"person/{login}" if login else fallback

    monkeypatch.setattr(hermes_acl_identity, "resolve_subject", _resolve)
    return True


@pytest.fixture()
def self_service(monkeypatch):
    monkeypatch.setattr(prof_acl, "self_service_enabled", lambda: True)
    return True


class TestCreateAnswerCarriesTheQuestionnaire:
    def test_personal_create_names_the_path(self, _isolated_home):
        r = _client("alice").post("/api/profiles", json={"name": "01-testuser", "no_skills": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["questionnaire_path"].endswith(soul.QUESTIONNAIRE_FILENAME)
        assert Path(body["questionnaire_path"]).is_file()

    def test_non_personal_create_reports_no_questionnaire(self, _isolated_home):
        r = _client("alice").post("/api/profiles", json={"name": "17-plain", "no_skills": True})
        assert r.status_code == 200, r.text
        assert r.json()["questionnaire_path"] == ""


class TestApplyQuestionnaireEndpoint:
    def _seed(self, name: str) -> Path:
        from hermes_cli.profiles import create_profile

        return create_profile(name, no_alias=True, no_skills=True)

    def test_applies_the_answers(self, _isolated_home):
        path = self._seed("01-testuser")
        r = _client("alice").post("/api/profiles/01-testuser/soul/apply-questionnaire",
                                  json={"address_form": "ты", "answer_length": "кратко"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["changed"] is True
        text = (path / "SOUL.md").read_text(encoding="utf-8")
        assert "на «ты»" in text and "- Длина ответа: кратко." in text
        assert body["soul_sha256"] == hashlib.sha256(text.encode()).hexdigest()

    def test_empty_questionnaire_leaves_the_file_alone(self, _isolated_home):
        path = self._seed("01-testuser")
        before = hashlib.sha256((path / "SOUL.md").read_bytes()).hexdigest()
        r = _client("alice").post("/api/profiles/01-testuser/soul/apply-questionnaire", json={})
        assert r.status_code == 200, r.text
        after = hashlib.sha256((path / "SOUL.md").read_bytes()).hexdigest()
        assert before == after
        assert r.json()["changed"] is False

    def test_memory_entries_are_written(self, _isolated_home):
        path = self._seed("01-testuser")
        r = _client("alice").post("/api/profiles/01-testuser/soul/apply-questionnaire",
                                  json={"area": "судостроение", "terms": "ГСУ = гибридная СУ"})
        assert r.status_code == 200, r.text
        entries = r.json()["memory_entries"]
        assert any("судостроение" in e for e in entries)
        memory = path / "memories" / "MEMORY.md"
        assert memory.is_file() and "судостроение" in memory.read_text(encoding="utf-8")

    def test_a_foreign_person_is_refused(self, _isolated_home, registry, multi_user):
        self._seed("01-testuser")
        r = _client("bob").post("/api/profiles/01-testuser/soul/apply-questionnaire",
                                json={"answer_length": "кратко"})
        # Not visible at all → the 404 an absent name gets (a 403 would confirm it exists).
        assert r.status_code == 404, r.text

    def test_a_shared_profile_is_read_only(self, _isolated_home, registry, multi_user):
        self._seed(SHARED)
        r = _client("alice").post(f"/api/profiles/{SHARED}/soul/apply-questionnaire",
                                  json={"answer_length": "кратко"})
        assert r.status_code == 403, r.text

    def test_a_profile_without_a_soul_is_refused(self, _isolated_home):
        from hermes_cli.profiles import get_profile_dir

        directory = get_profile_dir("17-nosoul")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.yaml").write_text("{}\n", encoding="utf-8")
        r = _client("alice").post("/api/profiles/17-nosoul/soul/apply-questionnaire",
                                  json={"answer_length": "кратко"})
        assert r.status_code == 400, r.text


class TestForeignNamespaceCreate:
    def test_a_person_cannot_create_in_a_foreign_namespace(self, _isolated_home, registry, multi_user, self_service):
        r = _client("alice").post("/api/profiles", json={"name": BOB_ROOT, "no_skills": True})
        assert r.status_code == 403, r.text

    def test_a_person_may_create_their_own_root(self, _isolated_home, registry, multi_user, self_service):
        r = _client("bob").post("/api/profiles", json={"name": "01-bob", "no_skills": True})
        assert r.status_code == 200, r.text
