"""A refused start on split code: separate exit code, no failure budget, one actionable reason.

26.11.2026: a gateway served ``da0f60614069`` while workers booted ``8fc03744473``. Every
``usage.main`` came back ``invalid_params``, the receipt queue rolled back and each turn died ~60 s
in — five cards walked into retry limit on ONE host condition. The gate now refuses before the first
dial with ``CODE_SKEW_EXIT_CODE`` (76), and the crash sweep must treat that as a host condition:
requeue to ``ready`` without charging ``consecutive_failures`` and space the retries.
"""
from __future__ import annotations

import json
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client as wwc

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]


def _absent_pid(base: int = 3_950_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


# --- the code and its meaning --------------------------------------------------------------------

def test_the_two_sides_agree_on_the_code():
    assert kb.KANBAN_CODE_SKEW_EXIT_CODE == wwc.CODE_SKEW_EXIT_CODE == 76, (
        "the submitter and the crash sweep must name the same exit code")


def test_the_code_is_not_a_card_crash():
    """Explicit: 76 is not 1, not the platform-loss code, and not the owner-lost code."""
    assert kb.KANBAN_CODE_SKEW_EXIT_CODE not in {
        0, 1, kb.KANBAN_WORKER_LOST_EXIT_CODE, kb.KANBAN_OWNER_LOST_EXIT_CODE,
        kb.KANBAN_RATE_LIMIT_EXIT_CODE,
    }


def test_exit_code_76_classifies_as_code_skew():
    assert kbd._exit_code_kind(kb.KANBAN_CODE_SKEW_EXIT_CODE) == (
        "code_skew", kb.KANBAN_CODE_SKEW_EXIT_CODE)


def test_classification_reads_as_a_host_condition_with_the_fix():
    dead = kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", kb.KANBAN_CODE_SKEW_EXIT_CODE)
    assert dead.code_skew
    assert dead.run_outcome == "code_skew"
    assert not dead.worker_lost and not dead.rate_limited and not dead.owner_lost
    assert "without counting a failure" in dead.error_text
    assert "restart the gateway" in dead.error_text, "the message must name the one fix"


def test_the_new_kind_does_not_disturb_the_others():
    assert kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", 1).run_outcome == "crashed"
    assert not kbd._classify_dead_worker(_absent_pid(), f"{HOST}:1", 1).code_skew
    assert kbd._classify_dead_worker(
        _absent_pid(), f"{HOST}:1", kb.KANBAN_WORKER_LOST_EXIT_CODE).run_outcome == "interrupted"


# --- the board: requeue, no failure budget --------------------------------------------------------

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    path = tmp_path / "kanban.db"
    kbc.init_db(path)
    conn = kbc.connect(path)
    yield conn, path
    conn.close()


def _state(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


def _dead_run(conn, tid: str, exit_code: int) -> int:
    pid = _absent_pid()
    lock = f"{HOST}:{_absent_pid(base=4_100_000)}"
    started = NOW - 600
    run = int(conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'default', 'running', ?, ?, ?, ?)",
        (tid, lock, NOW + 3600, pid, started),
    ).lastrowid)
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
        "worker_pid, worker_started_at, current_run_id, started_at, priority, consecutive_failures) "
        "VALUES (?, ?, 'default', 'running', ?, ?, ?, ?, ?, ?, ?, 5, 2)",
        (tid, tid, NOW - 86400, lock, NOW + 3600, pid, f"{lock}:1|1", run, started),
    )
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                 "VALUES (?,?,'claimed',?,?)", (tid, run, json.dumps({"source_status": "ready"}), started))
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                 "VALUES (?,?,'worker_bound',?,?)",
                 (tid, run, json.dumps({"pid": pid, "claim_lock": lock,
                                        "started_at": f"{lock}:1|1"}), started + 10))
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                 "VALUES (?,?,'worker_result',?,?)",
                 (tid, run, json.dumps({"pid": pid, "claim_lock": lock,
                                        "exit_code": exit_code, "last_output": ""}), NOW - 5))
    conn.commit()
    return run


def test_a_split_contract_requeues_without_charging_the_card(board):
    conn, _ = board
    _dead_run(conn, "t_skew", kb.KANBAN_CODE_SKEW_EXIT_CODE)

    kbd.detect_crashed_workers(conn)

    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == [], (
        "one host condition must never auto-block a card")
    state = _state(conn, "t_skew")
    assert state["status"] == "ready", "the card goes back to the queue, not to blocked"
    assert int(state["consecutive_failures"]) == 2, (
        "the pre-existing count must be untouched: a host skew is not the card's failure")
    assert state["claim_lock"] is None


def test_many_cards_on_one_skew_never_trip_the_breaker(board):
    """The incident had five cards; one host condition must not block any of them."""
    conn, _ = board
    for i in range(5):
        _dead_run(conn, f"t_skew_{i}", kb.KANBAN_CODE_SKEW_EXIT_CODE)

    kbd.detect_crashed_workers(conn)

    for i in range(5):
        state = _state(conn, f"t_skew_{i}")
        assert state["status"] == "ready", f"card {i} must stay workable"
        assert int(state["consecutive_failures"]) == 2


def test_a_real_crash_still_charges(board):
    """The exemption stays narrow: exit 1 keeps spending the budget."""
    conn, _ = board
    _dead_run(conn, "t_crash", 1)

    kbd.detect_crashed_workers(conn)

    assert int(_state(conn, "t_crash")["consecutive_failures"]) >= 3


# --- spacing: retries are throttled, never hammered ------------------------------------------------

def test_the_retry_is_spaced_after_a_skew_requeue(board, monkeypatch):
    conn, _ = board
    _dead_run(conn, "t_cool", kb.KANBAN_CODE_SKEW_EXIT_CODE)
    kbd.detect_crashed_workers(conn)
    row = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = 't_cool' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["outcome"] == "code_skew"

    guard = kbd.check_respawn_guard(conn, "t_cool")
    assert guard == "code_skew_cooldown", "the retry must wait, not spawn on the next tick"

    # After the cooldown the card must be spawnable again (the restart heals it).
    later = NOW + kbd.DEFAULT_CODE_SKEW_COOLDOWN_SECONDS + 30
    monkeypatch.setattr(time, "time", lambda: later)
    assert kbd.check_respawn_guard(conn, "t_cool") != "code_skew_cooldown"


def test_a_skew_outcome_supersedes_stale_blocker_text():
    """The blocker text of an older run must not park the card after a skew requeue."""
    assert "code_skew" in kbd._RESPAWN_BLOCKER_SUPERSEDED_OUTCOMES


# --- the worker start hook: the two probes the card asks for --------------------------------------

def _run_hook(monkeypatch, gate_result=None, gate_exc=None):
    from hermes_cli import code_skew_gate
    checkpoints = []
    if gate_exc is not None:
        def boom():
            raise gate_exc
        monkeypatch.setattr(code_skew_gate, "check_code_skew", boom)
    else:
        monkeypatch.setattr(code_skew_gate, "check_code_skew", lambda: gate_result)
    monkeypatch.setattr(wwc, "_checkpoint_code_skew",
                        lambda params, db, skew: checkpoints.append(skew))
    code = wwc._refuse_on_code_skew({"task_id": "t", "run_id": 1}, "/nonexistent.db")
    return code, checkpoints


def test_matching_versions_let_the_worker_start(monkeypatch):
    """Probe 2a: the gate finds nothing → None, so ``run()`` proceeds."""
    code, checkpoints = _run_hook(monkeypatch, gate_result=None)
    assert code is None
    assert checkpoints == []


def test_split_versions_refuse_the_start_with_its_own_code(monkeypatch):
    """Probe 2b: a proven split → exit 76 and a checkpoint naming both commits."""
    skew = wwc_code_skew()
    code, checkpoints = _run_hook(monkeypatch, gate_result=skew)
    assert code == wwc.CODE_SKEW_EXIT_CODE == 76
    assert code != 1
    assert checkpoints == [skew], "the refusal must be recorded on the card"


def test_a_broken_gate_never_blocks_a_start(monkeypatch):
    """Fail-open: an internal gate error is not evidence of a split."""
    code, _ = _run_hook(monkeypatch, gate_exc=RuntimeError("no git"))
    assert code is None


def wwc_code_skew():
    from hermes_cli.code_skew_gate import CodeSkew
    return CodeSkew(
        disk_sha="8fc03744473d4f4c2f0b6bdbd0d6d2e2f0b0f4c1",
        gateway_sha="da0f60614069c1a2b3c4d5e6f7a8b9c0d1e2f3a4",
        problems=("usage.main on disk sends `prompt_cache_clock`, which the gateway rejects",),
        gateway_pid=2524011,
    )
