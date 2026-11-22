"""The auto-split phase as a step that never runs under the dispatcher's lock.

``run_split_phase`` calls the decomposer — an aux LLM request of up to 180 s,
retried up to 3 times. It used to run inside ``_dispatch_once_locked``, i.e. under
the board's single-writer ``.dispatch.lock``, so while one card was decomposing
the whole board stopped: no reclaim of dead workers, no promotion, no spawn. The
reviewer measured the consequence directly — a two-process probe where the second
dispatcher got ``skipped_locked=True``, and a live journal showing
``auto_split_queued`` -> ``auto_split`` 616.8 s apart with no ``spawned`` /
``promoted`` / ``claimed`` between, on a board with ``dispatch_interval_seconds: 60``.

This module keeps the phase out of that critical section. It runs *before* the
tick rather than after it: a split moves the burned card to ``todo`` and its
children to ``ready``, which is exactly what the following spawn pass must pick
up — running it afterwards left the card claimed by the spawn loop and therefore
never split.

``.split.lock`` (see ``kanban_db_connect._dispatch_split_lock``) is a separate,
non-blocking flock: it keeps a sibling dispatcher from paying for the same
decomposition twice without serialising anyone's writes, and ``flock`` is released
by the kernel if the process dies, so a killed decomposer cannot wedge the board.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_auto_continue as _kac
from hermes_cli import kanban_db_connect as _kbc


def run_split_before_tick(
    conn: sqlite3.Connection, db_path: Optional[Path], *, dry_run: bool = False,
) -> Optional[tuple[list[str], list[str]]]:
    """Run the split phase with no dispatch lock held; ``None`` means it did not run.

    Returns ``(auto_split, auto_split_failed)`` when it ran, ``None`` for a dry
    run (which must never create children) and for the case where a sibling
    process already holds this board's split lock. At most one call per tick.
    """
    if dry_run:
        return None
    if db_path is None:
        return _kac.run_split_phase(conn)
    with _kbc._dispatch_split_lock(db_path) as split_held:
        if not split_held:
            return None
        return _kac.run_split_phase(conn)
