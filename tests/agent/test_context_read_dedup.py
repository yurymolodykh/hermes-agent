"""Read-cache invalidation uses real local readers and task-scoped state."""

import json

import pytest

from agent.context_read_dedup import invalidate_context_read_dedup
from tools.file_tools import read_file_tool, reset_file_dedup
from tools.skills_tool import _skill_view_with_bump, reset_skill_view_dedup


@pytest.mark.parametrize("scope", ["active", None, ""])
def test_read_caches_are_scoped_and_rehydrate_real_files(tmp_path, monkeypatch, scope):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    skill = tmp_path / "home" / "skills" / "read-cache-demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: read-cache-demo\ndescription: Cache test.\n---\n# Main content\n",
        encoding="utf-8",
    )
    (skill / "references" / "guide.md").write_text("# Linked procedure\n", encoding="utf-8")
    document = tmp_path / "ordinary-file.txt"
    document.write_text("Actual local file content\n", encoding="utf-8")

    def reads(task):
        return [
            json.loads(_skill_view_with_bump(args, task_id=task))
            for args in ({"name": "read-cache-demo"},
                         {"name": "read-cache-demo", "file_path": "references/guide.md"})
        ] + [json.loads(read_file_tool(str(document), task_id=task))]

    try:
        for task in ("active", "other"):
            full = reads(task)
            assert "Main content" in full[0].get("content", ""), full
            assert "Linked procedure" in full[1].get("content", ""), full
            assert "Actual local file content" in full[2].get("content", ""), full
        invalidate_context_read_dedup(scope)
        active = reads("active")
        if scope == "active":
            assert all(r.get("content") and not r.get("dedup") for r in active), active
        else:
            assert all(r.get("dedup") is True for r in active), active
        assert all(r.get("dedup") is True for r in reads("other"))
    finally:
        from tools.file_tools import _file_ops_cache
        from tools.terminal_tool import cleanup_vm
        for task in ("active", "other"):
            reset_skill_view_dedup(task)
            reset_file_dedup(task)
            _file_ops_cache.pop(task, None)
            cleanup_vm(task)
