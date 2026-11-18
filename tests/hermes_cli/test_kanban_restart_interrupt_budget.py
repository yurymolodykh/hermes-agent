"""A gateway restart must not spend a running Kanban card's failure budget (OPS-12).

Incident (18.11 16:01): four workers had run 32 minutes and all died at once with exit 130
and "owner admission settled interrupted" when the gateway restarted. The stop was the host's
own doing — the gateway interrupted its turns to restart — yet the crash sweep booked each one
as a card failure, the retry budget was already spent, and every card went ``blocked``.

The fix has two halves, pinned here:

1. the owner records WHICH stop it sent on the run's ``worker_result``
   (``interrupt_message``: "Gateway restarting" / "Gateway shutting down");
2. the crash sweep reads that cause and requeues the card WITHOUT counting a failure —
   exactly like a quota wall — so a restart never blocks a card. A user's Stop carries no
   such cause and keeps counting, and a plain crash still trips the breaker.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]


def _absent_pid(base: int = 3_700_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    path = tmp_path / "kanban.db"
    kbc.init_db(path)
    conn = kbc.connect(path)
    yield conn, path
    conn.close()


def _dead_run(conn, tid: str, *, interrupt_message=None, exit_code=130) -> int:
    """A ``running`` card whose bound owner-side interpreter exited (a managed run)."""
    worker_pid = _absent_pid()
    lock = f"{HOST}:{_absent_pid(base=3_800_000)}"
    started = NOW - 600
    run = int(conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'default', 'running', ?, ?, ?, ?)",
        (tid, lock, NOW + 3600, worker_pid, started),
    ).lastrowid)
    existing = conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
            "worker_pid, worker_started_at, current_run_id, started_at, priority, consecutive_failures) "
            "VALUES (?, ?, 'default', 'running', ?, ?, ?, ?, ?, ?, ?, 5, 0)",
            (tid, tid, NOW - 86400, lock, NOW + 3600, worker_pid, f"{lock}:1|1", run, started),
        )
    else:
        # A second attempt on the same card: re-arm it without touching consecutive_failures.
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, worker_pid = ?, "
            "worker_started_at = ?, current_run_id = ?, started_at = ? WHERE id = ?",
            (lock, NOW + 3600, worker_pid, f"{lock}:1|1", run, started, tid),
        )
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,'claimed',?,?)",
                 (tid, run, json.dumps({"source_status": "ready"}), started))
    # The owner-side interpreter is the bound pid; worker_result speaks for this run under the lock.
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,'worker_bound',?,?)",
                 (tid, run, json.dumps({"pid": worker_pid, "claim_lock": lock, "started_at": f"{lock}:1|1"}), started + 10))
    payload = {"pid": worker_pid, "claim_lock": lock, "exit_code": exit_code, "last_output": ""}
    if interrupt_message:
        payload["interrupt_message"] = interrupt_message
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,'worker_result',?,?)",
                 (tid, run, json.dumps(payload), NOW - 5))
    conn.commit()
    return run


def _state(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


# --- the classification itself ---------------------------------------------------------------

@pytest.mark.parametrize("cause", ["Gateway restarting", "Gateway shutting down"])
def test_gateway_stop_cause_is_classified_as_host_interruption(cause):
    dead = kbd._classify_dead_worker(1234, f"{HOST}:1", 130, interrupt_message=cause)
    assert dead.host_interrupted
    assert dead.run_outcome == "interrupted"
    assert "without counting a failure" in dead.error_text


def test_user_stop_and_plain_crash_keep_their_old_booking():
    """Only a HOST-caused stop is exempt: the user's Stop and a bare crash still count."""
    user_stop = kbd._classify_dead_worker(1234, f"{HOST}:1", 130, interrupt_message="")
    assert not user_stop.host_interrupted
    assert user_stop.run_outcome == "crashed"

    crash = kbd._classify_dead_worker(1234, f"{HOST}:1", 1)
    assert not crash.host_interrupted
    assert crash.run_outcome == "crashed"


# --- end to end through the crash sweep ------------------------------------------------------

def test_gateway_restart_requeues_without_blocking_the_card(board):
    """The incident shape: a restart interruption leaves the card retryable, not blocked."""
    conn, _ = board
    _dead_run(conn, "t_restarted", interrupt_message="Gateway restarting")

    kbd.detect_crashed_workers(conn)

    # ``detect_crashed_workers`` returns the reclaimed ids; auto-blocks ride a side channel.
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == [], \
        "a gateway restart must never auto-block a card"
    state = _state(conn, "t_restarted")
    assert state["status"] == "ready"
    assert int(state["consecutive_failures"]) == 0, "the host's restart must not spend the card's budget"
    assert state["claim_lock"] is None
    assert "interrupted by its gateway" in (state["last_failure_error"] or "")


def test_gateway_restart_never_trips_the_breaker_even_repeatedly(board):
    """Repeated restarts during one long card must not accumulate toward the failure limit."""
    conn, _ = board
    for i in range(4):
        _dead_run(conn, f"t_r{i}", interrupt_message="Gateway shutting down")
    kbd.detect_crashed_workers(conn)
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == []
    for i in range(4):
        state = _state(conn, f"t_r{i}")
        assert state["status"] == "ready"
        assert int(state["consecutive_failures"]) == 0


def test_ordinary_crash_still_counts_and_trips_at_the_limit(board, monkeypatch):
    """The exemption is narrow: a real crash keeps the documented retry budget."""
    conn, _ = board
    _dead_run(conn, "t_crash1", exit_code=1)
    kbd.detect_crashed_workers(conn)
    assert int(_state(conn, "t_crash1")["consecutive_failures"]) == 1

    _dead_run(conn, "t_crash1", exit_code=1)
    kbd.detect_crashed_workers(conn)
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == ["t_crash1"]
    assert _state(conn, "t_crash1")["status"] == "blocked"
