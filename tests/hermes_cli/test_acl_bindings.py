"""The bindings registry: format, validation, fail-closed loading, backups, migration.

``hermes_acl_bindings`` answers "which profiles belong to which person" and "are these two subjects
one person" for steps 3-6 of the multi-user mission. Every test here runs against an EXPLICIT
registry path (``tmp_path``), never the default one: the default is anchored to the Hermes ROOT, so
a test that used it would write into the operator's real access model.

Two properties carry the weight and are asserted from several directions:

* **fail-closed** — a file that is present but does not validate raises, and the readers that are
  allowed to degrade (``load_cached``) degrade to ABSENCE, never to a wider answer;
* **additive** — no file means every existing comparison keeps its exact previous answer, which is
  what lets step 2 land before step 3 turns identity on.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import hermes_acl_bindings as hb
from gateway import owner_identity


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_cache():
    """The store caches by path; a test must never inherit another test's view."""
    hb.reset_cache()
    yield
    hb.reset_cache()


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "acl" / "profile-bindings.json"


@pytest.fixture
def store(path) -> hb.BindingStore:
    return hb.BindingStore(path)


def _valid_document(**overrides) -> dict:
    document = {
        "schema": hb.SCHEMA,
        "written_at": "2026-11-20T00:00:00+00:00",
        "written_by": "admin",
        "persons": [
            {
                "slug": "person/yury",
                "display_name": "Юрий",
                "shared": False,
                "profiles": ["01-orchestrator"],
                "subjects": ["uid:1001", "cron-owner"],
                "owner": "person/yury",
                "source": "admin",
                "created_at": "2026-11-20T00:00:00+00:00",
                "updated_at": "2026-11-20T00:00:00+00:00",
            }
        ],
    }
    document.update(overrides)
    return document


def _write(path: Path, document, *, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


@pytest.fixture
def bound(store) -> hb.Bindings:
    """One person holding two profiles, plus a shared set — the shape step 3 expects."""
    registry = store.bootstrap_owner(
        slug="person/yury", profiles=["01-orchestrator", "16-personal"],
        subjects=["uid:1001", "cron-owner", "125805788"], display_name="Юрий",
        shared_profiles=["02-hermes-platform", "03-software-engineer"],
    )
    return registry


# ---------------------------------------------------------------------------
# 1. The document: format, round-trip, stable render
# ---------------------------------------------------------------------------


class TestDocument:
    def test_empty_store_reads_as_absent_not_as_error(self, store):
        assert store.exists() is False
        assert store.read() is None
        assert store.load().persons == {}

    def test_write_then_read_round_trips_the_record(self, store, bound):
        reread = store.read(use_cache=False)
        record = reread.record("person/yury")
        assert record.profiles == ("01-orchestrator", "16-personal")
        assert record.subjects == ("uid:1001", "cron-owner", "125805788")
        assert record.display_name == "Юрий"
        assert record.shared is False

    def test_file_is_0600(self, store, path):
        store.bootstrap_owner(slug="person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        assert stat.S_IMODE(path.stat().st_mode) == hb.FILE_MODE == 0o600

    def test_render_is_stable_and_sorted(self, store):
        store.bind("person/bob", profiles=["04-web-researcher"], subjects=["tg:2"])
        store.bind("person/amy", profiles=["05-rag-knowledge-engineer"], subjects=["tg:1"])
        text = store.path.read_text(encoding="utf-8")
        first = text.index("person/amy")
        assert first < text.index("person/bob")
        assert json.loads(text)["persons"][0]["slug"] == "person/amy"

    def test_document_is_a_single_json_object_not_jsonl(self, store):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        parsed = json.loads(store.path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict) and "persons" in parsed
        assert len(store.path.read_text(encoding="utf-8").splitlines()) > 1  # indented, still one object

    def test_missing_optional_fields_get_defaults(self, path, store):
        _write(path, {"persons": [{"slug": "person/yury", "profiles": ["01-orchestrator"]}]})
        record = store.read().record("person/yury")
        assert record.display_name == "person/yury"
        assert record.owner == "person/yury"
        assert record.subjects == ()
        assert record.shared is False

    def test_schema_field_is_required_to_match(self, path, store):
        _write(path, _valid_document(schema=99))
        with pytest.raises(hb.BindingsCorrupt, match="unsupported schema"):
            store.read()


# ---------------------------------------------------------------------------
# 2. Fail-closed: a broken file is a REFUSAL, never a partial registry
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_invalid_json_raises_with_the_path(self, path, store):
        _write(path, "{not json")
        with pytest.raises(hb.BindingsCorrupt, match="invalid JSON"):
            store.read()

    def test_top_level_list_is_corrupt(self, path, store):
        _write(path, [{"slug": "person/yury"}])
        with pytest.raises(hb.BindingsCorrupt, match="not a JSON object"):
            store.read()

    def test_duplicate_record_is_corrupt(self, path, store):
        record = _valid_document()["persons"][0]
        _write(path, _valid_document(persons=[record, record]))
        with pytest.raises(hb.BindingsCorrupt, match="duplicate record"):
            store.read()

    def test_subject_claimed_by_two_people_is_corrupt(self, path, store):
        first = _valid_document()["persons"][0]
        second = dict(first, slug="person/alice", profiles=["05-rag-knowledge-engineer"])
        _write(path, _valid_document(persons=[first, second]))
        with pytest.raises(hb.BindingsCorrupt, match="claimed by both"):
            store.read()

    def test_profile_claimed_by_two_people_is_corrupt(self, path, store):
        first = _valid_document()["persons"][0]
        second = dict(first, slug="person/alice", subjects=["tg:7"])
        _write(path, _valid_document(persons=[first, second]))
        with pytest.raises(hb.BindingsCorrupt, match="belongs to both"):
            store.read()

    @pytest.mark.parametrize("slug", ["yury", "person/", "person_/x", "admin", "person/Yury", "/etc/passwd"])
    def test_invalid_slug_is_corrupt(self, path, store, slug):
        record = dict(_valid_document()["persons"][0], slug=slug)
        _write(path, _valid_document(persons=[record]))
        with pytest.raises(hb.BindingsCorrupt):
            store.read()

    def test_person_record_cannot_be_shared(self, path, store):
        record = dict(_valid_document()["persons"][0], shared=True)
        _write(path, _valid_document(persons=[record]))
        with pytest.raises(hb.BindingsCorrupt, match="cannot be shared"):
            store.read()

    def test_shared_record_must_be_marked_shared(self, path, store):
        record = dict(_valid_document()["persons"][0], slug="shared/service", shared=False)
        _write(path, _valid_document(persons=[record]))
        with pytest.raises(hb.BindingsCorrupt, match="must be marked shared"):
            store.read()

    @pytest.mark.parametrize("profile", ["../etc", "a/b", "-bad", "UPPER", "with space", ""])
    def test_invalid_profile_name_is_corrupt(self, path, store, profile):
        record = dict(_valid_document()["persons"][0], profiles=[profile])
        _write(path, _valid_document(persons=[record]))
        with pytest.raises(hb.BindingsCorrupt):
            store.read()

    def test_profile_list_of_wrong_type_is_corrupt(self, path, store):
        record = dict(_valid_document()["persons"][0], profiles="01-orchestrator")
        _write(path, _valid_document(persons=[record]))
        with pytest.raises(hb.BindingsCorrupt, match="expected a list"):
            store.read()

    def test_subject_entry_of_wrong_type_is_corrupt(self, path, store):
        record = dict(_valid_document()["persons"][0], subjects=[7])
        _write(path, _valid_document(persons=[record]))
        with pytest.raises(hb.BindingsCorrupt, match="not a bare non-empty string"):
            store.read()

    def test_shared_flag_of_wrong_type_is_corrupt(self, path, store):
        record = dict(_valid_document()["persons"][0], shared="yes")
        _write(path, _valid_document(persons=[record]))
        with pytest.raises(hb.BindingsCorrupt, match="must be true/false"):
            store.read()

    def test_a_broken_file_never_yields_a_partial_registry(self, path, store):
        """The whole point: one bad record voids the file. Half a registry looks like 'that
        person merely has no profiles', which is the wrong conclusion to draw silently."""
        good = _valid_document()["persons"][0]
        broken = dict(good, slug="person/broken", profiles=["../escape"], subjects=["tg:7"])
        _write(path, _valid_document(persons=[good, broken]))
        with pytest.raises(hb.BindingsCorrupt):
            store.read()
        with pytest.raises(hb.BindingsCorrupt):  # and it refuses again on the next read
            store.load()

    def test_unreadable_file_raises_unavailable(self, path, store):
        if os.geteuid() == 0:  # root reads through 0000
            pytest.skip("running as root: permission bits do not deny reads")
        _write(path, _valid_document())
        os.chmod(path, 0o000)
        try:
            with pytest.raises(hb.BindingsUnavailable):
                store.read()
        finally:
            os.chmod(path, 0o600)

    def test_group_readable_file_warns_but_loads(self, path, store, caplog):
        _write(path, _valid_document(), mode=0o644)
        with caplog.at_level("WARNING"):
            registry = store.read()
        assert registry.record("person/yury") is not None
        assert any("readable beyond its owner" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# 3. Lookups: who is this subject, whose are these profiles
# ---------------------------------------------------------------------------


class TestLookups:
    def test_person_of_own_slug(self, bound):
        assert bound.person_of("person/yury") == "person/yury"

    def test_person_of_a_recorded_subject(self, bound):
        assert bound.person_of("uid:1001") == "person/yury"
        assert bound.person_of("cron-owner") == "person/yury"
        assert bound.person_of("125805788") == "person/yury"

    @pytest.mark.parametrize("subject", ["uid:2002", "person/alice", "", None, "   ", "person/yur", "YURY"])
    def test_person_of_unknown_is_none(self, bound, subject):
        assert bound.person_of(subject) is None

    def test_subjects_of_includes_the_slug_itself(self, bound):
        assert bound.subjects_of("uid:1001") == ("person/yury", "uid:1001", "cron-owner", "125805788")

    def test_profiles_of_person(self, bound):
        assert bound.profiles_of("uid:1001") == ("01-orchestrator", "16-personal")

    def test_profiles_of_unknown_is_empty(self, bound):
        assert bound.profiles_of("nobody") == ()

    def test_shared_profiles_are_not_personal(self, bound):
        assert bound.personal_profiles_of("uid:1001") == ("01-orchestrator", "16-personal")
        assert bound.shared_profiles() == ("02-hermes-platform", "03-software-engineer")

    def test_is_shared(self, bound):
        assert bound.is_shared("02-hermes-platform") is True
        assert bound.is_shared("01-orchestrator") is False
        assert bound.is_shared("") is False
        assert bound.is_shared("no-such-profile") is False

    def test_profile_owner_prefers_the_person(self, bound):
        assert bound.profile_owner("01-orchestrator") == "person/yury"
        assert bound.profile_owner("02-hermes-platform") == "shared/service"
        assert bound.profile_owner("unknown") is None

    def test_allows_profile_grants_own_and_shared(self, store, bound):
        store.bind("person/alice", profiles=["01-alice"], subjects=["uid:2002"])
        registry = store.load()
        assert registry.allows_profile("uid:1001", "01-orchestrator") is True
        assert registry.allows_profile("uid:2002", "01-alice") is True
        assert registry.allows_profile("uid:2002", "02-hermes-platform") is True  # shared
        assert registry.allows_profile("uid:2002", "01-orchestrator") is False  # someone else's
        assert registry.allows_profile("uid:1001", "01-alice") is False
        assert registry.allows_profile("stranger", "02-hermes-platform") is False

    def test_allows_profile_on_an_empty_registry_is_false(self, store):
        assert store.load().allows_profile("uid:1001", "01-orchestrator") is False


# ---------------------------------------------------------------------------
# 4. same_person: the one comparison steps 3-6 call
# ---------------------------------------------------------------------------


class TestSamePerson:
    def test_a_subject_is_itself(self):
        assert hb.same_person("person/yury", "person/yury") is True

    def test_stamps_of_one_person_agree(self, path, bound):
        assert hb.same_person("uid:1001", "cron-owner", bindings=bound) is True
        assert hb.same_person("uid:1001", "person/yury", bindings=bound) is True

    def test_two_people_never_agree(self, store, bound):
        store.bind("person/alice", profiles=["01-alice"], subjects=["uid:2002"])
        registry = store.load()
        assert hb.same_person("uid:1001", "uid:2002", bindings=registry) is False
        assert hb.same_person("person/yury", "person/alice", bindings=registry) is False

    def test_the_measured_gap_is_closed_by_a_binding(self, path, bound):
        """The exact pair the mission measured as False before step 2 existed."""
        assert hb.same_person("uid:1001", "person/alice", bindings=bound) is False
        store = hb.BindingStore(path)
        store.bind("person/alice", profiles=["01-alice"], subjects=["uid:1001"])
        assert hb.same_person("uid:1001", "person/alice", bindings=store.load()) is True

    @pytest.mark.parametrize("left,right", [("", "x"), ("x", ""), (None, "x"), ("x", None), ("", "")])
    def test_empty_never_agrees(self, left, right, bound):
        assert hb.same_person(left, right, bindings=bound) is False

    def test_unknown_subjects_never_agree(self, bound):
        assert hb.same_person("uid:9999", "uid:8888", bindings=bound) is False

    def test_no_registry_is_false_not_an_error(self, monkeypatch, path):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        assert hb.same_person("uid:1001", "person/yury") is False

    def test_case_is_not_folded_into_a_match(self, bound):
        """A different case is a DIFFERENT stamp; folding it would let ``UID:1001`` claim a row."""
        assert hb.same_person("UID:1001", "person/yury", bindings=bound) is False

    def test_surrounding_whitespace_is_trimmed_on_both_sides(self, bound):
        """A header value may carry stray spaces; trimming never creates a cross-person match."""
        assert hb.same_person(" uid:1001 ", "person/yury", bindings=bound) is True
        assert hb.same_person(" uid:1001 ", "person/alice", bindings=bound) is False


# ---------------------------------------------------------------------------
# 5. Writes: rebinding, conflicts, cascade, atomicity
# ---------------------------------------------------------------------------


class TestWrites:
    def test_bind_creates_and_updates(self, store):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        registry = store.bind("person/yury", profiles=["16-personal"], subjects=["cron-owner"],
                              replace_subjects=False)
        record = registry.record("person/yury")
        assert record.profiles == ("01-orchestrator", "16-personal")  # union
        assert record.subjects == ("uid:1001", "cron-owner")

    def test_rebinding_a_subject_moves_it(self, store):
        """One stamp may name only ONE person: binding it elsewhere takes it away."""
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        store.bind("person/alice", profiles=["01-alice"], subjects=["uid:1001"])
        registry = store.load()
        assert registry.record("person/yury").subjects == ()
        assert registry.person_of("uid:1001") == "person/alice"
        assert registry.person_of("person/yury") == "person/yury"

    def test_rebinding_keeps_created_at_and_rewrites_the_document(self, store):
        first = store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        created = first.record("person/yury").created_at
        assert created
        second = store.bind("person/yury", profiles=["16-personal"], replace_subjects=False)
        assert second.record("person/yury").created_at == created
        assert second.record("person/yury").updated_at >= created

    @pytest.mark.parametrize("slug", ["yury", "admin", "person/", "shared/x/y", ""])
    def test_bind_rejects_an_invalid_slug(self, store, slug):
        with pytest.raises(hb.BindingsConflict):
            store.bind(slug, profiles=["01-orchestrator"])

    def test_bind_rejects_a_profile_held_by_another_person(self, store):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        with pytest.raises(hb.BindingsCorrupt, match="belongs to both"):
            store.bind("person/alice", profiles=["01-orchestrator"], subjects=["uid:2002"])

    def test_unbind_removes_the_record(self, store, bound):
        registry = store.unbind("person/yury")
        assert registry.record("person/yury") is None
        assert registry.person_of("uid:1001") is None

    def test_unbind_of_a_missing_record_is_a_no_op(self, store):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        before = store.path.read_text(encoding="utf-8")
        store.unbind("person/ghost")
        assert store.path.read_text(encoding="utf-8") == before

    def test_set_shared_replaces_the_shared_set(self, store, bound):
        registry = store.set_shared(["05-rag-knowledge-engineer"])
        assert registry.shared_profiles() == ("05-rag-knowledge-engineer",)
        assert registry.is_shared("02-hermes-platform") is False

    def test_set_shared_rejects_an_invalid_profile(self, store):
        with pytest.raises(hb.BindingsConflict):
            store.set_shared(["../etc"])

    def test_a_failed_write_leaves_the_previous_file_intact(self, store):
        good = store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        before = store.path.read_text(encoding="utf-8")
        with pytest.raises(hb.BindingsCorrupt):
            store.bind("person/alice", profiles=["01-orchestrator"], subjects=["uid:2002"])
        assert store.path.read_text(encoding="utf-8") == before
        assert store.read(use_cache=False).record("person/yury") == good.record("person/yury")

    def test_write_leaves_no_temp_files(self, store):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        leftovers = [p.name for p in store.path.parent.iterdir() if p.name.startswith(".tmp_")]
        assert leftovers == []


# ---------------------------------------------------------------------------
# 6. Backups: every change keeps the previous model recoverable
# ---------------------------------------------------------------------------


class TestBackups:
    def test_first_write_has_no_backup(self, store, path):
        assert store.backup() is None
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        assert not (hb.backups_dir(path) / hb.BACKUP_LATEST).exists()

    def test_every_change_writes_latest_and_a_stamped_copy(self, store, path):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        store.bind("person/yury", profiles=["16-personal"], replace_subjects=False)
        store.bind("person/alice", profiles=["01-alice"], subjects=["uid:2002"])
        latest = hb.backup_path_latest(path)
        assert latest.exists()
        stamped = sorted(p for p in hb.backups_dir(path).glob(f"{hb.BACKUP_PREFIX}*.json")
                         if p.name != hb.BACKUP_LATEST)
        assert len(stamped) == 2  # the two changes that HAD a previous version
        assert stat.S_IMODE(latest.stat().st_mode) == 0o600

    def test_latest_holds_the_version_before_the_last_change(self, store, path):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        store.bind("person/alice", profiles=["01-alice"], subjects=["uid:2002"])
        snapshot = json.loads(hb.backup_path_latest(path).read_text(encoding="utf-8"))
        slugs = [p["slug"] for p in snapshot["persons"]]
        assert slugs == ["person/yury"]  # alice is the NEW version, not the backup

    def test_restoring_the_backup_returns_the_previous_model(self, store, path):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        store.bind("person/alice", profiles=["01-alice"], subjects=["uid:2002"])
        hb.copy_registry(hb.backup_path_latest(path), path)
        assert store.read(use_cache=False).record("person/alice") is None
        assert store.read(use_cache=False).record("person/yury") is not None

    def test_rotation_keeps_a_bounded_number_of_copies(self, store, path, monkeypatch):
        monkeypatch.setattr(hb, "BACKUP_KEEP", 3)
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        for index in range(6):
            store.bind("person/yury", profiles=[f"01-p{index}"], replace_subjects=False)
        stamped = [p for p in hb.backups_dir(path).glob(f"{hb.BACKUP_PREFIX}*.json")
                   if p.name != hb.BACKUP_LATEST]
        assert len(stamped) == 3

    def test_backup_survives_an_unreadable_registry(self, path, store):
        """A corrupt file cannot be parsed but CAN be copied: the operator's escape hatch."""
        _write(path, "{not json")
        with pytest.raises(hb.BindingsCorrupt):
            store.read()
        copied = store.backup()
        assert copied is not None and copied.exists()
        assert copied.read_text(encoding="utf-8") == "{not json"


# ---------------------------------------------------------------------------
# 7. Bootstrap / migration: the owner keeps their 246 sessions
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_owner_bootstrap_records_the_legacy_stamps(self, store):
        registry = store.bootstrap_owner(
            slug="person/yury", profiles=["01-orchestrator"],
            subjects=["uid:1001", "cron-owner"], display_name="Юрий")
        record = registry.record("person/yury")
        assert record.profiles == ("01-orchestrator",)
        assert record.subjects == ("uid:1001", "cron-owner")
        assert record.source == "bootstrap-owner"

    def test_owner_bootstrap_is_idempotent(self, store, path):
        first = store.bootstrap_owner(slug="person/yury", profiles=["01-orchestrator"],
                                      subjects=["uid:1001", "cron-owner"])
        before = store.path.read_text(encoding="utf-8")
        second = store.bootstrap_owner(slug="person/yury", profiles=["01-orchestrator"],
                                       subjects=["uid:1001", "cron-owner"])
        assert first.to_json() == second.to_json()  # same document, no double record
        assert len(second.persons) == 1
        assert store.path.read_text(encoding="utf-8") == before  # and not even rewritten
        assert not hb.backup_path_latest(path).exists()  # so no backup churn on a re-run

    def test_owner_bootstrap_adds_the_shared_set_in_the_same_run(self, store):
        registry = store.bootstrap_owner(
            slug="person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"],
            shared_profiles=["02-hermes-platform"])
        assert registry.shared_profiles() == ("02-hermes-platform",)
        assert registry.allows_profile("uid:1001", "02-hermes-platform") is True

    def test_owner_bootstrap_can_migrate_an_old_subject_set(self, store):
        """A migration re-run with a wider stamp set extends the same person, not a new one."""
        store.bootstrap_owner(slug="person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        registry = store.bootstrap_owner(
            slug="person/yury", profiles=["01-orchestrator"],
            subjects=["uid:1001", "cron-owner", "125805788"])
        assert registry.record("person/yury").subjects == ("uid:1001", "cron-owner", "125805788")
        assert len(registry.persons) == 1

    def test_owner_bootstrap_writes_a_backup_of_the_previous_model(self, store, path):
        store.bootstrap_owner(slug="person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        store.bootstrap_owner(slug="person/yury", profiles=["01-orchestrator"],
                              subjects=["uid:1001", "cron-owner"])
        assert hb.backup_path_latest(path).exists()


# ---------------------------------------------------------------------------
# 8. Process accessors: caching, activation, priming
# ---------------------------------------------------------------------------


class TestAccessors:
    def test_store_is_shared_per_path(self, path):
        assert hb.store(path) is hb.store(path)
        assert hb.store(path) is not hb.store(path.parent / "other.json")

    def test_cache_follows_the_file(self, store, path):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        assert store.read().person_of("uid:1001") == "person/yury"
        _write(path, _valid_document(persons=[]))
        # A different size/mtime invalidates; force the same second by changing content length.
        assert store.read().persons == {}

    def test_reset_cache_forgets_every_store(self, store, path):
        store.bind("person/yury", profiles=["01-orchestrator"], subjects=["uid:1001"])
        assert hb.store(path).read().persons
        hb.reset_cache()
        assert hb.store(path).read().persons  # re-read from disk, still correct

    def test_registry_is_inactive_while_acl_is_off(self, monkeypatch, path):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        monkeypatch.setattr(hb, "registry_active", lambda: False)
        _write(path, "{not json")
        hb.reset_cache()
        assert hb.load_cached() is None  # logged, ignored: ACL off means nothing changes

    def test_registry_raises_while_acl_is_on(self, monkeypatch, path):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        monkeypatch.setattr(hb, "registry_active", lambda: True)
        _write(path, "{not json")
        hb.reset_cache()
        with pytest.raises(hb.BindingsCorrupt):
            hb.load_cached()

    def test_person_stamps_is_empty_without_a_registry(self, monkeypatch, path):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        assert hb.person_stamps("uid:1001") == ()

    def test_person_stamps_expands_a_known_subject(self, monkeypatch, path, bound):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        assert hb.person_stamps("uid:1001") == ("person/yury", "uid:1001", "cron-owner", "125805788")

    def test_prime_never_raises_on_a_broken_file(self, monkeypatch, path, caplog):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        _write(path, "{not json")
        hb.reset_cache()
        with caplog.at_level("ERROR"):
            assert hb.prime() is None
        assert any("will DENY" in m for m in caplog.messages)

    def test_prime_reports_the_absent_file_without_an_error(self, monkeypatch, path, caplog):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        with caplog.at_level("INFO"):
            assert hb.prime() is None
        assert any("not present yet" in m for m in caplog.messages)

    def test_prime_loads_and_logs_the_record_count(self, monkeypatch, path, bound, caplog):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        with caplog.at_level("INFO"):
            registry = hb.prime()
        assert registry is not None and registry.record("person/yury") is not None
        assert any("record(s)" in m and "profile-bindings.json" in m for m in caplog.messages)

    def test_describe_names_the_records(self, bound):
        text = hb.describe(bound)
        assert "person/yury" in text and "01-orchestrator" in text
        assert hb.describe(None) == "bindings: absent"


# ---------------------------------------------------------------------------
# 9. same_owner integration: the registry is additive, never widening
# ---------------------------------------------------------------------------


class TestSameOwnerIntegration:
    """``gateway.owner_identity.same_owner`` must consult the registry ONLY as an explicit
    statement about identity, and must keep every previous answer when no registry exists."""

    @pytest.fixture
    def with_registry(self, monkeypatch, path, bound):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        return bound

    def test_registry_ties_the_owners_legacy_stamp_to_their_person(self, with_registry):
        assert owner_identity.same_owner("uid:1001", "person/yury") is True
        assert owner_identity.same_owner("cron-owner", "person/yury") is True
        assert owner_identity.same_owner("125805788", "person/yury") is True

    def test_registry_never_ties_two_people(self, with_registry):
        assert owner_identity.same_owner("uid:1001", "person/alice") is False

    def test_equality_still_wins_without_any_registry(self, monkeypatch, path):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        hb.reset_cache()
        assert owner_identity.same_owner("uid:1001", "uid:1001") is True
        assert owner_identity.same_owner("uid:1001", "uid:2002") is False

    def test_a_broken_registry_denies_instead_of_raising(self, monkeypatch, path):
        monkeypatch.setattr(hb, "bindings_path", lambda: path)
        _write(path, "{not json")
        hb.reset_cache()
        assert owner_identity.same_owner("uid:1001", "person/yury") is False

    def test_owners_agree_consults_the_registry_both_ways(self, with_registry):
        assert owner_identity.owners_agree("uid:1001", "person/yury") is True
        assert owner_identity.owners_agree("person/yury", "uid:1001") is True
        assert owner_identity.owners_agree("uid:1001", "person/alice") is False
