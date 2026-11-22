"""Scratch-workspace teardown must not destroy git work that exists nowhere else.

A scratch workspace is a worker's private space, and a worker may well clone a
repo into it and commit there. ``_cleanup_workspace`` used to ``shutil.rmtree``
the directory unconditionally, so those commits vanished with nothing but a
``Preserving worktree ...``-style signal for the ``worktree`` kind. The guard
here preserves the directory, logs it and appends a
``workspace_cleanup_preserved_unsaved_work`` event for a repository holding
local commits, uncommitted changes, a stash or history that was never pushed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw

PRESERVED_EVENT = "workspace_cleanup_preserved_unsaved_work"


def _git(*args: str, cwd: Path | str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The first-use tip writes a sentinel under the home; keep it isolated too.
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    kb.init_db()
    return home


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """Bare remote + a clone whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("config", "user.email", "t@example.com", cwd=project)
    _git("config", "user.name", "t", cwd=project)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=project)
    _git("commit", "-m", "init", cwd=project)
    _git("push", "origin", "HEAD", cwd=project)
    return project


def _scratch_task(conn, title: str = "scratch with repo") -> tuple[str, Path]:
    task_id = kb.create_task(conn, title=title)
    ws = kbw.resolve_workspace(kb.get_task(conn, task_id))
    kbw.set_workspace_path(conn, task_id, ws)
    return task_id, ws


def _events(conn, task_id: str, kind: str) -> list:
    return [e for e in kb.list_events(conn, task_id) if e.kind == kind]


def _clone_into(origin_repo: Path, dest: Path) -> Path:
    _git("clone", str(origin_repo), str(dest))
    _git("config", "user.email", "t@example.com", cwd=dest)
    _git("config", "user.name", "t", cwd=dest)
    return dest


# ---------------------------------------------------------------------------
# Preserved: work that lives only in the scratch dir
# ---------------------------------------------------------------------------


def test_local_commit_preserves_scratch_workspace(kanban_home, origin_repo):
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "clone")
        (repo / "work.txt").write_text("local only\n", encoding="utf-8")
        _git("add", "work.txt", cwd=repo)
        _git("commit", "-m", "work that was never pushed", cwd=repo)

        assert kb.complete_task(conn, task_id, result="done")

        assert ws.is_dir(), "scratch workspace holding an unpushed commit must survive"
        assert (repo / "work.txt").exists()
        preserved = _events(conn, task_id, PRESERVED_EVENT)
        assert len(preserved) == 1
        payload = preserved[0].payload
        assert payload["path"] == str(ws)
        assert payload["repo"] == str(repo)
        assert "remote" in payload["reason"]
        # The local commit is still there, reachable from HEAD.
        assert "work that was never pushed" in _git("log", "--oneline", "HEAD", cwd=repo)


def test_uncommitted_changes_preserve_scratch_workspace(kanban_home, origin_repo):
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "clone")
        (repo / "draft.py").write_text("print(1)\n", encoding="utf-8")

        assert kb.complete_task(conn, task_id, result="done")

        assert (repo / "draft.py").exists()
        payload = _events(conn, task_id, PRESERVED_EVENT)[0].payload
        assert payload["reason"] == "uncommitted changes"


def test_repo_without_any_remote_is_preserved(kanban_home):
    """Nothing was ever pushed: the only copy of this history is in scratch."""
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = ws / "local-repo"
        repo.mkdir()
        _git("init", "-b", "main", str(repo))
        _git("config", "user.email", "t@example.com", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-m", "local", cwd=repo)

        assert kb.complete_task(conn, task_id, result="done")

        assert repo.is_dir()
        payload = _events(conn, task_id, PRESERVED_EVENT)[0].payload
        assert "no remote-tracking refs" in payload["reason"]


def test_stash_is_preserved(kanban_home, origin_repo):
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "clone")
        (repo / "README.md").write_text("stashed edit\n", encoding="utf-8")
        _git("stash", "push", "-m", "wip", cwd=repo)

        assert kb.complete_task(conn, task_id, result="done")

        assert repo.is_dir()
        assert _events(conn, task_id, PRESERVED_EVENT)[0].payload["reason"] == "stash entries"


def test_nested_repo_is_found(kanban_home, origin_repo):
    """The clone need not be at the workspace root."""
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "vendor" / "deep" / "clone")
        (repo / "x.txt").write_text("x\n", encoding="utf-8")
        _git("add", "x.txt", cwd=repo)
        _git("commit", "-m", "nested work", cwd=repo)

        assert kb.complete_task(conn, task_id, result="done")

        assert (repo / "x.txt").exists()
        assert _events(conn, task_id, PRESERVED_EVENT)[0].payload["repo"] == str(repo)


# ---------------------------------------------------------------------------
# Removed: nothing lost
# ---------------------------------------------------------------------------


def test_clean_pushed_repo_still_removed(kanban_home, origin_repo):
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "clone")
        assert (repo / "README.md").exists()

        assert kb.complete_task(conn, task_id, result="done")

        assert not ws.exists(), "a clean, fully pushed clone holds no unique work"
        assert _events(conn, task_id, PRESERVED_EVENT) == []


def test_repo_with_pushed_branch_removed(kanban_home, origin_repo):
    """A commit that reached the remote on its own branch is not unique work."""
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "clone")
        _git("checkout", "-b", "feature", cwd=repo)
        (repo / "f.txt").write_text("f\n", encoding="utf-8")
        _git("add", "f.txt", cwd=repo)
        _git("commit", "-m", "feature work", cwd=repo)
        _git("push", "origin", "feature", cwd=repo)

        assert kb.complete_task(conn, task_id, result="done")

        assert not ws.exists()
        assert _events(conn, task_id, PRESERVED_EVENT) == []


def test_workspace_without_a_repo_still_removed(kanban_home):
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        (ws / "notes.md").write_text("scratch\n", encoding="utf-8")

        assert kb.complete_task(conn, task_id, result="done")

        assert not ws.exists()
        assert _events(conn, task_id, PRESERVED_EVENT) == []


def test_vendored_tool_dirs_are_not_scanned(kanban_home):
    """node_modules/.venv hold thousands of files; scanning them buys nothing."""
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        (ws / "node_modules" / "pkg").mkdir(parents=True)
        (ws / ".venv" / "lib").mkdir(parents=True)

        assert kb.complete_task(conn, task_id, result="done")

        assert not ws.exists()
        assert _events(conn, task_id, PRESERVED_EVENT) == []


def test_scan_that_stops_at_the_depth_cap_preserves(kanban_home, monkeypatch):
    """Too deep to verify is not the same fact as clean — keep the dir."""
    from hermes_cli import kanban_db_workspace as kbw

    monkeypatch.setattr(kbw, "_SCRATCH_REPO_SCAN_MAX_DEPTH", 1)
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        deep = ws / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "data.txt").write_text("deep\n", encoding="utf-8")

        assert kb.complete_task(conn, task_id, result="done")

        assert ws.is_dir()
        payload = _events(conn, task_id, PRESERVED_EVENT)[0].payload
        assert payload["reason"] == "workspace tree too deep to verify"


# ---------------------------------------------------------------------------
# Every deletion path carries the guard
# ---------------------------------------------------------------------------


def test_archive_preserves_unsaved_scratch_workspace(kanban_home, origin_repo):
    """Archiving reaps the workspace too — the same guard has to hold there."""
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "clone")
        (repo / "work.txt").write_text("x\n", encoding="utf-8")
        _git("add", "work.txt", cwd=repo)
        _git("commit", "-m", "unpushed", cwd=repo)

        assert kb.archive_task(conn, task_id)

        assert repo.is_dir()
        assert _events(conn, task_id, PRESERVED_EVENT)


def test_deferred_parent_cleanup_preserves_unsaved_scratch_workspace(kanban_home, origin_repo):
    """The parent's cleanup runs when its last child finishes — same guard."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        parent_ws = kbw.resolve_workspace(kb.get_task(conn, parent))
        kbw.set_workspace_path(conn, parent, parent_ws)
        repo = _clone_into(origin_repo, parent_ws / "clone")
        (repo / "work.txt").write_text("x\n", encoding="utf-8")
        _git("add", "work.txt", cwd=repo)
        _git("commit", "-m", "unpushed", cwd=repo)

        child = kb.create_task(
            conn, title="dir child",
            workspace_kind="dir", workspace_path=str(kanban_home / "child-dir"),
        )
        kb.link_tasks(conn, parent, child)
        kbw.resolve_workspace(kb.get_task(conn, child))
        assert kb.complete_task(conn, parent, result="parent handoff")
        assert parent_ws.exists(), "deferred while the dir child is active"
        assert kb.complete_task(conn, child, result="child done")

        assert repo.is_dir(), "parent's unpushed commit must survive the deferred sweep"
        assert _events(conn, parent, PRESERVED_EVENT)


def test_preserved_workspace_is_reported_on_the_card(kanban_home, origin_repo):
    """The warning has to be readable from the board, not just the log."""
    with kbc.connect() as conn:
        task_id, ws = _scratch_task(conn)
        repo = _clone_into(origin_repo, ws / "clone")
        (repo / "work.txt").write_text("x\n", encoding="utf-8")
        _git("add", "work.txt", cwd=repo)
        _git("commit", "-m", "unpushed", cwd=repo)

        kb.complete_task(conn, task_id, result="done")
        events = kb.list_events(conn, task_id)

    preserved = [e for e in events if e.kind == PRESERVED_EVENT]
    assert len(preserved) == 1
    # Serialized payload survives the round trip through SQLite as JSON.
    assert str(ws) in str(preserved[0].payload)
    assert preserved[0].payload["path"] == str(ws)
