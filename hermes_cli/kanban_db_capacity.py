"""Host-level concurrency accounting for the kanban dispatcher: how many workers are already running.

``kanban.max_in_progress`` bounds the MACHINE, but each board's tick only sees its own DB, so a
derived cap of N would multiply by the number of active boards without the cross-board count here.
Every probe fails open (0): a broken board must not brick dispatch on healthy ones, so each catch-all
below logs the traceback and carries on.

Split out of ``hermes_cli.kanban_db_dispatch``; ``kanban_db`` is reached late-bound inside the
functions so monkeypatching ``kanban_db.list_boards`` keeps working.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'`` on this board."""
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        log.warning("kanban dispatch: cannot count running tasks, assuming none", exc_info=True)
        return 0


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board``.

    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every board to one file)
    yields 0. Fails open per board.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        current_path = None
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:  # the board registry is optional: no cap beats no dispatch
        log.warning("kanban dispatch: cannot enumerate boards for the host cap", exc_info=True)
        return 0
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if not path.exists():
                continue
            other = _kbc.connect(board=slug)
            try:
                total += count_running_tasks(other)
            finally:
                try:
                    other.close()
                except sqlite3.Error:
                    pass  # a broken handle on another board must not abort the count
        except Exception:  # one unreadable board is skipped, never fatal
            log.warning("kanban dispatch: skipping board %r in the host cap", slug, exc_info=True)
            continue
    return total
