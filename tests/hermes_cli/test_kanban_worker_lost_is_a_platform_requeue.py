"""OPS-16: a platform-lost execution must not be booked as a card crash.

Incident (23.11.2026, run 708, card t_a4ae2056, profile 09-independent-reviewer): the worker
started at 01:18:07 AFTER the 01:16:55 gateway boot, was admitted at 01:18:13 and bound at
01:18:19 (pid 3579136) — so the card's "worker from the previous gateway generation" hypothesis
is refuted. At 02:00:01 the root watchdog (``/usr/local/bin/hermes-watchdog``) fired on its
30-minute cron tick and recorded ``STALE pid=3579136 state=Dsl age=41:49``: its rule 2 is meant
for processes wedged >6h in state D or Z but its awk gate (``$2 ~ /^[DZ]/``) tests the state
only, so any worker momentarily in uninterruptible disk I/O — exactly what a worker writing to
``state.db`` does — is SIGTERMed. The owner then recorded ``worker_lost: EOFError``, its admission
went ``unknown``, and the submitter found no ``worker_result``: it raised and exited 1. The crash
sweep booked ``crashed`` and spent the card's failure budget on a platform loss; 42 minutes of
work were dropped.

The contract this file pins:

1. no settlement and no ``worker_result`` is NOT the card's failure — the submitter exits the
   platform-loss code (not 1) and leaves a durable checkpoint on the card;
2. the crash sweep books that code as ``interrupted`` and requeues WITHOUT touching
   ``consecutive_failures`` — the same exemption ``owner_lost`` and ``host_interrupted`` already get;
3. a saved ``worker_result`` still outranks the receipt, and a genuine exit-1 crash still charges,
   so the exemption stays narrow.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client as wwc

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]


def _absent_pid(base: int = 3_900_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


# --- 1. the submitter must not exit 1 when the platform lost the attempt -----------------------

@pytest.fixture(name="no_saved_result")
def no_saved_result_fixture(monkeypatch):
    """No ``worker_result`` for this claim — the run 708 shape."""
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, "worker_result", lambda _db, _params: {})


@pytest.fixture(name="checkpoints")
def checkpoints_fixture(monkeypatch):
    """Capture the durable checkpoint instead of writing to a board."""
    recorded: list[dict] = []

    def record(params, board_db, cause):
        recorded.append({"task_id": params["task_id"], "run_id": params["run_id"],
                         "claim_lock": params["claim_lock"], "detail": cause})

    monkeypatch.setattr(wwc, "_checkpoint_worker_lost", record)
    return recorded


def _exit_code(receipt_status: str, params: dict | None = None):
    return wwc._exit_code(
        {"status": receipt_status},
        params or {"task_id": "t", "run_id": 1, "claim_lock": "cl"},
        "/nonexistent.db",
    )


def test_unknown_without_a_result_exits_the_platform_loss_code_not_one(no_saved_result, checkpoints):
    """RUN 708: no settlement, no worker_result. Exit 1 booked the card as crashed."""
    code = _exit_code("unknown")
    assert code == wwc.WORKER_LOST_EXIT_CODE, (
        "a lost execution is the platform's condition; exit 1 spends the card's failure budget")
    assert code != 1
    assert kb.KANBAN_WORKER_LOST_EXIT_CODE == wwc.WORKER_LOST_EXIT_CODE, (
        "the submitter and the crash sweep must agree on the same code")


def test_the_lost_execution_leaves_a_durable_checkpoint(no_saved_result, checkpoints):
    """The card must carry WHY: the owner forgot the execution, not that the work was wrong."""
    _exit_code("unknown")
    assert len(checkpoints) == 1, "no checkpoint means the next attempt re-diagnoses from scratch"
    assert checkpoints[0]["task_id"] == "t" and checkpoints[0]["run_id"] == 1


def test_a_saved_result_still_outranks_the_receipt(monkeypatch):
    """Regression: the primary source first — a recorded result keeps its own exit code."""
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, "worker_result",
                        lambda _db, p: {"claim_lock": p["claim_lock"], "exit_code": 0,
                                        "last_output": "готово"})
    assert _exit_code("unknown") == 0


def test_a_terminal_receipt_is_unchanged(monkeypatch):
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, "worker_result",
                        lambda _db, p: {"claim_lock": p["claim_lock"], "exit_code": 0})
    assert _exit_code("terminal") == 0


# --- 2. the crash sweep requeues it without charging the card ----------------------------------

def _state(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


def test_worker_lost_code_is_classified_as_a_platform_requeue():
    dead = kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", kb.KANBAN_WORKER_LOST_EXIT_CODE)
    assert dead.worker_lost, "exit 71 means the platform lost the execution"
    assert dead.run_outcome == "interrupted", "the board must read a platform condition"
    assert not dead.rate_limited and not dead.owner_lost
    assert "without counting a failure" in dead.error_text


def test_worker_lost_does_not_disturb_the_other_exit_kinds():
    """The exemption stays narrow: 0 / 1 / 69 / 75 keep their documented meanings."""
    assert kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", 0).protocol_violation
    crash = kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", 1)
    assert crash.run_outcome == "crashed" and not crash.worker_lost
    assert kbd._classify_dead_worker(
        _absent_pid(), f"{HOST}:1", kb.KANBAN_OWNER_LOST_EXIT_CODE).run_outcome == "owner_lost"
    assert kbd._classify_dead_worker(
        _absent_pid(), f"{HOST}:1", kb.KANBAN_RATE_LIMIT_EXIT_CODE).run_outcome == "rate_limited"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    path = tmp_path / "kanban.db"
    kbc.init_db(path)
    conn = kbc.connect(path)
    yield conn, path
    conn.close()


def _dead_run(conn, tid: str, exit_code: int) -> int:
    worker_pid = _absent_pid()
    lock = f"{HOST}:{_absent_pid(base=4_000_000)}"
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
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, worker_pid = ?, "
            "worker_started_at = ?, current_run_id = ?, started_at = ? WHERE id = ?",
            (lock, NOW + 3600, worker_pid, f"{lock}:1|1", run, started, tid),
        )
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                 "VALUES (?,?,'claimed',?,?)", (tid, run, json.dumps({"source_status": "ready"}), started))
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                 "VALUES (?,?,'worker_bound',?,?)",
                 (tid, run, json.dumps({"pid": worker_pid, "claim_lock": lock,
                                        "started_at": f"{lock}:1|1"}), started + 10))
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                 "VALUES (?,?,'worker_result',?,?)",
                 (tid, run, json.dumps({"pid": worker_pid, "claim_lock": lock,
                                        "exit_code": exit_code, "last_output": ""}), NOW - 5))
    conn.commit()
    return run


def test_platform_lost_execution_requeues_without_spending_the_budget(board):
    conn, _ = board
    _dead_run(conn, "t_wlost", kb.KANBAN_WORKER_LOST_EXIT_CODE)

    kbd.detect_crashed_workers(conn)

    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == [], (
        "a platform loss must never auto-block a card")
    state = _state(conn, "t_wlost")
    assert state["status"] == "ready", "the card must go back to the queue, not to blocked"
    assert int(state["consecutive_failures"]) == 0, "the platform's loss is not the card's fault"
    assert state["claim_lock"] is None


def test_repeated_platform_losses_never_trip_the_breaker(board):
    """A flapping host must not walk a long card into the failure limit."""
    conn, _ = board
    for i in range(5):
        _dead_run(conn, f"t_wl{i}", kb.KANBAN_WORKER_LOST_EXIT_CODE)
    kbd.detect_crashed_workers(conn)
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == []
    for i in range(5):
        assert _state(conn, f"t_wl{i}")["status"] == "ready"
        assert int(_state(conn, f"t_wl{i}")["consecutive_failures"]) == 0


def test_a_genuine_crash_still_charges_the_budget(board):
    """The exemption stays narrow: exit 1 keeps spending the documented retry budget."""
    conn, _ = board
    _dead_run(conn, "t_crash16", 1)
    kbd.detect_crashed_workers(conn)
    assert int(_state(conn, "t_crash16")["consecutive_failures"]) == 1


# --- 3. the drain names the managed workers it force-stopped -----------------------------------

def test_the_force_drain_warning_names_managed_workers(monkeypatch, caplog):
    """01:16:32 logged "0 active agent(s)" while six managed workers were live and killed.

    The journal is the only record an operator has of what a restart interrupted: it must
    carry the managed count, or six killed turns read as a phantom drain timeout.
    """
    import logging
    import time

    from gateway import run_shutdown
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, _adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.01
    monkeypatch.setattr(run_shutdown, "managed_turn_count", lambda _runner: 6)
    monkeypatch.setattr(
        "tools.terminal_tool_lifecycle.cleanup_all_environments", lambda: None)
    monkeypatch.setattr(
        "tools.browser_session_lifecycle.cleanup_all_browsers", lambda: None)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])
    monkeypatch.setattr("tools.async_delegation.interrupt_all", lambda *a, **k: 0)

    ctx = run_shutdown.GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    ctx.started_at = time.monotonic()

    with caplog.at_level(logging.WARNING):
        import asyncio
        asyncio.run(runner._stop_interrupt_remaining_work(ctx))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    drain = [m for m in warnings if "Gateway drain timed out" in m]
    assert drain, f"no drain-timeout warning was logged; saw {warnings}"
    assert "6 managed-worker turn(s)" in drain[0], (
        f"the warning must name the six managed workers it force-stopped, got: {drain[0]}")
    assert "0 active agent(s)" in drain[0], drain[0]
