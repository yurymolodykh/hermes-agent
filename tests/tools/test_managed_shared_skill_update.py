"""Isolated regression coverage for CAS-managed shared SKILL.md maintenance."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skill_manager_tool import skill_manage


def sha(data):
    return hashlib.sha256(data).hexdigest()


def skill(name="example", body="before"):
    return f"---\nname: {name}\ndescription: Test.\n---\n\n# {body}\n"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    local = tmp_path / "local"
    shared = tmp_path / "shared"
    local.mkdir(); shared.mkdir()
    target = shared / "creative" / "example" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(skill(), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "10-skill-librarian"))
    with patch("tools.skill_manager_tool.SKILLS_DIR", local), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[local, shared]), \
         patch("hermes_cli.config.load_config", return_value={"skills": {"create_dir": str(shared)}}):
        yield local, shared, target


def call(expected, **kw):
    return json.loads(skill_manage(
        action="update_shared", name="example", shared_path="creative/example/SKILL.md",
        expected_sha256=expected, **kw))


def test_update_cas_backup_journal_and_rollback(roots):
    local, shared, target = roots
    old = target.read_bytes()
    new = skill(body="after")
    update = call(sha(old), content=new)
    assert update["success"], update
    assert target.read_text(encoding="utf-8") == new
    assert Path(update["backup"]).read_bytes() == old
    record = json.loads(Path(update["journal"]).read_text(encoding="utf-8"))
    assert record["status"] == "committed"
    assert record["before_sha256"] == sha(old)
    assert record["after_sha256"] == sha(new.encode())
    assert update["post_readback"] == sha(new.encode())
    assert not (local / "example").exists()
    # End-client loader must consume the just-published content, not only the
    # low-level file readback.
    from tools.skills_tool import skill_view
    with patch("tools.skills_tool.SKILLS_DIR", local), \
         patch("agent.skill_utils.get_external_skills_dirs", return_value=[shared]):
        loaded = json.loads(skill_view("example", preprocess=False))
    assert loaded["success"], loaded
    assert "# after" in loaded["content"]
    stale = call(sha(old), content=skill(body="stale"))
    assert stale["success"] is False and "CAS mismatch" in stale["error"]
    bad_rollback = call(sha(old), rollback_id=record["id"])
    assert bad_rollback["success"] is False
    restored = call(sha(new.encode()), rollback_id=record["id"])
    assert restored["success"], restored
    assert target.read_bytes() == old
    assert json.loads(Path(restored["journal"]).read_text())["source_rollback_id"] == record["id"]


@pytest.mark.parametrize("override", [
    {"shared_path": "../example/SKILL.md"},
    {"shared_path": "creative/other/SKILL.md"},
    {"shared_path": "creative\\example\\SKILL.md"},
    {"content": skill("wrong")},
    {"content": "---\nname: example\nname: wrong\n---\n"},
    {"expected_sha256": "0" * 64},
])
def test_fail_closed_scope_candidate_and_cas(roots, override):
    _, _, target = roots
    old = target.read_bytes()
    params = {"content": skill(body="after"), "shared_path": "creative/example/SKILL.md",
              "expected_sha256": sha(old)}
    params.update(override)
    result = json.loads(skill_manage(action="update_shared", name="example", **params))
    assert result["success"] is False, result
    assert target.read_bytes() == old


def test_refuses_ambiguous_shared_name(roots):
    _, shared, target = roots
    other = shared / "other" / "example" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text(skill(), encoding="utf-8")
    result = call(sha(target.read_bytes()), content=skill(body="after"))
    assert not result["success"]
    assert "ambiguous" in result["error"]
    assert target.read_text() == skill()


def test_refuses_symlinked_target(roots):
    _, shared, target = roots
    outside = shared.parent / "victim"
    outside.write_text(skill(), encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported")
    result = call(sha(outside.read_bytes()), content=skill(body="after"))
    assert not result["success"]
    assert outside.read_text() == skill()


def test_refuses_unconfigured_profile_and_background_review(roots):
    local, shared, target = roots
    with patch("hermes_cli.config.load_config", return_value={"skills": {"create_dir": str(local)}}):
        result = call(sha(target.read_bytes()), content=skill(body="after"))
        assert not result["success"]
        assert "shared/external" in result["error"]
    with patch("tools.skill_provenance.is_background_review", return_value=True):
        result = call(sha(target.read_bytes()), content=skill(body="after"))
        assert not result["success"]
    assert target.read_text() == skill()


def test_foreign_profile_cannot_gain_access_by_config_only(roots, monkeypatch):
    _, _, target = roots
    monkeypatch.setenv("HERMES_HOME", str(target.parents[3] / "01-orchestrator"))
    result = call(sha(target.read_bytes()), content=skill(body="after"))
    assert not result["success"]
    assert "skill-librarian" in result["error"]
    assert target.read_text() == skill()


def test_backup_tamper_refuses_rollback(roots):
    _, _, target = roots
    old = target.read_bytes()
    new = skill(body="after")
    update = call(sha(old), content=new)
    assert update["success"]
    Path(update["backup"]).write_text("tampered")
    result = call(sha(new.encode()), rollback_id=update["transaction"]["id"])
    assert not result["success"] and "checksum" in result["error"]
    assert target.read_text() == new


def test_lock_contention_fails_without_write(roots):
    _, shared, target = roots
    lock = shared / ".managed-shared-example.lock"
    lock.write_text("foreign-owner", encoding="ascii")
    before = target.read_bytes()
    result = call(sha(before), content=skill(body="after"))
    assert not result["success"]
    assert lock.read_text() == "foreign-owner"
    assert target.read_bytes() == before


def test_atomic_replace_failure_keeps_original_and_prepared_backup(roots):
    _, shared, target = roots
    before = target.read_bytes()
    with patch("tools.managed_shared_skill_update._atomic_bytes", side_effect=OSError("injected")):
        result = call(sha(before), content=skill(body="after"))
    assert not result["success"]
    assert target.read_bytes() == before
    records = list((shared / ".managed-shared-history").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["status"] == "prepared"
    assert records[0].with_suffix(".bak").read_bytes() == before


def test_commit_journal_failure_reports_changed_target_and_recovery_locators(roots):
    _, shared, target = roots
    from tools.managed_shared_skill_update import _journal as real_journal
    calls = 0

    def fail_second_journal(path, record):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected commit-journal failure")
        return real_journal(path, record)

    old = target.read_bytes()
    new = skill(body="after")
    with patch("tools.managed_shared_skill_update._journal", side_effect=fail_second_journal):
        result = call(sha(old), content=new)
    assert not result["success"]
    assert result["current_sha256"] == sha(new.encode())
    assert target.read_text() == new
    assert Path(result["backup"]).read_bytes() == old
    assert json.loads(Path(result["journal"]).read_text())["status"] == "prepared"
    recovered = call(sha(new.encode()), rollback_id=Path(result["journal"]).stem)
    assert recovered["success"], recovered
    assert target.read_bytes() == old


@pytest.mark.parametrize("action", ["delete", "remove_file"])
@pytest.mark.parametrize("review", [False, True])
def test_external_destructive_actions_cannot_bypass_readonly(roots, monkeypatch, action, review):
    _, _, target = roots
    supporting = target.parent / "references" / "api.md"
    supporting.parent.mkdir()
    supporting.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(target.parents[3] / "09-independent-reviewer"))
    original = target.read_bytes()
    if review:
        from tools.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin
        from tools.skill_manager_tool import mark_background_review_skill_read
        token = set_current_write_origin(BACKGROUND_REVIEW)
        mark_background_review_skill_read(target)
        mark_background_review_skill_read(supporting)
    try:
        args = {"file_path": "references/api.md"} if action == "remove_file" else {"absorbed_into": ""}
        result = json.loads(skill_manage(action=action, name="example", **args))
    finally:
        if review:
            reset_current_write_origin(token)
    assert result["success"] is False, result
    assert target.read_bytes() == original
    assert supporting.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("action", ["delete", "remove_file"])
def test_explicit_profile_local_destructive_action_still_works(roots, action):
    local, _, _ = roots
    personal = local / "personal"
    personal.mkdir()
    (personal / "SKILL.md").write_text(skill(name="personal"), encoding="utf-8")
    supporting = personal / "references" / "api.md"
    supporting.parent.mkdir()
    supporting.write_text("keep", encoding="utf-8")
    args = {"file_path": "references/api.md"} if action == "remove_file" else {"absorbed_into": ""}
    result = json.loads(skill_manage(action=action, name="personal", **args))
    assert result["success"], result
    assert (not supporting.exists()) if action == "remove_file" else (not personal.exists())


def test_enabled_security_guard_blocks_candidate_before_backup_or_commit(roots):
    _, shared, target = roots
    before = target.read_bytes()
    proposed = skill(body="unsafe")
    seen = []

    def block_candidate(path, **kwargs):
        seen.append((Path(path, "SKILL.md").read_text(encoding="utf-8"), target.read_bytes()))
        return "Security scan blocked candidate"

    with patch("hermes_cli.config.load_config", return_value={"skills": {
        "create_dir": str(shared), "guard_agent_created": True,
    }}), patch("tools.skill_manager_tool._security_scan_skill", side_effect=block_candidate) as scanner:
        result = call(sha(before), content=proposed)
    assert not result["success"], result
    assert "Security scan" in result["error"]
    assert scanner.call_count == 1
    assert seen == [(proposed, before)]
    assert target.read_bytes() == before
    history = shared / ".managed-shared-history"
    assert not list(history.glob("*.bak"))
    assert not list(history.glob("*.json"))


def test_enabled_security_guard_scanner_failure_fails_closed(roots):
    _, shared, target = roots
    before = target.read_bytes()
    with patch("hermes_cli.config.load_config", return_value={"skills": {
        "create_dir": str(shared), "guard_agent_created": True,
    }}), patch("tools.skill_manager_tool.scan_skill", side_effect=RuntimeError("injected")):
        result = call(sha(before), content=skill(body="after"))
    assert not result["success"] and "Security scan failed" in result["error"]
    assert target.read_bytes() == before
    assert not list((shared / ".managed-shared-history").glob("*.bak"))


def test_enabled_security_guard_allows_clean_candidate_and_rollback(roots):
    _, shared, target = roots
    before = target.read_bytes()
    candidate = skill(body="safe")
    seen = []

    def allow_candidate(path, **kwargs):
        seen.append((Path(path) / "SKILL.md").read_bytes())
        return None

    with patch("hermes_cli.config.load_config", return_value={"skills": {
        "create_dir": str(shared), "guard_agent_created": True,
    }}), patch("tools.skill_manager_tool._security_scan_skill", side_effect=allow_candidate):
        updated = call(sha(before), content=candidate)
        assert updated["success"], updated
        restored = call(sha(candidate.encode()), rollback_id=updated["transaction"]["id"])
        assert restored["success"], restored
    assert seen == [candidate.encode(), before]
    assert target.read_bytes() == before


def test_enabled_real_scanner_allows_clean_candidate(roots):
    _, shared, target = roots
    before = target.read_bytes()
    with patch("hermes_cli.config.load_config", return_value={"skills": {
        "create_dir": str(shared), "guard_agent_created": True,
    }}):
        result = call(sha(before), content=skill(body="A harmless step-by-step procedure."))
    assert result["success"], result
    assert target.read_text(encoding="utf-8") == skill(body="A harmless step-by-step procedure.")
