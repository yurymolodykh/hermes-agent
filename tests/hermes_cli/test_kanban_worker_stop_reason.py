"""Why a stopped Kanban worker should say so — one test per stop cause.

Before this, four very different platform conditions (time limit, reassignment, archive, gateway
shutdown) all reached the card as a bare ``EOFError``/«обрыв потока»: the submitter had no SIGTERM
handler, so it died before writing anything, and the reader could only report that the frame
stream ended. These tests pin the transport (``.stop-reason.json`` written BEFORE the signal), the
worker's own durable note, and the gateway's substitution of the recorded cause for ``EOFError``.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_stop_reason as ksr
# Imported at module scope like every other test that reaches it: the module bootstraps the
# committed environment on import, which must happen before any test chdirs to a tmp dir.
from hermes_cli import kanban_worker_client as kwc


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an initialised kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_task(conn, workspace: Path, *, status="running", max_runtime=None) -> str:
    task_id = kb.create_task(conn, title="stop reason", body="x", assignee="03-software-engineer")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status=?, workspace_path=?, max_runtime_seconds=?, "
            "claim_lock=NULL, worker_pid=NULL WHERE id=?",
            (status, str(workspace), max_runtime, task_id),
        )
    return task_id


def _load_worker_client():
    """The submitter module, imported at module scope (see the top of this file)."""
    return kwc


def _fingerprint(pid: int) -> str:
    """The spawn-time identity the dispatcher records; a fake one reads as a recycled PID."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    return _process_fingerprint(pid) or "unreadable"


def _live_claim(conn, workspace: Path, *, max_runtime=None):
    """A running card with one open run on THIS host, as the dispatcher leaves it."""
    task_id = kb.create_task(conn, title="stop reason", body="x", assignee="03-software-engineer",
                             workspace_path=str(workspace), max_runtime_seconds=max_runtime)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    assert kb.claim_task(conn, task_id) is not None
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=?, worker_started_at=?, workspace_path=? WHERE id=?",
            (os.getpid(), _fingerprint(os.getpid()), str(workspace), task_id),
        )
        if max_runtime:
            # Runtime is measured from the run row: backdate it so the limit has already elapsed.
            conn.execute("UPDATE task_runs SET started_at = started_at - ? WHERE id = ?",
                         (int(max_runtime) + 60, run_id))
    return task_id, run_id


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


# ---------------------------------------------------------------------------
# Transport: the file itself
# ---------------------------------------------------------------------------


def test_write_reason_is_atomic_and_reads_back(board, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert ksr.write_reason(ws / ksr.STOP_FILE_NAME, "limit_exceeded",
                            detail="остановлен: лимит времени 60 с", source="enforce_max_runtime", pid=4242)
    payload = ksr.read_reason(ws / ksr.STOP_FILE_NAME)
    assert payload["reason"] == "limit_exceeded"
    assert payload["detail"] == "остановлен: лимит времени 60 с"
    assert payload["pid"] == 4242
    # No half-written temp files left behind.
    assert [p.name for p in ws.iterdir()] == [ksr.STOP_FILE_NAME]


def test_write_reason_degrades_on_unwritable_workspace(board, tmp_path):
    """A missing/unwritable workspace must not raise: the stop has to proceed anyway."""
    missing_parent = tmp_path / "does-not-exist"
    # A FILE where the directory should be makes mkdir fail for any implementation.
    (tmp_path / "blocker").write_text("x", encoding="utf-8")
    assert ksr.write_reason(tmp_path / "blocker" / ksr.STOP_FILE_NAME, "archived") is False
    assert ksr.read_reason(missing_parent / ksr.STOP_FILE_NAME) is None


def test_unknown_is_the_absence_not_a_recorded_value(board, tmp_path):
    """A SIGKILLed worker leaves no file, and that must read as unknown — never a fake cause."""
    ws = tmp_path / "ws"
    ws.mkdir()
    assert ksr.read_reason_for_workspace(ws) is None
    assert ksr.describe(None) == "unknown"
    assert ksr.is_known(None) is False


# ---------------------------------------------------------------------------
# Stop sites: reason written BEFORE the signal, and a durable copy on the board
# ---------------------------------------------------------------------------


def test_limit_exceeded_written_before_sigterm(board, tmp_path):
    """Acceptance 1: max_runtime overrun records limit_exceeded in the reason file and the run."""
    ws = tmp_path / "ws-limit"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _live_claim(conn, ws, max_runtime=1)

    seen = {}

    def fake_kill(pid, sig):
        # The whole point: the file exists ALREADY when the signal is sent.
        seen["payload"] = ksr.read_reason_for_workspace(ws)

    kbd.enforce_max_runtime(conn, signal_fn=fake_kill)
    conn.commit()

    assert seen["payload"] is not None, "reason must be written before the signal"
    assert seen["payload"]["reason"] == "limit_exceeded"

    row = conn.execute("SELECT outcome, status, metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert row["outcome"] == "timed_out"
    metadata = json.loads(row["metadata"] or "{}")
    assert metadata["stop_reason"] == "limit_exceeded"
    assert "лимит времени" in metadata["stop_detail"]
    conn.close()


def test_reassigned_written_before_sigterm(board, tmp_path):
    """Acceptance 2: a reclaim/reassign names the cause and stamps the run metadata."""
    ws = tmp_path / "ws-reassign"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _live_claim(conn, ws)

    seen = {}

    def fake_kill(pid, sig):
        seen["payload"] = ksr.read_reason_for_workspace(ws)

    assert kb.reclaim_task(conn, task_id, signal_fn=fake_kill)
    conn.commit()

    assert seen.get("payload") and seen["payload"]["reason"] == "reassigned"
    events = _events(conn, task_id, "reclaimed")
    assert events and events[-1]["payload"]["stop_reason"] == "reassigned"
    row = conn.execute("SELECT outcome, metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert row["outcome"] == "reclaimed"
    assert json.loads(row["metadata"] or "{}")["stop_reason"] == "reassigned"
    conn.close()


def test_archived_written_before_sigterm(board, tmp_path):
    """Acceptance 3: archiving a live card records archived, in the event and the run."""
    ws = tmp_path / "ws-archived"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _live_claim(conn, ws)

    seen = {}

    def fake_kill(pid, sig):
        seen["payload"] = ksr.read_reason_for_workspace(ws)

    assert kb.archive_task(conn, task_id, signal_fn=fake_kill)
    conn.commit()

    assert seen.get("payload") and seen["payload"]["reason"] == "archived"
    events = _events(conn, task_id, "archived")
    assert events and events[-1]["payload"]["stop_reason"] == "archived"
    row = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert json.loads(row["metadata"] or "{}")["stop_reason"] == "archived"
    conn.close()


# ---------------------------------------------------------------------------
# Worker side: the dying worker names its own cause
# ---------------------------------------------------------------------------


def test_submitter_signal_handler_records_reason_and_exits(board, tmp_path, monkeypatch):
    """The SIGTERM handler reads the reason file, notes it on the card, and exits."""
    kwc = _load_worker_client()

    ws = tmp_path / "ws-worker"
    ws.mkdir()
    db_path = tmp_path / ".hermes" / "kanban.db"
    conn = kbc.connect()
    task_id, run_id = _live_claim(conn, ws)
    conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))

    ksr.write_reason_for_task(task_id, "limit_exceeded", workspace=ws,
                              detail="остановлен: лимит времени 60 с")

    # Drive the handler body directly: installing a real SIGTERM handler and signalling ourselves
    # would kill the test process.
    import contextlib as _ctx
    calls = []
    monkeypatch.setattr(os, "_exit", lambda code: calls.append(code))
    kwc._record_stopping("limit_exceeded", "остановлен: лимит времени 60 с", signal.SIGTERM)

    conn = kbc.connect()
    events = _events(conn, task_id, "worker_stopping")
    assert events, "the worker must leave a durable note naming its cause"
    assert events[-1]["payload"]["reason"] == "limit_exceeded"
    assert events[-1]["run_id"] == run_id
    conn.close()


def test_submitter_signal_handler_stays_silent_for_a_stale_run(board, tmp_path, monkeypatch):
    """A note about an attempt the card already moved past must not be written."""
    kwc = _load_worker_client()

    ws = tmp_path / "ws-stale"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _live_claim(conn, ws)
    conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 999))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / ".hermes" / "kanban.db"))

    kwc._record_stopping("reassigned", "stale", signal.SIGTERM)

    conn = kbc.connect()
    assert _events(conn, task_id, "worker_stopping") == []
    conn.close()


# ---------------------------------------------------------------------------
# Gateway side: EOFError is replaced by the cause the stopper recorded
# ---------------------------------------------------------------------------


class _Policy:
    def __init__(self, payload):
        self.kanban_json = json.dumps(payload)


def test_record_worker_loss_names_the_recorded_cause(board, tmp_path):
    """Acceptance 1/2/3 at the gateway edge: no bare EOFError when a cause was recorded."""
    from gateway.session_kanban import record_worker_loss

    ws = tmp_path / "ws-gw"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _live_claim(conn, ws)
    conn.close()

    # A real stop path names the attempt it stops: without a run_id the record cannot be
    # attributed to this attempt and reads as STALE (see the stale-cause test below).
    ksr.write_reason_for_task(task_id, "archived", workspace=ws, detail="остановлен: карточка архивирована",
                              run_id=run_id)
    policy = _Policy({"task_id": task_id, "run_id": run_id, "claim_lock": "host-1-x",
                      "db": str(tmp_path / ".hermes" / "kanban.db"), "workspace": str(ws)})

    record_worker_loss(policy, "EOFError", pid=1234, bound=True)

    conn = kbc.connect()
    events = _events(conn, task_id, "worker_lost")
    assert events, "the loss must still be recorded"
    payload = events[-1]["payload"]
    assert payload["stop_reason"] == "archived"
    assert payload["reason"].startswith("archived:")
    assert payload["transport_error"] == "EOFError"
    conn.close()


def test_record_worker_loss_falls_back_to_unknown(board, tmp_path):
    """Acceptance 4: a SIGKILL leaves no file, so the card says unknown and nothing else changes."""
    from gateway.session_kanban import record_worker_loss

    ws = tmp_path / "ws-gw-unknown"
    ws.mkdir()
    conn = kbc.connect()
    task_id, run_id = _live_claim(conn, ws)
    conn.close()

    policy = _Policy({"task_id": task_id, "run_id": run_id, "claim_lock": "host-1-x",
                      "db": str(tmp_path / ".hermes" / "kanban.db"), "workspace": str(ws)})

    record_worker_loss(policy, "EOFError", pid=1234, bound=True)

    conn = kbc.connect()
    events = _events(conn, task_id, "worker_lost")
    payload = events[-1]["payload"]
    assert "stop_reason" not in payload
    assert payload["reason"] == "unknown (EOFError)"
    conn.close()


def test_gateway_restart_notes_its_cause(board, tmp_path):
    """The gateway stop path records its cause for the card before the Stop.

    ``stop_authority_turns`` signals workers cooperatively, but a worker can still die before
    writing anything, so the gateway writes the cause itself first.
    """
    from gateway.run_runtime import _note_stop_reason

    ws = tmp_path / "ws-gw-stop"
    ws.mkdir()

    class _Worker:
        policy = _Policy({"task_id": "t_whatever", "workspace": str(ws)})

    _note_stop_reason(_Worker(), "gateway restart (drain)")

    payload = ksr.read_reason_for_workspace(ws)
    assert payload is not None and payload["reason"] == "shutdown"
    assert "останов шлюза" in payload["detail"]


def test_gateway_stop_skips_a_non_kanban_worker(board, tmp_path):
    """A plain CLI turn has no card and no workspace: nothing is written, nothing raises."""
    from gateway.run_runtime import _note_stop_reason

    class _Worker:
        policy = None

    _note_stop_reason(_Worker(), "gateway restart")
