"""OPS-15 (second root): the RESTART CAUSE must reach the child that writes the card's result.

Incident 18.11 20:01 had a second, independent defect under the one already fixed. The owner
recorded ``worker.stop_cause`` only on its OWN ``ManagedWorker`` object and then sent the child a
bare ``{'type': 'stop'}``. The child is the process that writes the durable ``worker_result`` the
crash sweep reads, so that event carried ``exit_code: 130`` and NO ``interrupt_message``: every
card a restart touched was booked as a plain crash, spent its failure budget and went ``blocked``.
A live drill against the real gateway (3 running cards, SIGUSR1, relaunch) reproduced it exactly —
0 of 3 cards survived — before this fix.

Pinned here:
1. ``ManagedWorker.control`` puts the cause on the frame the child receives;
2. ``WorkerControls`` hands that cause to the agent's interrupt, so the turn settles with it;
3. a user's Stop (no cause) stays a plain interrupt — it must keep counting against the card;
4. the booking half: a ``worker_result`` carrying the cause is requeued, not charged.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

RESTART = "Gateway restarting"

# A child that echoes every control frame it reads back on stdout, so the frame itself is asserted
# (not our intent to send one).
ECHO = """
import json, sys
for line in sys.stdin.buffer:
    frame = json.loads(line)
    if frame.get('type') == 'finish':
        break
    sys.stdout.buffer.write(json.dumps({'type': 'delta', 'text': json.dumps(frame)}).encode() + b'\\n')
    sys.stdout.buffer.flush()
"""


# --- 1. the frame the CHILD receives carries the cause ---------------------------------------

@pytest.mark.platforms("linux")
def test_owner_cause_reaches_the_child_frame():
    from gateway.session_managed_worker import ManagedWorker

    child = subprocess.Popen([sys.executable, "-c", ECHO], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    worker = ManagedWorker(child)
    worker.writer.start()
    try:
        worker.stop_cause = RESTART          # what stop_authority_turns() sets
        worker.control({"type": "stop"})

        async def read_one():
            async with asyncio.timeout(10):
                return await worker.next_frame(None, ack=5)

        frame = asyncio.run(read_one())
    finally:
        worker.close()
    assert frame["type"] == "delta"
    sent = json.loads(frame["text"])
    assert sent == {"type": "stop", "cause": RESTART}, sent


@pytest.mark.platforms("linux")
def test_a_users_stop_sends_no_cause():
    """The user's own Stop must stay indistinguishable from one: it keeps charging the card."""
    from gateway.session_managed_worker import ManagedWorker

    child = subprocess.Popen([sys.executable, "-c", ECHO], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    worker = ManagedWorker(child)
    worker.writer.start()
    try:
        worker.control({"type": "stop"})     # no stop_cause: a user stopped this turn

        async def read_one():
            async with asyncio.timeout(10):
                return await worker.next_frame(None, ack=5)

        frame = asyncio.run(read_one())
    finally:
        worker.close()
    assert json.loads(frame["text"]) == {"type": "stop"}


# --- 2. the child hands the cause to the agent's interrupt ------------------------------------

class _RecordingAgent:
    def __init__(self):
        self.messages = []

    def hard_interrupt(self, message=None, *, tool_reason=None):
        self.messages.append(message)


def _controls_with(agent):
    from agent.managed_worker import WorkerControls
    controls = WorkerControls(channel=None, route=None)
    controls.agent = agent
    return controls


def test_child_stop_carries_the_cause_into_the_interrupt():
    agent = _RecordingAgent()
    controls = _controls_with(agent)
    controls.stop(RESTART)
    assert agent.messages == [RESTART], agent.messages
    assert controls.stopped.is_set()


def test_child_stop_without_a_cause_interrupts_plainly():
    agent = _RecordingAgent()
    controls = _controls_with(agent)
    controls.stop(None)
    assert agent.messages == [None], agent.messages


# --- 3. the booking half ----------------------------------------------------------------------

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]


def _absent_pid(base: int = 3_950_000) -> int:
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
    yield conn
    conn.close()


def _card_with_result(conn, *, interrupt_message: str | None, exit_code: int = 130) -> str:
    """A running card whose current run recorded the owner-side ``worker_result``."""
    tid = "t_ops15cause"
    worker_pid, lock_pid = _absent_pid(), _absent_pid(base=3_960_000)
    claim = f"{HOST}:{lock_pid}"
    run = int(conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'default', 'running', ?, ?, ?, ?)",
        (tid, claim, NOW + 3600, worker_pid, NOW - 600)).lastrowid)
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, worker_pid, "
        "worker_started_at, current_run_id, started_at, priority, consecutive_failures, max_retries) "
        "VALUES (?, 'incident card', 'default', 'running', ?, ?, ?, ?, ?, ?, ?, 5, 0, 3)",
        (tid, NOW - 86400, claim, NOW + 3600, worker_pid, f"{claim}:1|1", run, NOW - 600))
    conn.commit()
    result = {"pid": worker_pid, "claim_lock": claim, "exit_code": exit_code, "last_output": ""}
    if interrupt_message:
        result["interrupt_message"] = interrupt_message
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                 (tid, run, "worker_result", json.dumps(result), NOW))
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                 (tid, run, "worker_bound", json.dumps({"pid": worker_pid, "claim_lock": claim}), NOW))
    conn.commit()
    return tid


def test_restart_caused_result_is_requeued_without_charging_the_card(board):
    tid = _card_with_result(board, interrupt_message=RESTART)
    assert tid in (kbd.detect_crashed_workers(board) or []), "the card must be reclaimed"
    row = board.execute("SELECT status, consecutive_failures FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row[0] == "ready", row
    assert row[1] == 0, "a host-caused interruption must not spend the card's failure budget"
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) == []


def test_same_exit_without_a_cause_still_charges_the_card(board):
    """Control: the SAME exit code with no cause is a real crash and must still be booked."""
    tid = _card_with_result(board, interrupt_message=None)
    assert tid in (kbd.detect_crashed_workers(board) or [])
    assert board.execute("SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)).fetchone()[0] > 0

