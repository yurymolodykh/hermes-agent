"""Where the dispatcher touches the per-card key pool.

Split out of ``hermes_cli.kanban_db_dispatch`` (file cap) and kept stdlib-only
at import time: ``agent.kanban_key_pool`` is resolved inside each call so a
no-pool install never pays for it and a broken pool can never break a tick.

The rule this module enforces, in one place: **a slot is released when the run
that held it is over, and only then.** Two forces pull against each other and
both are real:

* a live card must never share its account, so nothing here may drop a slot
  whose owner process is still alive (that is the whole point of the pool);
* a run that ended — ``timed_out``, ``crashed``, ``stale``, ``gave_up``,
  ``reclaimed`` — must not keep holding a slot, or the ready queue starves.

The second one cannot be served by the worker's own ``atexit`` release: every
death path above closes the run from the DISPATCHER process, and a worker that
was SIGKILLed never runs an exit hook at all. Before this module existed that
gap was real: two ``timed_out`` workers held 2 of 6 card slots as ``STALE``,
``hermes kanban dispatch`` answered ``spawned: []`` and the gateway logged
"ready queue non-empty for 36 consecutive ticks but 0 workers spawned" while
nothing was actually busy.

Liveness is the pool's own staleness rule (``_reservation_is_stale``: dead
owner and dead siblings ⇒ stale), so the release here can never be more
aggressive than the ephemeral sweep the operator runs by hand.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _pool_config():
    """The usable pool config, or ``None`` (unconfigured / not in force)."""
    from agent.kanban_key_pool import load_config

    cfg = load_config()
    return cfg if cfg is not None and cfg.usable else None


def keypool_has_free_slot(reserve: int = 0) -> bool:
    """True when at least ``reserve + 1`` card key slots are free right now.

    A kanban card reserves ONE API key for its whole run and may never fall
    back to the shared env key. When every card key is occupied, a worker
    spawned now dies at key resolution (``KeyPoolError``) and burns the retry
    budget until the card auto-blocks on a transient condition. The dispatcher
    therefore defers such a card instead of spawning it.

    Occupancy is counted from the per-SLOT reservation rows, and a stale slot
    does not count: a worker killed by ``max_runtime`` or a crash can sit as a
    corpse for the moments between the death sweep and the pool sweep, and
    counting it made the whole ready queue read as ``skipped_keypool_capped``
    while nothing was actually busy.

    ``reserve`` counts workers already spawned earlier in THIS tick whose child
    process has not written its reservation yet. Without it two cards spawned
    back-to-back both see the same free slot, the second child then dies at key
    resolution and the card burns a failure. Checking headroom against the
    in-tick tally closes that race without holding the pool lock across
    ``spawn()``.

    Fail-open: any probe error returns True so a broken pool cannot stop
    dispatch entirely, and no pool config at all returns True (no-op).
    """
    try:
        from agent.kanban_key_pool import pool_status

        if _pool_config() is None:
            return True
        status = pool_status() or {}
        default_limit = int(status.get("max_cards_per_key") or 1) or 1
        limits = {
            int(row.get("key_id") or 0): int(row.get("limit") or default_limit)
            for row in (status.get("keys") or [])
        }
        occupied: dict[int, int] = {}
        for slot in (status.get("reservations") or []):
            if slot.get("stale"):
                continue
            kid = int(slot.get("key_id") or 0)
            occupied[kid] = occupied.get(kid, 0) + 1
        free = sum(max(0, limit - occupied.get(kid, 0)) for kid, limit in limits.items())
        return free > int(reserve or 0)
    except Exception:  # health: allow BLE001 -- fail-open: a broken pool must never stop dispatch
        return True


def release_dead_card_key(task_id: str) -> list[dict[str, Any]]:
    """Free ``task_id``'s provably-dead key-pool slots (best-effort).

    Called from the transition that ended the run, so a killed worker's slot is
    gone the moment its card is no longer running rather than a TTL later.
    Only slots the pool itself calls stale go: a card whose worker is still
    alive keeps its key. Never raises — a bookkeeping error must not turn a
    successful reclaim into an error, and ``keypool_sweep`` is the backstop.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return []
    try:
        from agent.kanban_key_pool import release_dead_task_slots

        return release_dead_task_slots(task_id)
    except Exception as exc:  # health: allow BLE001 -- best-effort bookkeeping; the sweep is the backstop
        logger.debug("kanban key-pool release for %s failed: %s", task_id, exc)
        return []


def keypool_sweep() -> list[dict[str, Any]]:
    """Reap every dead key-pool reservation; never raises (no pool = no-op).

    Runs on each dispatcher tick after the death sweeps and BEFORE the spawn
    gate reads occupancy. It is the backstop for a reservation the per-card
    release cannot attribute to a card in this tick — a worker orphaned by a
    host restart, a run that died on another code path — so a corpse costs one
    tick at most, never a TTL.
    """
    try:
        from agent.kanban_key_pool import sweep

        if _pool_config() is None:
            return []
        return list(sweep() or [])
    except Exception as exc:  # health: allow BLE001 -- best-effort; a tick must not die on bookkeeping
        logger.debug("kanban key-pool sweep failed: %s", exc)
        return []
