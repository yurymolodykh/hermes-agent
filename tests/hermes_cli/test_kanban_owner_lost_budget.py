"""OPS-15: a gateway restart must not lose the cards it interrupts.

Incident (18.11 20:01): ``hermes gateway restart --yes`` opened a restart after-turn wait.
While that window was open the owner refused every submitter's socket, so each submitter hit its
``OWNER_WAIT_S=180`` budget first, exited 1, and its managed turn died of the submitter's death
(``Managed worker lost: EOFError``). That death — not drained work — released the drain, the
gateway exited 75 (shown as a clean 0 by ``SuccessExitStatus=75``) and five cards were booked as
crashes.

The fix has two halves, pinned here:

1. the submitter's owner-silence budget outlives a legitimate restart (>=10 min, env-tunable up);
2. a submitter that truly outlives it exits ``KANBAN_OWNER_LOST_EXIT_CODE`` with a durable
   checkpoint, and the crash sweep requeues the card WITHOUT counting a failure.

Plus the observability half: every termination writes one GATEWAY TERMINATION line naming its
initiator, its reason and how much work was live, before anything drains.
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


# --- 1. the submitter's budget outlives a restart ---------------------------------------------

def test_owner_silence_budget_outlives_a_gateway_restart():
    """The 18.11 gate was 180s — shorter than a restart's own after-turn wait. It must be >= 10 min."""
    assert wwc.owner_wait_seconds() >= 600.0
    assert wwc.OWNER_WAIT_FLOOR_S >= 600.0


def test_owner_silence_budget_is_tunable_upward_but_never_below_the_floor(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_OWNER_WAIT_SECONDS", "900")
    assert wwc.owner_wait_seconds() == 900.0
    monkeypatch.setenv("HERMES_KANBAN_OWNER_WAIT_SECONDS", "60")
    assert wwc.owner_wait_seconds() == wwc.OWNER_WAIT_FLOOR_S, "a shorter budget is the incident's gate"
    monkeypatch.setenv("HERMES_KANBAN_OWNER_WAIT_SECONDS", "not-a-number")
    assert wwc.owner_wait_seconds() == wwc.OWNER_WAIT_S


# --- 2. owner-lost is a requeue, not a card failure --------------------------------------------

def test_owner_lost_exit_code_is_classified_as_an_infrastructure_requeue():
    dead = kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", kb.KANBAN_OWNER_LOST_EXIT_CODE)
    assert dead.owner_lost, "exit 69 means the OWNER was absent"
    assert dead.run_outcome == "owner_lost"
    assert not dead.rate_limited
    assert "without counting a failure" in dead.error_text


def test_owner_lost_does_not_disturb_the_other_exit_kinds():
    """Only 69 is exempt; the documented meanings of 0 / 1 / 75 must not drift."""
    assert kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", 0).protocol_violation
    crash = kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", 1)
    assert crash.run_outcome == "crashed" and not crash.owner_lost
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
        # A second attempt on the same card: re-arm it without touching consecutive_failures.
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


def _state(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


def test_owner_lost_requeues_the_card_without_spending_its_budget(board):
    conn, _ = board
    _dead_run(conn, "t_ownerlost", kb.KANBAN_OWNER_LOST_EXIT_CODE)

    kbd.detect_crashed_workers(conn)

    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == [], \
        "an absent owner must never auto-block a card"
    state = _state(conn, "t_ownerlost")
    assert state["status"] == "ready"
    assert int(state["consecutive_failures"]) == 0
    assert state["claim_lock"] is None


def test_owner_lost_never_trips_the_breaker_even_repeatedly(board):
    """Several restarts during one long card must not accumulate toward the failure limit."""
    conn, _ = board
    for i in range(5):
        _dead_run(conn, f"t_ol{i}", kb.KANBAN_OWNER_LOST_EXIT_CODE)
    kbd.detect_crashed_workers(conn)
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == []
    for i in range(5):
        assert _state(conn, f"t_ol{i}")["status"] == "ready"
        assert int(_state(conn, f"t_ol{i}")["consecutive_failures"]) == 0


def test_a_plain_crash_still_counts_and_trips_at_the_limit(board):
    """The exemption is narrow: a real crash keeps the documented retry budget."""
    conn, _ = board
    _dead_run(conn, "t_crash", 1)
    kbd.detect_crashed_workers(conn)
    assert int(_state(conn, "t_crash")["consecutive_failures"]) == 1

    _dead_run(conn, "t_crash", 1)
    kbd.detect_crashed_workers(conn)
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == ["t_crash"]
    assert _state(conn, "t_crash")["status"] == "blocked"


# --- 3. the checkpoint the submitter leaves behind ---------------------------------------------

def test_owner_lost_checkpoint_is_written_to_the_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    path = tmp_path / "kanban.db"
    kbc.init_db(path)
    conn = kbc.connect(path)
    try:
        run = _dead_run(conn, "t_cp", 1)
        claim = wwc._Claim(answered=time.monotonic() - 640.0, session_id="local-abc",
                           admission_id="adm-1")
        wwc._checkpoint_owner_lost(
            {"task_id": "t_cp", "run_id": run}, str(path), claim, 600.0, "Gateway draining: restart_wait")
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id='t_cp' AND kind='checkpoint' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None, "an owner-lost exit must leave a durable note"
        payload = json.loads(row["payload"])
        assert payload["kind"] == "owner_lost"
        assert payload["owner_silent_s"] >= 600
        assert payload["budget_s"] == 600.0
        assert "restart_wait" in payload["detail"]
    finally:
        conn.close()


def test_a_checkpoint_write_failure_never_masks_the_exit_code(tmp_path, monkeypatch):
    """A note that cannot be written must not replace the exit code the sweep depends on."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(kb, "_append_event", _boom)
    wwc._checkpoint_owner_lost(
        {"task_id": "t_x", "run_id": 1}, str(tmp_path / "kanban.db"),
        wwc._Claim(answered=time.monotonic()), 600.0, "x")  # must not raise


# --- 4. the restart wait must not hold a turn it is itself starving ---------------------------

class _FakeWorker:
    def __init__(self, kanban, pid):
        self.closed = type("E", (), {"is_set": lambda self: False})()
        self.worker = (pid,)
        self.process = type("P", (), {"pid": pid})()
        self.kanban_submitted = kanban


def _runner(workers, chat=0, api=0, deferred=0, wedged=0):
    from gateway.run import GatewayRunner
    r = object.__new__(GatewayRunner)
    r._running_agent_count = lambda: chat
    r._active_api_run_count = lambda: api
    r._active_deferred_agent_worker_count = lambda: deferred
    r._wedged_chat_agent_count = lambda: wedged
    r._restart_wait_cron_counts = lambda: {"awaitable": 0, "wedged": 0, "restart_safe": 0}
    auth = type("A", (), {"_managed_workers": workers})()
    r._all_authorities = lambda: [auth]
    return r


def test_restart_wait_does_not_hold_kanban_turns_it_is_starving(monkeypatch):
    """The 20:01 deadlock: the wait held turns whose submitters it refused for the whole wait.

    A Kanban submitter dials inside ``outside_restart_wait()``, so the owner refuses it while the
    after-turn wait is open. Holding the wait for such a turn waits on a turn that cannot progress
    — it always ends in the submitter's death. Only those are excluded; every other managed turn
    is still awaited.
    """
    from gateway import session_runtime_workers as srw
    monkeypatch.setattr(srw, "all_authorities", lambda r: list(r._all_authorities()))

    incident = {f"local-{i}": _FakeWorker(True, 1000 + i) for i in range(5)}
    r = _runner(incident, chat=1)
    assert r._awaitable_work_count() == 1, "only the chat turn may hold the wait"
    assert r._active_work_count() == 6, "the announcement must still see ALL live work"

    # A managed turn that is NOT Kanban-submitted is still awaited.
    assert _runner({"s": _FakeWorker(False, 2001)})._awaitable_work_count() == 1

    # Mixed: 3 Kanban + 2 other managed + 1 wedged chat -> the 2 others hold it.
    mixed = {f"k{i}": _FakeWorker(True, 3000 + i) for i in range(3)}
    mixed.update({f"m{i}": _FakeWorker(False, 3100 + i) for i in range(2)})
    assert _runner(mixed, chat=1, wedged=1)._awaitable_work_count() == 2

    # All Kanban: the wait ends at once instead of blocking for the whole cap.
    assert _runner({f"k{i}": _FakeWorker(True, 4000 + i) for i in range(6)})._awaitable_work_count() == 0


def test_an_unreadable_unit_list_fails_soft_toward_waiting(monkeypatch):
    """Never a false "drained": if the count cannot be read, nothing is excluded."""
    from gateway import session_runtime_workers as srw

    def boom(*a, **k):
        raise RuntimeError("read failed")

    monkeypatch.setattr(srw, "kanban_submitted_count", boom)
    assert _runner({"_": _FakeWorker(True, 1)})._restart_starved_managed_count() == 0

