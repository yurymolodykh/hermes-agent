"""A scan-preserved scratch workspace must stay findable after the card is gone.

Completion keeps a scratch dir whose git work exists nowhere else and records
``workspace_cleanup_preserved_unsaved_work`` in ``task_events``. That event was
the ONLY pointer to the directory, and ``task_events`` is retention-scoped
(``gc_events``, 30 days by default) and deleted outright when an archived card
is hard-deleted — while the directory itself stays on disk forever. A month
later nothing names it, so the documented "clean it up by hand" step is
impossible and the guard becomes a slow disk leak (verdict t_896b6548, Д3).

These tests pin the three ways the pointer must survive: event retention, the
task's hard delete, and the operator's discovery path (``hermes kanban gc``
prints the absolute paths, not just a counter). The last one pins Д7: repeated
``gc`` runs must not keep appending the same event.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_ops

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
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    kb.init_db()
    return home


def _ws_with_unpushed_repo(conn) -> tuple[str, Path]:
    """A scratch task whose workspace holds a repo nothing else has a copy of."""
    task_id = kb.create_task(conn, title="scratch with unpushed repo")
    ws = kbw.resolve_workspace(kb.get_task(conn, task_id))
    kbw.set_workspace_path(conn, task_id, ws)
    repo = ws / "clone"
    repo.mkdir(parents=True)
    _git("init", "-b", "main", str(repo))
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "work.txt").write_text("only copy\n", encoding="utf-8")
    _git("add", "work.txt", cwd=repo)
    _git("commit", "-m", "never pushed", cwd=repo)
    return task_id, ws


def _events(conn, task_id: str) -> list:
    return [e for e in kb.list_events(conn, task_id) if e.kind == PRESERVED_EVENT]


def _age_events(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = 0 WHERE task_id = ?", (task_id,)
        )


def _gc(args=None) -> int:
    return kanban_ops._cmd_gc(
        args
        or argparse.Namespace(event_retention_days=30, log_retention_days=30)
    )


# ---------------------------------------------------------------------------
# Д3 — retention keeps the provenance
# ---------------------------------------------------------------------------


def test_event_retention_keeps_preserved_workspace_provenance(
    kanban_home, capsys
):
    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert ws.is_dir()
        assert _events(conn, task_id), "precondition: the guard recorded the save"
        assert kb.archive_task(conn, task_id)
        _age_events(conn, task_id)
        # A neighbouring retention-scoped event must still be collected, so the
        # assertion below cannot pass by retention simply doing nothing.
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?, NULL, 'completed', NULL, 0)",
                (task_id,),
            )

        removed = kb.gc_events(conn, older_than_seconds=86400)

        assert removed >= 1, "unrelated old events are still pruned"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'",
            (task_id,),
        ).fetchone()[0] == 0
        survived = _events(conn, task_id)
        assert len(survived) == 1, "retention ate the only pointer to the directory"
        assert survived[0].payload["path"] == str(ws)
        assert ws.is_dir()


# ---------------------------------------------------------------------------
# Д3 — a hard-deleted card leaves the directory findable
# ---------------------------------------------------------------------------


def test_hard_delete_keeps_preserved_workspace_findable(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)

        assert kb.delete_archived_task(conn, task_id) is True

        assert kb.get_task(conn, task_id) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 0
        assert ws.is_dir(), "precondition: the directory outlives the card"

    assert _gc() == 0
    out = capsys.readouterr().out
    assert str(ws) in out, (
        "after a hard delete the operator has no command that names the "
        f"preserved directory:\n{out}"
    )


# ---------------------------------------------------------------------------
# Owner-visible discovery: gc prints paths, not just a counter
# ---------------------------------------------------------------------------


def test_gc_prints_preserved_workspace_paths(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)

    assert _gc() == 0
    out = capsys.readouterr().out
    assert "GC complete" in out
    assert str(ws) in out, f"gc must name the preserved directory:\n{out}"


def test_gc_forgets_a_preserved_dir_the_operator_removed(kanban_home, capsys):
    """Once the directory is gone the entry (and its event) go with it."""
    import shutil

    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)
    shutil.rmtree(ws)

    assert _gc() == 0
    out = capsys.readouterr().out
    assert str(ws) not in out, "a gone directory must not be advertised"
    with kbc.connect_closing() as conn:
        assert _events(conn, task_id) == [], (
            "the retained event must be reaped once its directory is gone"
        )
    # Second run has nothing left to report or rewrite.
    assert _gc() == 0
    assert str(ws) not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Д7 — repeated gc runs must not append another event
# ---------------------------------------------------------------------------


def test_gc_event_is_idempotent_on_repeated_runs(kanban_home):
    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)

    for _ in range(3):
        assert _gc() == 0

    with kbc.connect_closing() as conn:
        events = _events(conn, task_id)
        assert len(events) == 1, (
            f"each gc run re-recorded the same preserved directory: "
            f"{[e.payload['path'] for e in events]}"
        )
        assert events[0].payload["path"] == str(ws)


def test_registry_survives_a_board_scoped_round_trip(kanban_home):
    """The registry lives next to the board DB, so a named board keeps its own."""
    kb.create_board("prov")
    with kbc.connect_closing(board="prov") as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)
        assert kb.delete_archived_task(conn, task_id) is True

    path = Path(str(kb.kanban_db_path(board="prov"))).with_name(
        "kanban-preserved-workspaces.json"
    )
    assert path.exists(), "the durable registry never got written"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in payload] == [str(ws)]
    assert ws.is_dir()
    assert time.time() > 0  # keep the module's clock import honest
