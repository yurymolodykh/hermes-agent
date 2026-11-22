"""The key pool's exception types, apart from the pool itself.

Sibling of :mod:`agent.kanban_key_pool` (facade + siblings layout). Two reasons
they live here rather than in the facade:

* the facade is already over its ``FILE_LINES`` ratchet, so a class added to it
  may only be offset by a move — this is that move;
* the exit mapping in ``hermes_cli.turn_exit`` and the dispatcher's classification
  read these types without needing the reservation machinery, and a module with no
  imports cannot take part in a cycle.

All three are re-exported from ``agent.kanban_key_pool``, so every caller and test
keeps importing them there.

The split is load-bearing, not cosmetic. A :class:`KeyPoolWait` means *the resource
is momentarily unavailable and the same attempt would succeed later* — the card is
requeued without spending ``consecutive_failures``, exactly like a provider quota
wall. A plain :class:`KeyPoolError` means the pool is unusable for a reason a retry
cannot heal (not configured, not in force, a key the provider rejected), and it must
stay a crash: marking those transient would loop the card forever instead of
surfacing the misconfiguration.
"""

from __future__ import annotations


class KeyPoolError(RuntimeError):
    """A key-pool operation failed in a way the caller must handle."""


class KeyPoolWait(KeyPoolError):
    """A resource the pool arbitrates is busy right now — retrying later can succeed.

    Raised by the two waits the pool can enter: every card key occupied
    (:class:`KeyPoolSlotBusy`) and a state lock held past its timeout.
    """


class KeyPoolSlotBusy(KeyPoolWait):
    """Every card key is occupied NOW — a resource wait, not a work failure.

    Raised only by the exhaustion branch of ``reserve_for_card``: the card never
    started, so its attempt is requeued without spending ``consecutive_failures``
    (22.11.2026: a worker that failed at reservation still blocked the card).
    """
