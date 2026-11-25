"""Regressions for the eight defects an independent review found in the stop-cause commit.

Each test here FAILED against ``19b9577a`` (the reviewed revision) and passes on top of it. They are
the reviewer's own probes (``repro_stale_reason.py``, ``probe_handler_env.py``,
``probe_unlabelled_paths.py``, ``probe_sigterm_hang.py``) reduced to their assertions, plus the two
P3 cases (absent vs corrupt record, the submitter watchdog's hard exit).

D1  a cause recorded for an EARLIER attempt must never be read as this attempt's
D2  the SIGTERM handler must see the card after ``_submit()`` scrubbed ``HERMES_KANBAN_*``
D3  ``detect_stale_running`` must name the cause of the worker it kills
D4  ``invalidate_descendants_for_parent_reopen`` must name it too
D5  a meaningful transport reason stays verbatim (``managed_worker_failed`` is not ``EOFError``)
D6  the handler must not block the stop on a contended board
D7  absent and corrupt records must read differently
D8  the submitter watchdog must name the cause before ``os._exit``
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_stop_reason as ksr
from hermes_cli import kanban_worker_client as kwc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fingerprint(pid: int) -> str:
    return kbd._process_fingerprint(pid) or "unreadable"


def _claim(conn, workspace: Path, *, max_runtime=None) -> tuple[str, int]:
    task_id = kb.create_task(conn, title="review defect", body="x",
                             assignee="03-software-engineer", workspace_path=str(workspace),
                             max_runtime_seconds=max_runtime)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    assert kb.claim_task(conn, task_id) is not None
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    return task_id, run_id


def _retry(conn, task_id: str) -> int:
    """Close the attempt the way a retry does and claim the card again."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, worker_pid=NULL, "
            "worker_started_at=NULL, current_run_id=NULL WHERE id=?", (task_id,))
    assert kb.claim_task(conn, task_id) is not None
    return conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]


def _events(conn, task_id, kind=None):
    sql = "SELECT kind, payload, run_id FROM task_events WHERE task_id=?"
    args = [task_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    out = []
    for row in conn.execute(sql + " ORDER BY id", args):
        item = dict(row)
        payload = item.get("payload")
        item["payload"] = json.loads(payload) if isinstance(payload, str) else (payload or {})
        out.append(item)
    return out


class _Policy:
    def __init__(self, payload):
        self.kanban_json = json.dumps(payload)


# ---------------------------------------------------------------------------
# D1 — a stale record is not this attempt's cause
# ---------------------------------------------------------------------------


def test_d1_the_read_side_rejects_a_record_naming_another_attempt(board, tmp_path):
    """A SIGKILLed retry must not inherit the previous attempt's ``limit_exceeded``.

    The record is written straight to disk (not through a stop path) so the read-side guard is
    what is under test here, independently of the claim-time cleanup below.
    """
    from gateway.session_kanban import record_worker_loss

    ws = tmp_path / "ws-stale-read"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run1 = _claim(conn, ws)
    # Attempt 1 hit its time limit and said so.
    ksr.write_reason_for_task(task_id, "limit_exceeded", workspace=ws,
                              detail="остановлен: лимит времени 60 с", run_id=run1)
    run2 = _retry(conn, task_id)

    # Attempt 2 is SIGKILLed: the file on disk still names attempt 1.
    ksr.write_reason_for_task(task_id, "limit_exceeded", workspace=ws,
                              detail="остановлен: лимит времени 60 с", run_id=run1)
    assert ksr.read_reason_for_workspace(ws, run_id=run2) is None
    assert ksr.inspect_reason(ksr.stop_file_path(ws), run_id=run2)[0] == ksr.STALE

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    record_worker_loss(_Policy({"task_id": task_id, "run_id": run2, "claim_lock": "host-1-x",
                                "db": db_path, "workspace": str(ws)}),
                       "EOFError", pid=999999, bound=True)
    payload = _events(conn, task_id, "worker_lost")[-1]["payload"]
    assert payload.get("stop_reason") != "limit_exceeded", "attempt 2 must not borrow attempt 1's cause"
    assert payload["reason"] == "unknown (запись прошлой попытки)"
    conn.close()


def test_d1_a_new_attempt_clears_the_previous_attempts_record(board, tmp_path):
    """Claiming the card a second time removes the cause attempt 1 left in the shared workspace."""
    ws = tmp_path / "ws-stale-clear"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run1 = _claim(conn, ws)
    ksr.write_reason_for_task(task_id, "limit_exceeded", workspace=ws,
                              detail="остановлен: лимит времени 60 с", run_id=run1)
    assert (ws / ksr.STOP_FILE_NAME).exists()

    run2 = _retry(conn, task_id)

    assert run2 != run1
    assert not (ws / ksr.STOP_FILE_NAME).exists(), "a new attempt must start with no recorded cause"
    conn.close()


# ---------------------------------------------------------------------------
# D2 — the SIGTERM handler still knows the card after the environment is scrubbed
# ---------------------------------------------------------------------------


class _CapturedHandler:
    """The stop handler as ``signal.signal`` actually received it."""

    def __init__(self):
        self.fn = None


@pytest.fixture
def capture_handler(monkeypatch):
    captured = _CapturedHandler()
    real_signal = signal.signal

    def fake_signal(sig, handler):
        if sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            captured.fn = handler
            return None
        return real_signal(sig, handler)

    monkeypatch.setattr(signal, "signal", fake_signal)
    return captured


def test_d2_the_handler_names_the_cause_after_submit_scrubs_the_environment(
        board, tmp_path, monkeypatch, capture_handler):
    """``_submit()`` pops every ``HERMES_KANBAN_*`` key before the turn: the handler must not need
    them at signal time. This is the reviewer's ``scrubbed`` mode.
    """
    ws = tmp_path / "ws-handler"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _claim(conn, ws)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    for key, value in (("HERMES_KANBAN_TASK", task_id), ("HERMES_KANBAN_RUN_ID", str(run_id)),
                       ("HERMES_KANBAN_DB", str(db_path)), ("HERMES_KANBAN_WORKSPACE", str(ws)),
                       ("HERMES_KANBAN_CLAIM_LOCK", "host-1-x")):
        monkeypatch.setenv(key, value)
    ksr.write_reason_for_task(task_id, "limit_exceeded", workspace=ws,
                              detail="остановлен: лимит времени 60 с", run_id=run_id)

    kwc._install_stop_handler()
    assert capture_handler.fn is not None, "the handler must be installed"

    # Exactly what _submit() does at the start of every real attempt.
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_") and key not in (
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_BUSY_TIMEOUT_MS",
                "HERMES_KANBAN_OWNER_WAIT_SECONDS"):
            os.environ.pop(key)

    exits = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    capture_handler.fn(signal.SIGTERM, None)

    assert exits == [1], "the handler must still exit the worker"
    conn = kbc.connect()
    events = _events(conn, task_id, "worker_stopping")
    assert events, "the dying worker must leave a durable note naming its cause"
    assert events[-1]["payload"]["reason"] == "limit_exceeded"
    assert events[-1]["payload"]["detail"].startswith("остановлен: лимит времени")
    conn.close()


# ---------------------------------------------------------------------------
# D3 / D4 — the two reclaim paths that killed a live worker with no cause
# ---------------------------------------------------------------------------


def _spawn_sleep():
    proc = subprocess.Popen(["sleep", "120"])
    time.sleep(0.3)
    return proc


def test_d3_detect_stale_running_names_the_cause_of_the_worker_it_kills(board, tmp_path):
    """The stale-heartbeat sweep is a reassignment, not an anonymous kill."""
    ws = tmp_path / "ws-stale-hb"
    ws.mkdir()
    conn = kbc.connect()
    proc = _spawn_sleep()
    try:
        task_id, run_id = _claim(conn, ws)
        host = kb._host_prefix()
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid=?, worker_started_at=?, claim_lock=?, "
                "last_heartbeat_at=NULL, started_at=datetime('now','-9000 seconds') WHERE id=?",
                (proc.pid, _fingerprint(proc.pid), f"{host}probe", task_id))
            conn.execute("UPDATE task_runs SET started_at=datetime('now','-9000 seconds') "
                         "WHERE task_id=?", (task_id,))

        seen = {}

        def signal_fn(pid, sig):
            # The whole point: the record already names THIS attempt when the signal is sent.
            seen["payload"] = ksr.read_reason_for_workspace(ws, run_id=run_id)
            os.kill(pid, sig)

        assert kbd.detect_stale_running(conn, stale_timeout_seconds=60, signal_fn=signal_fn)
        assert seen.get("payload") is not None, "the cause must be on disk before the SIGTERM"
        assert seen["payload"]["reason"] == "reassigned"
        assert seen["payload"]["run_id"] == run_id
        event = _events(conn, task_id, "stale")[-1]["payload"]
        assert event["stop_reason"] == "reassigned"
        assert "heartbeat" in event["stop_detail"]
    finally:
        if proc.poll() is None:
            proc.kill()
        conn.close()


def test_d4_invalidating_a_descendant_names_the_cause_of_the_worker_it_kills(board, tmp_path):
    """Reopening an ancestor demotes a running descendant: that kill is a reassignment too.

    Asserted through durable facts only (the record left in the workspace and the audit event),
    so the test does not depend on intercepting a module-level function whose identity other
    tests in this package replace.
    """
    ws = tmp_path / "ws-descendant"
    ws.mkdir()
    conn = kbc.connect()
    proc = _spawn_sleep()
    try:
        parent = kb.create_task(conn, title="parent", body="x", assignee="03-software-engineer",
                                workspace_path=str(ws))
        child = kb.create_task(conn, title="child", body="x", assignee="03-software-engineer",
                               workspace_path=str(ws))
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        kb.recompute_ready(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child) is not None
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (child,)).fetchone()[0]
        host = kb._host_prefix()
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=?, worker_started_at=?, claim_lock=? "
                         "WHERE id=?", (proc.pid, _fingerprint(proc.pid), f"{host}probe2", child))

        result = kb.invalidate_descendants_for_parent_reopen(conn, parent, author="test")

        assert [t[0] for t in result["terminations"]] == [proc.pid]
        # The demoted worker is really dead...
        proc.wait(timeout=10)
        assert proc.returncode is not None
        # ...and the cause of that death is on disk, scoped to the attempt that was killed.
        payload = ksr.read_reason_for_workspace(ws, run_id=run_id)
        assert payload is not None, "the killed worker must leave a named cause"
        assert payload["reason"] == "reassigned"
        assert payload["run_id"] == run_id
        assert "предок переоткрыт" in payload["detail"]
        event = _events(conn, child, "descendant_invalidated")[-1]["payload"]
        assert event["stop_reason"] == "reassigned"
    finally:
        if proc.poll() is None:
            proc.kill()
        conn.close()


# ---------------------------------------------------------------------------
# D6 — the handler must not block the stop on a contended board
# ---------------------------------------------------------------------------


def test_d6_the_handler_does_not_wait_out_the_busy_timeout(board, tmp_path, monkeypatch,
                                                           capture_handler):
    """A stop that lands while the board is locked must exit promptly, not after BUSY_TIMEOUT.

    The reviewer measured 13.9 s at ``busy=2000`` and >45 s at the 120 s default, against 1.9 s
    uncontended. The handler now opens its own connection with ``busy_timeout=0``.
    """
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "2000")
    ws = tmp_path / "ws-hang"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _claim(conn, ws)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    for key, value in (("HERMES_KANBAN_TASK", task_id), ("HERMES_KANBAN_RUN_ID", str(run_id)),
                       ("HERMES_KANBAN_DB", str(db_path)), ("HERMES_KANBAN_WORKSPACE", str(ws))):
        monkeypatch.setenv(key, value)
    ksr.write_reason_for_task(task_id, "limit_exceeded", workspace=ws, detail="лимит", run_id=run_id)

    kwc._install_stop_handler()
    monkeypatch.setattr(os, "_exit", lambda code: None)

    holder = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE tasks SET last_heartbeat_at=last_heartbeat_at WHERE id=?", (task_id,))
        started = time.monotonic()
        capture_handler.fn(signal.SIGTERM, None)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert elapsed < 1.0, f"the stop waited {elapsed:.1f}s on a contended board"


# ---------------------------------------------------------------------------
# D5 — a meaningful transport reason is not flattened into "unknown"
# ---------------------------------------------------------------------------


def test_d5_a_meaningful_transport_reason_stays_verbatim():
    """``managed_worker_failed`` asserts a cause; only a pure stream end is wrapped."""
    assert ksr.fallback_for(ksr.ABSENT, "managed_worker_failed") == "managed_worker_failed"
    assert ksr.fallback_for(ksr.ABSENT, "EOFError") == "unknown (EOFError)"
    assert ksr.fallback_for(ksr.ABSENT, None) == "unknown"


# ---------------------------------------------------------------------------
# D7 — absent and corrupt records are distinguishable
# ---------------------------------------------------------------------------


def test_d7_absent_and_corrupt_records_read_differently(board, tmp_path):
    ws = tmp_path / "ws-corrupt"
    ws.mkdir()
    assert ksr.inspect_reason(ksr.stop_file_path(ws))[0] == ksr.ABSENT
    assert ksr.fallback_for(ksr.ABSENT, "EOFError") == "unknown (EOFError)"

    (ws / ksr.STOP_FILE_NAME).write_text("{not json", encoding="utf-8")
    status, payload = ksr.inspect_reason(ksr.stop_file_path(ws))
    assert status == ksr.CORRUPT and payload is None
    assert ksr.fallback_for(ksr.CORRUPT, "EOFError") == "unknown (запись повреждена)"

    (ws / ksr.STOP_FILE_NAME).write_text(json.dumps({"detail": "no reason field"}), encoding="utf-8")
    assert ksr.inspect_reason(ksr.stop_file_path(ws))[0] == ksr.CORRUPT


# ---------------------------------------------------------------------------
# D8 — the submitter watchdog names the cause before its hard exit
# ---------------------------------------------------------------------------


def test_d8_the_submitter_watchdog_notes_the_cause_before_exiting(board, tmp_path):
    """``_watch_submitter`` exits with ``os._exit``: the note is the last chance to say why."""
    from gateway.session_kanban import _note_submitter_death

    ws = tmp_path / "ws-watchdog"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _claim(conn, ws)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    ksr.write_reason_for_task(task_id, "limit_exceeded", workspace=ws,
                              detail="остановлен: лимит времени 60 с", run_id=run_id)

    _note_submitter_death(Path(db_path), run_id)

    events = _events(conn, task_id, "worker_stopping")
    assert events, "the hard exit must leave the cause on the board"
    assert events[-1]["payload"]["reason"] == "limit_exceeded"
    assert events[-1]["payload"]["source"] == "submitter_watch"
    conn.close()
