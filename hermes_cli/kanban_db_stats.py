"""Board-level statistics and per-task age metrics.

Split out of the ``kanban_db`` facade (which may only shrink): these are pure
readers over one connection — no writes, no promotion logic — so they live with
their own tests and keep the facade from growing when a new counter is added.

``by_status``, ``by_assignee`` and ``oldest_ready_age_seconds`` are the shape the
CLI, the dashboard plugin and the diagnostics already read; the scheduled-card
block (``scheduled_total`` / ``scheduled_no_deadline`` / ``scheduled_overdue`` /
``next_wakeups``) is additive and absent-key-safe, so an older caller that only
knows the first three keeps working.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional


def _kb():
    from hermes_cli import kanban_db

    return kanban_db


def counts_by_assignee(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{assignee: {status: n}}`` over non-archived tasks."""
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])
    return counts


def _scheduled_stats(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    """Parked cards: how many, how many have no deadline, how many are overdue."""
    from hermes_cli import kanban_db_schedule as kbsch

    total = 0
    no_deadline = 0
    overdue = 0
    for row in conn.execute(
        "SELECT id, scheduled_until FROM tasks WHERE status = 'scheduled'"
    ):
        total += 1
        epoch = _kb()._to_epoch(row["scheduled_until"])
        if epoch is None:
            # A card parked before the column existed still carries its deadline
            # in its own notes — that is a deadline, not an anomaly.
            epoch = kbsch.legacy_deadline_from_comments(conn, row["id"], now=now)
        if epoch is None:
            no_deadline += 1
        elif int(epoch) <= now:
            overdue += 1
    return {
        "scheduled_total": total,
        "scheduled_no_deadline": no_deadline,
        "scheduled_overdue": overdue,
        "next_wakeups": kbsch.pending_wakeups(conn),
    }


def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, the oldest ``ready`` age and parked deadlines."""
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee = counts_by_assignee(conn)

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
        **_scheduled_stats(conn, now),
    }


def task_age(task: Any) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c, _s, _co = task.created_at, task.started_at, task.completed_at  # from_row coerced
    return {
        "created_age_seconds": now - _c if _c is not None else None,
        "started_age_seconds": now - _s if _s is not None else None,
        "time_to_complete_seconds": _co - (_s or _c) if _co is not None else None,
    }


__all__: tuple[str, ...] = ("board_stats", "counts_by_assignee", "task_age")
_ = Optional  # keep the typing import meaningful for annotated callers
