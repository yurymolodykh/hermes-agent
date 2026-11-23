"""Retention sweeps behind ``hermes kanban gc``: stale task events and worker logs.

Split out of ``kanban_db`` (the facade) so the file can keep shrinking while
the sweeps grow; ``kanban_db`` tail-imports these names, which is how every
other topical sibling in this package is wired.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

from hermes_cli.kanban_db_connect import write_txn
from utils import unlink_files_older_than


def _retention_seconds(older_than_seconds: int) -> int:
    """Normalise a gc retention window, rejecting negatives.

    Shared by both gc sweeps: a negative window puts the cutoff in the future,
    so "older than cutoff" would match every row / file instead of none —
    refuse before any sweep runs.
    """
    older_than_seconds = int(older_than_seconds)
    if older_than_seconds < 0:
        raise ValueError(
            f"older_than_seconds must be >= 0, got {older_than_seconds!r}: "
            "a negative retention selects everything."
        )
    return older_than_seconds


def gc_events(conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600) -> int:
    """Prune old done/archived events, retaining decomposition identity until task deletion.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--event-retention-days 0`` to "disabled" before calling this.

    ``workspace_cleanup_preserved_unsaved_work`` is retained too: it names a
    directory that is still on disk, so pruning it would strand the directory
    with no provenance in the DB at all (verdict t_896b6548, Д3). The durable
    registry (``kanban_preserved_workspaces``) holds the pointer regardless,
    and ``kanban gc`` drops both once the directory itself is gone — that is
    what bounds the exception.
    """
    from hermes_cli import kanban_preserved_workspaces as kpw

    cutoff = int(time.time()) - _retention_seconds(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND kind != 'decomposed' "
            "AND kind != ? AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))",
            (cutoff, kpw.PRESERVED_KIND),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(*, older_than_seconds: int = 30 * 24 * 3600, board: Optional[str] = None) -> int:
    """Delete worker log files older than the cutoff on one board; returns the count.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--log-retention-days 0`` to "disabled" before calling this.
    """
    from hermes_cli.kanban_db import worker_logs_dir

    older_than_seconds = _retention_seconds(older_than_seconds)
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    return unlink_files_older_than(log_dir, "*", older_than_seconds)
