"""Root-cause fixes for the 2026-10-10 gateway-restart incident (t_d9451f51).

Covers the two dispatcher defect classes with isolated DB copies:

A1 — restart victims auto-release. A ``running`` card claimed by a PREVIOUS
dispatcher (dead claimer pid in ``claim_lock`` / foreign
``gateway_generation``) whose Windows exit status is unknown must be
requeued to its source phase with NO needs_input question and NO failure
counter tick — mirroring the provider_unavailable auto-release pattern.

A2 — skill spawn preflight. A card whose force-loaded skills cannot resolve
in the assignee profile's context must be blocked BEFORE spawn, with the
skill name and candidate paths in the event / last_failure_error, instead of
spawning a worker that exit-1s on startup and crash-loops (t_9b53a6bc).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a kanban DB (mirrors test_generation_reclaim)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _connect(home) -> sqlite3.Connection:
    conn = sqlite3.connect(
        kb.kanban_db_path(),
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _running_restart_victim(
    conn,
    *,
    generation: "str | None",
    claimer_pid: int,
    worker_pid: int = 999999,
):
    """Create a task whose running claim belongs to a DEAD dispatcher.

    ``worker_pid`` defaults to a pid that is not alive on any host, so the
    liveness check in ``detect_crashed_workers`` fires deterministically.
    """
    tid = kb.create_task(conn, title="restart victim", assignee="alice")
    kb.claim_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET gateway_generation = ?, worker_pid = ?, "
        "claim_lock = ? WHERE id = ?",
        (
            generation,
            worker_pid,
            f"{kb.socket_hostname()}:{claimer_pid}",
            tid,
        ),
    )
    conn.commit()
    return tid


def _events(conn, tid, kind=None):
    q = "SELECT kind, payload FROM task_events WHERE task_id = ?"
    args: tuple = (tid,)
    if kind:
        q += " AND kind = ?"
        args = (tid, kind)
    rows = conn.execute(q + " ORDER BY id", args).fetchall()
    return [(r["kind"], json.loads(r["payload"] or "{}")) for r in rows]


# ---------------------------------------------------------------------------
# A1: restart victims auto-release without needs_input / failure counting
# ---------------------------------------------------------------------------


class TestRestartVictimAutoRelease:
    def test_dead_claimer_pid_null_generation_requeues_to_ready(
        self, kanban_home, monkeypatch
    ):
        """The 2026-10-10 00:04 incident shape: NULL gateway_generation (the
        old gateway predated the stamp) + claim_lock naming a dead pid."""
        conn = _connect(kanban_home)
        try:
            tid = _running_restart_victim(
                conn, generation=None, claimer_pid=808
            )
            before = conn.execute(
                "SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)
            ).fetchone()["consecutive_failures"]

            monkeypatch.setattr(kb, "_IS_WINDOWS", True)
            monkeypatch.setattr(
                kb, "_classify_worker_exit", lambda pid: ("unknown", None)
            )
            # Force the grace window closed so the liveness check runs.
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            reclaimed = kb.detect_crashed_workers(conn)

            row = conn.execute(
                "SELECT status, block_kind, claim_lock, worker_pid, "
                "consecutive_failures, last_failure_error FROM tasks "
                "WHERE id = ?",
                (tid,),
            ).fetchone()
            assert row["status"] == "ready", "restart victim must requeue"
            assert row["claim_lock"] is None
            assert row["worker_pid"] is None
            assert row["block_kind"] is None, "no needs_input stamp"
            assert (
                row["consecutive_failures"] == before == 0
            ), "failure counter must not tick"
            assert "restart" in (row["last_failure_error"] or "").lower() or (
                row["last_failure_error"] is None
            )
            kinds = [k for k, _ in _events(conn, tid)]
            assert "restart_reclaimed" in kinds
            assert "blocked" not in kinds, "no needs_input block event"
            assert tid not in reclaimed, "not a crash: restart requeue only"
            assert getattr(
                kb.detect_crashed_workers, "_last_restart_reclaimed", []
            ) == [tid]
        finally:
            conn.close()

    def test_foreign_generation_stamp_requeues_even_with_live_lock_pid(
        self, kanban_home, monkeypatch
    ):
        """Generation-based detection (NULL claim_lock fallback path): a
        foreign stamp is authoritative — the claiming process is gone."""
        conn = _connect(kanban_home)
        try:
            tid = _running_restart_victim(
                conn,
                generation="pid:808:start:1.0:boot:deadbeef",
                claimer_pid=999999,
            )
            monkeypatch.setattr(kb, "_IS_WINDOWS", True)
            monkeypatch.setattr(
                kb, "_classify_worker_exit", lambda pid: ("unknown", None)
            )
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            kb.detect_crashed_workers(conn)
            row = conn.execute(
                "SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            assert row["status"] == "ready"
            assert row["block_kind"] is None
        finally:
            conn.close()

    def test_own_generation_and_pid_still_asks_human(
        self, kanban_home, monkeypatch
    ):
        """Regression guard: when the dispatcher has NOT changed (same pid,
        same generation), the conservative needs_input question stays."""
        conn = _connect(kanban_home)
        try:
            tid = _running_restart_victim(
                conn,
                generation=kb.current_gateway_generation(),
                claimer_pid=os.getpid(),
            )
            monkeypatch.setattr(kb, "_IS_WINDOWS", True)
            monkeypatch.setattr(
                kb, "_classify_worker_exit", lambda pid: ("unknown", None)
            )
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            kb.detect_crashed_workers(conn)
            row = conn.execute(
                "SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            assert row["status"] == "blocked"
            assert row["block_kind"] == "needs_input"
            kinds = [k for k, _ in _events(conn, tid)]
            assert "restart_reclaimed" not in kinds
        finally:
            conn.close()

    def test_live_worker_never_touched_even_with_foreign_generation(
        self, kanban_home, monkeypatch
    ):
        """A live worker pid is sacrosanct: foreign generation alone must
        not requeue a card whose worker is doing real work."""
        conn = _connect(kanban_home)
        try:
            tid = _running_restart_victim(
                conn,
                generation="pid:808:start:1.0:boot:deadbeef",
                claimer_pid=999999,
                worker_pid=os.getpid(),
            )
            monkeypatch.setattr(kb, "_IS_WINDOWS", True)
            monkeypatch.setattr(
                kb, "_classify_worker_exit", lambda pid: ("unknown", None)
            )
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            kb.detect_crashed_workers(conn)
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            assert row["status"] == "running", "live worker must be untouched"
        finally:
            conn.close()
