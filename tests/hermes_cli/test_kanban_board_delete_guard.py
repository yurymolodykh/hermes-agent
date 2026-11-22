"""``hermes kanban boards rm --delete`` must not destroy scratch work the guard saved.

A named board keeps its task scratch dirs inside the board directory
(``boards/<slug>/workspaces/<id>/``), so ``remove_board(..., archive=False)``
``shutil.rmtree``-ed the whole tree — including a workspace that task
completion had just *preserved* because it held an unpushed commit. The guard
is only honest if every deletion path honours it: a hard delete now walks the
board's workspaces first and refuses while any holds work that exists nowhere
else (or cannot be verified), unless ``force=True``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

PRESERVED_EVENT = "workspace_cleanup_preserved_unsaved_work"


def _git(*args: str, cwd: Path | str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _unpushed_repo(ws: Path) -> Path:
    """A local repo whose only history is inside *ws* (nothing was ever pushed)."""
    repo = ws / "clone"
    repo.mkdir(parents=True)
    _git("init", "-b", "main", str(repo))
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "work.txt").write_text("only copy\n", encoding="utf-8")
    _git("add", "work.txt", cwd=repo)
    _git("commit", "-m", "never pushed", cwd=repo)
    return repo


def _board_with_scratch_task(slug: str, *, unsaved: bool) -> tuple[str, Path]:
    kb.create_board(slug)
    with kbc.connect_closing(board=slug) as conn:
        task_id = kb.create_task(conn, title="scratch task")
    ws = kb.workspaces_root(board=slug) / task_id
    if unsaved:
        _unpushed_repo(ws)
    else:
        ws.mkdir(parents=True)
        (ws / "notes.md").write_text("scratch\n", encoding="utf-8")
    with kbc.connect_closing(board=slug) as conn:
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'scratch', workspace_path = ? WHERE id = ?",
            (str(ws), task_id),
        )
        conn.commit()
    return task_id, ws


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` through its real argparse tree and dispatcher, in-process.

    A subprocess would need a fully provisioned interpreter; the CLI surface under
    test is the parser flag plus ``_cmd_boards_rm`` wiring, so drive those directly.
    """
    import argparse
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_parser(parser.add_subparsers(dest="_top"))
    namespace = parser.parse_args(["kanban", *args])

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = kanban_command(namespace)
    return subprocess.CompletedProcess(args, rc, out.getvalue(), err.getvalue())


def test_hard_delete_refuses_and_keeps_unsaved_scratch_work(fresh_home):
    task_id, ws = _board_with_scratch_task("probeboard", unsaved=True)
    repo = ws / "clone"

    with pytest.raises(ValueError) as excinfo:
        kb.remove_board("probeboard", archive=False)

    assert str(ws) in str(excinfo.value)
    assert kb.board_dir("probeboard").is_dir(), "the refused delete must not touch the board"
    assert (repo / "work.txt").read_text(encoding="utf-8") == "only copy\n"
    assert "never pushed" in _git("log", "--oneline", "HEAD", cwd=repo)
    with kbc.connect_closing(board="probeboard") as conn:
        preserved = [e for e in kb.list_events(conn, task_id) if e.kind == PRESERVED_EVENT]
    assert len(preserved) == 1, "the refusal must be recorded on the task, not silent"
    assert preserved[0].payload["path"] == str(ws)


def test_hard_delete_with_force_reports_what_it_discards(fresh_home):
    task_id, ws = _board_with_scratch_task("forceboard", unsaved=True)

    res = kb.remove_board("forceboard", archive=False, force=True)

    assert res["action"] == "deleted"
    assert res["preserved"] == [str(ws)]
    assert not kb.board_dir("forceboard").exists()
    assert not ws.exists()


def test_hard_delete_of_a_clean_board_still_works(fresh_home):
    """No unsaved work anywhere: the ordinary delete must stay one call, no --force."""
    _task_id, ws = _board_with_scratch_task("cleanboard", unsaved=False)

    res = kb.remove_board("cleanboard", archive=False)

    assert res["action"] == "deleted"
    assert "preserved" not in res
    assert not kb.board_dir("cleanboard").exists()
    assert not ws.exists()


def test_force_on_a_clean_board_reports_nothing(fresh_home):
    _board_with_scratch_task("quietboard", unsaved=False)

    res = kb.remove_board("quietboard", archive=False, force=True)

    assert res["action"] == "deleted"
    assert "preserved" not in res


def test_archive_rename_path_is_unchanged(fresh_home):
    """The default (archive) path moves the tree, so preserved work travels with it."""
    task_id, ws = _board_with_scratch_task("archboard", unsaved=True)

    res = kb.remove_board("archboard", archive=True)

    assert res["action"] == "archived"
    moved = Path(res["new_path"]) / "workspaces" / task_id
    assert moved.is_dir()
    assert (moved / "clone" / "work.txt").read_text(encoding="utf-8") == "only copy\n"


def test_unreadable_workspace_root_also_vetoes_the_delete(fresh_home):
    """"Could not look" is not "nothing there": an unlistable root blocks the delete."""
    _task_id, ws = _board_with_scratch_task("lockedboard", unsaved=False)
    root = ws.parent
    root.chmod(0o000)
    try:
        with pytest.raises(ValueError) as excinfo:
            kb.remove_board("lockedboard", archive=False)
        assert str(root) in str(excinfo.value)
        assert kb.board_dir("lockedboard").is_dir()
    finally:
        root.chmod(0o755)


def test_cli_reports_the_refusal(fresh_home, tmp_path):
    """The operator sees the refusal and the recovery step, not a bare traceback."""
    _board_with_scratch_task("cliboard", unsaved=True)
    result = _run_cli(["boards", "rm", "cliboard", "--delete"])

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "unsaved work" in combined
    assert "--force" in combined
    assert kb.board_dir("cliboard").is_dir()


def test_cli_force_deletes_and_lists_paths(fresh_home, tmp_path):
    _task_id, ws = _board_with_scratch_task("cliforce", unsaved=True)
    result = _run_cli(["boards", "rm", "cliforce", "--delete", "--force"])

    assert result.returncode == 0, result.stderr
    assert str(ws) in result.stdout, "a forced delete must name what it discarded"
    assert not kb.board_dir("cliforce").exists()
