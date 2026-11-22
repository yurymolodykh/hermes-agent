"""Invariant: the terminal-worker reap never signals a worker that is still running.

The closed ``task_runs`` row keeps the worker's pid + spawn fingerprint so a worker that
outlived its own transition stays reachable (issue #111791). But ``ended_at`` is stamped at the
card's transition (``request_review`` / ``kanban_complete``), not at the process's exit: an
implementer whose row closed at ``review_requested`` may still be finalising its turn minutes
later. Reaping on run-row age alone killed such a worker mid-write — the loss cascaded into the
owner's admission going ``unknown`` and every later bind on that session being refused for the
gateway's lifetime (issue #157057).

The contract these tests pin:

1. A live worker of a closed run is left alone (no signal, evidence kept) however old the run.
2. Once that worker is gone, the stale evidence is retired and no signal is needed.
3. A recycled PID (fingerprint mismatch) and a legacy row without a fingerprint are never
   signalled.
4. A just-closed run stays inside the grace window.

Red on the pre-fix code: ``reap_terminal_workers`` signalled any live worker whose run row had
passed the grace, so tests 1 and 4 saw SIGTERM (and a ``terminal_worker_reaped`` event).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _sleeper():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.2)
    return proc


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def _completed_card_with_worker(conn, proc, *, ended_ago: int = 600) -> tuple[str, int]:
    tid = kb.create_task(conn, title="finished", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    kbd._set_worker_pid(conn, tid, proc.pid)
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id) is True
    # Default: the run closed long enough ago that the reaper's grace window has passed.
    conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?", (ended_ago, run_id))
    return tid, run_id


def _event_kinds(conn, tid):
    return [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]


def test_live_worker_of_a_closed_run_is_never_signalled(conn):
    """Run-row age is not evidence of exit: a live worker keeps its pid and its life."""
    proc = _sleeper()
    try:
        tid, run_id = _completed_card_with_worker(conn, proc)
        run = conn.execute(
            "SELECT worker_pid, worker_started_at, ended_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        # The evidence the running-only sweeps lost lives on the closed run row.
        assert run["ended_at"] is not None and run["worker_pid"] == proc.pid
        assert run["worker_started_at"] is not None

        # Dispatch tick path (the real caller), not just the helper.
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)

        assert result.reaped_terminal_workers == []
        assert proc.poll() is None, "a still-finalising worker must not be killed"
        # Evidence is kept, not dropped: the row stays in the candidate set for the next tick.
        run = conn.execute(
            "SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run["worker_pid"] == proc.pid and run["worker_started_at"] is not None
        assert "terminal_worker_reaped" not in _event_kinds(conn, tid)
    finally:
        _stop(proc)


def test_evidence_of_an_exited_worker_is_retired_without_a_signal(conn):
    """Once the worker has exited, the stale pid + fingerprint are cleared dead (no kill needed)."""
    proc = _sleeper()
    try:
        _tid, run_id = _completed_card_with_worker(conn, proc)
        _stop(proc)

        assert kbd.reap_terminal_workers(conn) == []

        run = conn.execute(
            "SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run["worker_pid"] is None and run["worker_started_at"] is None
    finally:
        _stop(proc)


def test_recycled_pid_and_legacy_row_are_never_signalled(conn):
    stranger = _sleeper()
    legacy = _sleeper()
    try:
        _tid, run_id = _completed_card_with_worker(conn, stranger)
        # PID reuse: the recorded fingerprint belongs to a process that no longer exists.
        conn.execute("UPDATE task_runs SET worker_started_at = worker_started_at - 1000000 WHERE id=?", (run_id,))
        _, legacy_run = _completed_card_with_worker(conn, legacy)
        conn.execute("UPDATE task_runs SET worker_started_at = NULL WHERE id=?", (legacy_run,))

        assert kbd.reap_terminal_workers(conn) == []

        assert stranger.poll() is None and legacy.poll() is None
        # Stale evidence is dropped so the row is not rescanned; the legacy row is left alone.
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()["worker_pid"] is None
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (legacy_run,)).fetchone()["worker_pid"] == legacy.pid
    finally:
        for p in (stranger, legacy):
            _stop(p)


def test_fresh_terminal_run_is_left_alone_until_grace_passes(conn):
    """A run closed inside the grace window is not inspected; past it, a live worker is still kept."""
    proc = _sleeper()
    try:
        _tid, run_id = _completed_card_with_worker(conn, proc, ended_ago=0)

        assert kbd.reap_terminal_workers(conn) == []
        assert proc.poll() is None
        assert conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()["worker_pid"] == proc.pid  # evidence kept for a later tick

        conn.execute(
            "UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?",
            (kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS, run_id),
        )
        assert kbd.reap_terminal_workers(conn) == []
        assert proc.poll() is None, "passing the grace is not a reason to kill"
        assert conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()["worker_pid"] == proc.pid
    finally:
        _stop(proc)


def test_one_failing_row_does_not_abort_the_sweep(conn):
    """A liveness-probe failure on one run is logged and skipped; the other rows are still swept."""
    broken, healthy = _sleeper(), _sleeper()
    real_worker_alive = kbd._worker_alive
    try:
        _, broken_run = _completed_card_with_worker(conn, broken)
        _, healthy_run = _completed_card_with_worker(conn, healthy)
        _stop(broken)
        _stop(healthy)

        def flaky_worker_alive(pid, started_at):
            if pid == broken.pid:
                raise RuntimeError("boom")
            return real_worker_alive(pid, started_at)

        kbd._worker_alive = flaky_worker_alive
        try:
            assert kbd.reap_terminal_workers(conn) == []
        finally:
            kbd._worker_alive = real_worker_alive

        assert conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id=?", (healthy_run,)
        ).fetchone()["worker_pid"] is None
        # The failing row is untouched, so a later tick can still settle it.
        assert conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id=?", (broken_run,)
        ).fetchone()["worker_pid"] == broken.pid
    finally:
        for p in (broken, healthy):
            _stop(p)
