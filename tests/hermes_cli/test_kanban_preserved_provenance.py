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
import os
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


# ---------------------------------------------------------------------------
# Д3.1 — an unreadable path is "cannot tell", never "gone"
# ---------------------------------------------------------------------------

_not_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can read a 0o000 directory"
)


@_not_root
def test_unreadable_parent_keeps_registry_entry_and_event(kanban_home, capsys):
    """A stat we cannot complete must not delete the only pointer.

    ``Path.is_dir()`` is False for an unmounted volume or an unreadable
    ancestor just as it is for a deleted directory. Reaping on that reading
    drops the registry line AND the retention-exempt event while the directory
    is still on disk — the guard would strand exactly what it preserved.
    """
    from hermes_cli import kanban_preserved_workspaces as kpw

    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)
        registry = kpw.registry_path(conn)
        assert [e["path"] for e in kpw.entries(conn)] == [str(ws)]

    parent = ws.parent
    parent.chmod(0o000)
    try:
        assert _gc() == 0
        # Reporting it is the safe side of "unknown": the operator gets the
        # path, and nothing is reaped on a stat we could not complete.
    finally:
        parent.chmod(0o755)

    with kbc.connect_closing() as conn:
        assert [e["path"] for e in kpw.entries(conn)] == [str(ws)], (
            "an unreadable parent was read as a deleted directory"
        )
        assert _events(conn, task_id), "the retained event was reaped with it"
    assert registry is not None and registry.exists()
    assert (ws / "clone" / "work.txt").exists()

    # And once it is readable again the entry is live, not reaped.
    assert _gc() == 0
    assert str(ws) in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Д3.3 — a directory gone together with every ancestor is reliably gone
# ---------------------------------------------------------------------------


def test_missing_parent_chain_counts_as_gone(kanban_home, capsys):
    """No directory AND no parent is a reliable negative, not "cannot tell".

    ``kanban.md`` promises the entry is dropped "once the path is gone"; with
    only the parent's ``ENOENT`` swallowed by the generic ``OSError`` branch it
    read as "unknown" and the entry survived forever (verdict t_12e58882,
    Д-нов-1). A vanished workspace root is the ordinary end state when the
    scratch tree is wiped, so the sweep must reap on it.
    """
    import shutil

    from hermes_cli import kanban_preserved_workspaces as kpw

    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)
        assert [e["path"] for e in kpw.entries(conn)] == [str(ws)]

    # The directory and its parent (the board's workspace root) both vanish.
    shutil.rmtree(ws)
    shutil.rmtree(ws.parent)
    assert not ws.parent.exists()

    assert kpw.directory_is_gone(str(ws)) is True, (
        "a path whose parent is missing too is reliably gone, not unknown"
    )

    assert _gc() == 0
    out = capsys.readouterr().out
    assert str(ws) not in out, f"a vanished root must not be advertised:\n{out}"
    with kbc.connect_closing() as conn:
        assert [e["path"] for e in kpw.entries(conn)] == [], (
            "the registry entry of a path gone with its parent was never dropped"
        )
        assert _events(conn, task_id) == [], (
            "the retained event of a path gone with its parent was never dropped"
        )


# ---------------------------------------------------------------------------
# Д3.4 — a sweep that cannot take the registry lock drops nothing
# ---------------------------------------------------------------------------


def test_gc_under_a_held_registry_lock_forgets_no_event(kanban_home, capsys, monkeypatch):
    """Dropping events without the lock leaves a printed path with no pointer.

    ``prune_gone`` used to delete the retained events even when the lock was
    never taken (so the registry could not be rewritten). The window prints a
    path whose event is already gone, and ``adopt`` cannot restore it — the
    event is the source. A process killed in that window leaves a registry
    entry with no event forever (verdict t_12e58882, Д-нов-2).
    """
    import shutil

    from hermes_cli import kanban_preserved_workspaces as kpw

    with kbc.connect_closing() as conn:
        task_id, ws = _ws_with_unpushed_repo(conn)
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.archive_task(conn, task_id)
        registry = kpw.registry_path(conn)
        assert registry is not None and registry.exists()
    shutil.rmtree(ws)

    lock_path = registry.with_name(registry.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    assert kpw._lock_handle(handle), "precondition: the lock is held by the test"
    monkeypatch.setattr(kpw, "_LOCK_TIMEOUT_SECONDS", 0.0)
    try:
        assert _gc() == 0
    finally:
        kpw._unlock_handle(handle)
        handle.close()

    with kbc.connect_closing() as conn:
        assert len(_events(conn, task_id)) == 1, (
            "events were dropped while the registry lock was never taken"
        )
        assert [e["path"] for e in kpw.entries(conn)] == [str(ws)], (
            "precondition: the entry is still there, so the sweep was partial"
        )

    # Once the lock is free the same sweep completes consistently.
    assert _gc() == 0
    with kbc.connect_closing() as conn:
        assert _events(conn, task_id) == []
        assert kpw.entries(conn) == []


# ---------------------------------------------------------------------------
# Д3.2 — a card preserved BEFORE the registry existed still survives a delete
# ---------------------------------------------------------------------------


def _legacy_preserved_event(conn, task_id: str, path: Path) -> None:
    """Exactly what the pre-registry code wrote: one event, no registry row."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
            " VALUES (?, NULL, ?, ?, 0)",
            (task_id, PRESERVED_EVENT,
             json.dumps({"path": str(path), "repo": str(path), "reason": "pre-upgrade"})),
        )


def test_hard_delete_adopts_a_pre_registry_preserved_event(kanban_home, capsys):
    """A legacy card's event is the only pointer — the delete must migrate it first."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="legacy preserved")
        task = kb.get_task(conn, task_id)
        assert task is not None
        ws = Path(kbw.resolve_workspace(task))
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "only-copy.txt").write_text("the only copy\n", encoding="utf-8")
        _legacy_preserved_event(conn, task_id, ws)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (task_id,))
        reg = kpw_registry(conn)
        assert reg is not None and not reg.exists(), "precondition: no registry row yet"

        assert kb.delete_archived_task(conn, task_id) is True

        assert kb.get_task(conn, task_id) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 0, "precondition: the event itself is gone"
        assert ws.is_dir()

    assert _gc() == 0
    out = capsys.readouterr().out
    assert str(ws) in out, (
        "a card preserved before the registry lost its last pointer on delete: "
        f"{out}"
    )


def test_gc_adopts_a_pre_registry_preserved_event(kanban_home, capsys):
    """An existing board migrates on the first sweep, without waiting for a delete."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="legacy done")
        task = kb.get_task(conn, task_id)
        assert task is not None
        ws = Path(kbw.resolve_workspace(task))
        ws.mkdir(parents=True, exist_ok=True)
        _legacy_preserved_event(conn, task_id, ws)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))

    assert _gc() == 0
    out = capsys.readouterr().out
    assert str(ws) in out, f"gc never adopted the legacy event:\n{out}"
    with kbc.connect_closing() as conn:
        assert [e["path"] for e in kpw_entries(conn)] == [str(ws)]


def kpw_registry(conn):
    from hermes_cli import kanban_preserved_workspaces as kpw

    return kpw.registry_path(conn)


def kpw_entries(conn):
    from hermes_cli import kanban_preserved_workspaces as kpw

    return kpw.entries(conn)
