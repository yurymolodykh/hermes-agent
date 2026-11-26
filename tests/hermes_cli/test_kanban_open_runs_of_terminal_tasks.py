"""Tests: an open ``task_runs`` row must never outlive its task's run phase.

Capacity is counted from open run rows, not from worker liveness. Runs 117/118/119
(19.11) stayed ``ended_at IS NULL`` on cards that had been archived for a week:
every sweep in the reclaim phase filters ``tasks.status = 'running'``, so nothing
looked at them, the dispatcher counted three dead workers against
``kanban.max_in_progress``, and ``hermes kanban reclaim`` refused them with
"not running or unknown id".
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _open_run(conn, task_id: str, *, pid: int | None, lock: str | None = None) -> int:
    """Insert a ``running`` run row that no ``tasks.current_run_id`` points at."""
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, started_at, "
        "worker_pid, worker_started_at) VALUES (?, 'w', 'running', ?, ?, ?, NULL)",
        (task_id, lock, now, pid),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_archive_closes_every_open_run_not_just_current(conn):
    """The run that ``current_run_id`` lost must be closed by the archive too."""
    tid = kb.create_task(conn, title="archived with orphan run", assignee="w")
    kb.claim_task(conn, tid, claimer=f"{kb._claimer_id()}")
    current = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert current is not None

    orphan = _open_run(conn, tid, pid=_dead_pid())
    assert orphan != current

    assert kb.archive_task(conn, tid) is True

    rows = conn.execute(
        "SELECT id, ended_at, error FROM task_runs WHERE task_id=? ORDER BY id", (tid,)
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["ended_at"] is not None, f"run {row['id']} still open after archive"
    assert rows[1]["error"] == "stale run of an archived task"

    ev = conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? AND kind='archived_open_runs_closed'", (tid,)
    ).fetchone()
    assert ev is not None


def test_sweep_closes_open_run_of_non_running_task(conn):
    """A card that left ``running`` keeps no open run row."""
    tid = kb.create_task(conn, title="card left running", assignee="w")
    run_id = _open_run(conn, tid, pid=_dead_pid(), lock=f"{kb._host_prefix()}dead:1")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    conn.commit()

    closed = kbd.close_orphaned_runs_of_terminal_tasks(conn)
    assert run_id in closed
    row = conn.execute("SELECT ended_at, error FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert row["ended_at"] is not None
    assert "no longer running" in row["error"]


def test_sweep_never_touches_a_running_task(conn):
    """``running`` tasks belong to the dead-worker / stale / owner guards."""
    tid = kb.create_task(conn, title="live card", assignee="w")
    run_id = _open_run(conn, tid, pid=_dead_pid(), lock=f"{kb._host_prefix()}dead:2")
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    conn.commit()

    assert kbd.close_orphaned_runs_of_terminal_tasks(conn) == []
    assert conn.execute("SELECT ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] is None


def test_sweep_never_closes_a_run_under_a_live_worker(conn):
    """A live process may still be writing its result: never close beneath it."""
    alive = subprocess.Popen(["sleep", "30"])
    try:
        tid = kb.create_task(conn, title="worker still writing", assignee="w")
        run_id = _open_run(conn, tid, pid=alive.pid, lock=f"{kb._host_prefix()}live:3")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()

        assert kbd.close_orphaned_runs_of_terminal_tasks(conn) == []
        assert conn.execute(
            "SELECT ended_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()[0] is None
    finally:
        alive.terminate()
        alive.wait(timeout=10)


def test_sweep_leaves_other_hosts_runs_alone(conn):
    """A row claimed on another host is that host's dispatcher's to close."""
    tid = kb.create_task(conn, title="foreign host row", assignee="w")
    run_id = _open_run(conn, tid, pid=_dead_pid(), lock="otherhost:1234")
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
    conn.commit()

    assert kbd.close_orphaned_runs_of_terminal_tasks(conn) == []


def test_dispatch_result_reports_closed_runs(conn):
    """The tick result names what it freed, so the drain is visible in telemetry."""
    result = kbd.DispatchResult()
    assert result.closed_orphaned_runs == []
    tid = kb.create_task(conn, title="reported", assignee="w")
    run_id = _open_run(conn, tid, pid=_dead_pid(), lock=f"{kb._host_prefix()}dead:4")
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
    conn.commit()
    result.closed_orphaned_runs = kbd.close_orphaned_runs_of_terminal_tasks(conn)
    assert run_id in result.closed_orphaned_runs
