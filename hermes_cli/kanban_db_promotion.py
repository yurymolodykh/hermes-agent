"""Event-derived promotion decisions: sticky blocks and the resume status.

Split out of the ``kanban_db`` facade (which may only shrink) because these three
readers are pure event logic used by ``recompute_ready`` and the promotion paths,
with no write of their own.

The facade re-exports them; callers keep using ``kanban_db._has_sticky_block`` and
friends, and this module late-imports the facade's micro-helpers to avoid a cycle.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional


def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the newest ``blocked``/``unblocked``/``gave_up`` event says the
    block must wait for an operator: an explicit ``kanban_block`` (#28712), or a
    breaker trip ``_record_task_failure`` stamped ``sticky`` — the clean-exit
    protocol-violation budget or a systemic same-error wave. Those trip on a
    policy independent of ``consecutive_failures``, so ``recompute_ready``'s
    counter check cannot see them — without this the trip is promoted back to
    ``ready`` in the same tick and the card respawns forever. A plain
    (unified-budget) ``gave_up`` carries no marker and is judged by the counter,
    so raising ``failure_limit`` or ``assign_task`` to a fresh profile still
    releases it; a task with no such event at all (direct DB edit) auto-recovers.
    """
    from hermes_cli.kanban_db import _json_dict

    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row and row["kind"] == "blocked":
        return True
    trip = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'gave_up' AND id > COALESCE("
        "  (SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'unblocked'), 0) "
        "ORDER BY id DESC LIMIT 1", (task_id, task_id),
    ).fetchone()
    return bool(trip) and bool(_json_dict(trip["payload"]).get("sticky"))


def _latest_event(
    conn: sqlite3.Connection, task_id: str, kind: str, run_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """Newest ``task_events`` row of ``kind`` (optionally scoped to one run)."""
    sql = "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?"
    params: tuple[Any, ...] = (task_id, kind)
    if run_id is not None:
        sql += " AND run_id = ?"
        params = (*params, int(run_id))
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def _resume_status_from_events(conn: sqlite3.Connection, task_id: str) -> str:
    """``review`` when the newest lifecycle event carries a review
    ``resume_status``/``retry_status``/``source_status``, else ``ready`` (legacy)."""
    from hermes_cli.kanban_db import _json_dict, _row_get

    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind IN ("
        "'blocked', 'block_loop_detected', 'dependency_wait', 'gave_up', "
        "'unblocked', 'changes_requested', 'review_reopened', 'status', 'reclaimed', "
        "'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited', 'interrupted'"
        ") ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload"))
    for key in ("resume_status", "retry_status", "source_status"):
        if payload.get(key) == "review":
            return "review"
    return "ready"
