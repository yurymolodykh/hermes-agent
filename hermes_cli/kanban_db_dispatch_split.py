"""The auto-split phase as a step that never runs under the dispatcher's lock.

``run_split_phase`` calls the decomposer — an aux LLM request of up to 180 s,
retried up to 3 times. It used to run inside ``_dispatch_once_locked``, i.e. under
the board's single-writer ``.dispatch.lock``, so while one card was decomposing
the whole board stopped: no reclaim of dead workers, no promotion, no spawn. The
reviewer measured the consequence directly — a two-process probe where the second
dispatcher got ``skipped_locked=True``, and a live journal showing
``auto_split_queued`` -> ``auto_split`` 616.8 s apart with no ``spawned`` /
``promoted`` / ``claimed`` between, on a board with ``dispatch_interval_seconds: 60``.

This module owns the tick's outer shell so the phase cannot drift back inside the
critical section: it splits first (no lock), then takes the dispatch lock for the
reclaim/promote/spawn pass. Splitting *first* rather than afterwards is
behavioural, not stylistic — a split moves the burned card to ``todo`` and its
children to ``ready``, which is exactly what the following spawn pass must pick
up; running it afterwards left the card claimed by the spawn loop and therefore
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
from hermes_cli import kanban_db as _kb
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


def run_tick(
    conn: sqlite3.Connection,
    board: Optional[str],
    *,
    locked_tick=None,
    skipped_result=None,
    **tick_kwargs,
):
    """Split outside the lock, then take it for one ``_dispatch_once_locked`` pass.

    ``tick_kwargs`` go to ``locked_tick`` verbatim; ``dry_run`` is read from them
    so the split phase and the tick can never disagree about it. A contended lock
    yields ``DispatchResult(skipped_locked=True)`` and writes nothing, as before —
    the split has already happened, which is intentional: it needs no dispatch
    lock and its own lock keeps it single.

    ``locked_tick``/``skipped_result`` are injected by ``dispatch_once`` rather
    than re-imported here: this module sits below ``kanban_db_dispatch`` and a
    late ``from ... import`` rebuilds a SECOND module object whenever the caller
    dropped ``hermes_cli.kanban_db*`` from ``sys.modules`` (the documented test
    fixture pattern). The tick would then run the twin module's function, so a
    monkeypatched seam — and every ``DispatchResult`` field the caller reads —
    would silently belong to a different object.
    """
    if locked_tick is None or skipped_result is None:  # pragma: no cover - callers pass both
        from hermes_cli.kanban_db_dispatch import DispatchResult, _dispatch_once_locked
        locked_tick = _dispatch_once_locked if locked_tick is None else locked_tick
        skipped_result = DispatchResult if skipped_result is None else skipped_result

    dry_run = bool(tick_kwargs.get("dry_run"))
    try:
        db_path: Optional[Path] = _kb.kanban_db_path(board=board)
    # An unresolvable board path must not lose the tick, and the resolver's failure
    # modes are profile/HOME dependent; the pre-existing behaviour caught broadly too.
    except Exception:  # health: allow BLE001 -- never lose a tick to a path lookup
        db_path = None
    split = run_split_before_tick(conn, db_path, dry_run=dry_run)
    if db_path is None:
        result = locked_tick(conn, **tick_kwargs)
    else:
        with _kbc._dispatch_tick_lock(db_path) as held:
            if not held:
                result = skipped_result(skipped_locked=True)
            else:
                result = locked_tick(conn, **tick_kwargs)
                _kbc._maybe_checkpoint_wal(conn, db_path)  # still under the lock
    if split is not None:
        result.auto_split, result.auto_split_failed = split
    return result
