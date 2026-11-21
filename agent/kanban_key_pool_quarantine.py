"""Key quarantine: record a provider rejection and take a bad key out of rotation.

Sibling of :mod:`agent.kanban_key_pool` (facade + siblings layout): the pool
module owns reservations, this one owns the ``failed_keys`` map and the
quarantine window that keeps a rejected credential from reaching the next card.

The two halves live apart because the pool facade is already over the
``FILE_LINES`` target; both are re-exported from it, so every caller and test
keeps using ``agent.kanban_key_pool.mark_key_failed`` /
``clear_quarantine``.

State helpers (``_state_lock`` / ``_read_state`` / ``_write_state`` /
``_key_slots`` / ``state_path`` / ``_quarantine_seconds`` / ``_STATUS_BAD``)
are imported from the facade INSIDE the functions: a module-level import would
be a cycle, and a late import also honours a monkeypatched facade attribute
(the plumbing-error tests rely on that).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def mark_key_failed(
    task_id: str,
    status_code: Optional[int],
    *,
    reason: str = "",
    pid: Optional[int] = None,
    key_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Record that the provider rejected this card's own key, then quarantine it.

    The card does **not** get a different key from here — that would be the
    silent mid-run hop the owner forbade. What happens instead:

    * the failure is written to the state file's ``failed_keys`` map, which is
      the audit trail (§4 of the report) and survives restarts;
    * the card's OWN slot is kept, flipped to ``bad`` (defect D1: deleting it
      freed the key for a second card while the first was still running on it,
      which at the production limit of one card per key inverted the
      "one card = one key" invariant). A ``bad`` slot still counts against the
      key's capacity, so the key stays unavailable until the card releases it
      (``release_for_task`` / ``release_for_pid``) or the PID/TTL backstop
      reaps it — and a *quarantine* keeps it out of the next reservation for
      new cards until it expires (or forever, for a dead 401/403 key);
    * this process's binding flips to ``bad``, so every further resolution in
      it raises instead of quietly working off some other key.

    **Who is allowed to report.** Any process that actually holds the key: the
    reserving owner (``pid == entry.pid``) or a sibling of the same card
    (``pid in entry.attached_pids``). The check is on the *pair* ``(task_id,
    pid)``, not on the pid alone, so a stale worker of a different card cannot
    poison someone else's key. Restricting it to the owner pid (the original
    implementation) meant an adopted helper — the process that may well be the
    one that saw the 401 — could not quarantine a key it was using, and the
    key came back for the next card. ``key_id`` is accepted so a caller that
    already knows which key failed does not depend on the scan order.

    Quarantine policy, chosen so neither extreme is silent:

    * ``401`` / ``403`` — the key is dead or revoked. Quarantined with no
      expiry (``quarantine_until: 0``): it stays out until the owner fixes the
      key file or runs ``hermes kanban keypool clear``.
    * ``429`` — rate limited, not dead. Quarantined for
      ``HERMES_KANBAN_KEYPOOL_QUARANTINE_SECONDS`` (default 900s = the
      documented limit window), then it becomes eligible again automatically.
    * anything else — treated like 401 but with the 429 default window, since a
      non-auth failure was not attributed to the credential by the classifier.
    """
    from agent.kanban_key_pool import (
        _STATUS_BAD,
        _key_slots,
        _quarantine_seconds,
        _read_state,
        _state_lock,
        _write_state,
        state_path,
    )

    task_id = (task_id or "").strip()
    if not task_id:
        return None
    caller_pid = int(pid if pid is not None else os.getpid())
    path = state_path()
    if path is None or not path.exists():
        return None
    now = time.time()
    marked: Optional[dict[str, Any]] = None
    with _state_lock(path):
        state = _read_state(path)
        reservations: dict[str, Any] = state["reservations"]
        for key_entry, entry in list(reservations.items()):
            if not isinstance(entry, dict):
                continue
            kid = int(entry.get("key_id") or key_entry)
            if key_id is not None and kid != int(key_id):
                continue
            victim: Optional[dict[str, Any]] = None
            for slot in _key_slots(entry):
                if slot.get("task_id") != task_id:
                    continue
                owner_pid = int(slot.get("pid") or 0)
                attached = slot.get("attached_pids") or []
                is_holder = caller_pid == owner_pid or (
                    isinstance(attached, list) and caller_pid in attached
                )
                if not is_holder:
                    continue
                victim = slot
                break
            if victim is None:
                continue
            owner_pid = int(victim.get("pid") or 0)
            failure = {
                "status_code": status_code,
                "reason": reason or f"http_{status_code}",
                "at": int(now),
                "task_id": task_id,
                "run_id": victim.get("run_id"),
                "pid": caller_pid,
                "owner_pid": owner_pid,
                "reported_by": "sibling" if caller_pid != owner_pid else "owner",
                "degradation": "card_kept_its_key_then_failed",
            }
            marked = {
                "key_id": kid,
                "key_label": entry.get("key_label"),
                **dict(victim),
            }
            marked["status"] = _STATUS_BAD
            marked["failure"] = failure

            quarantine_until = 0
            if status_code == 429:
                quarantine_until = int(now + _quarantine_seconds())
            elif status_code in (401, 403):
                quarantine_until = 0  # dead key: no automatic return
            else:
                quarantine_until = int(now + _quarantine_seconds())
            # Quarantine the KEY, not the slot (requirement 7): a rejected
            # credential is not safe for the second card on the same key, so
            # the whole key leaves circulation while the window lasts.
            state.setdefault("failed_keys", {})[str(kid)] = {
                **failure,
                "key_id": kid,
                "key_label": entry.get("key_label") or f"key-{kid}",
                "quarantine_until": quarantine_until,
            }
            # The failing card keeps its slot while its own process is alive.
            # Deleting it was defect D1: at the production limit of one card
            # per key the key has exactly one slot, so ``kept`` came out empty,
            # the whole reservation record vanished, and the moment
            # ``quarantine_until`` elapsed the key went back into circulation
            # and was handed to a second card while the first was still
            # running on it. Marking the slot ``bad`` keeps it counted as
            # occupied, so the key is not free for anyone else. The slot is
            # removed by the card's own release (``release_for_task`` from
            # ``_release_card_key`` / ``release_for_pid`` on exit) or, if the
            # process dies without releasing, by the PID/TTL backstop in
            # ``_sweep_locked``. The OTHER card on a shared key keeps its own
            # slot untouched.
            victim["status"] = _STATUS_BAD
            victim["quarantined_at"] = int(now)
            break
        if marked is not None:
            _write_state(path, state)
    if marked is not None:
        logger.error(
            "key-pool: key %s for task %s rejected by the provider (HTTP %s) — "
            "marked bad and quarantined; this run kept its key and was not "
            "re-keyed (release_reason=key_failed)",
            marked.get("key_id"),
            task_id,
            status_code,
        )
    return marked


def clear_quarantine(key_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Drop quarantine records (owner action: trusted a key again).

    ``key_id=None`` clears every record — the recovery step after re-issuing a
    previously dead key.
    """
    from agent.kanban_key_pool import (
        _read_state,
        _state_lock,
        _write_state,
        state_path,
    )

    path = state_path()
    if path is None or not path.exists():
        return []
    cleared: list[dict[str, Any]] = []
    with _state_lock(path):
        state = _read_state(path)
        failed: dict[str, Any] = state.get("failed_keys") or {}
        if key_id is None:
            cleared = list(failed.values())
            state["failed_keys"] = {}
        else:
            record = failed.pop(str(int(key_id)), None)
            if record is not None:
                cleared = [record]
            state["failed_keys"] = failed
        if cleared:
            _write_state(path, state)
    return cleared
