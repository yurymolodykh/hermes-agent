"""Auto-continue / auto-split policy for a card that exhausted its retry counter.

The dispatcher's circuit breaker used to park every card that reached
``kanban.failure_limit`` in ``blocked`` and wait for a human. That is the wrong
trade when the cause is *internal to the card* — an exhausted iteration budget,
``max_runtime_seconds``, a crashed worker with no external cause: the work is
still owed, it just did not fit in one worker's run. This module turns that trip
into forward motion:

* generation 0, no continuation yet -> back to ``ready`` and continue
  (event ``auto_continued``);
* a second trip in the same generation -> split into child cards through the
  existing decomposer (event ``auto_split`` with the child ids);
* ``kanban.auto_continue_max_splits`` generations used up -> ``blocked`` with
  ``block_kind='needs_input'`` and reason ``auto_continue_exhausted``, so the
  human sees that automation reached its limit instead of silence;
* decomposer unavailable -> the card continues anyway (never parked, never
  loses what it already did).

The trip decision is made by :func:`plan_after_failure` inside the breaker's own
write transaction (no LLM call there). The split itself is an auxiliary LLM call,
so :func:`run_split_phase` performs it from the dispatcher tick, outside any
transaction, before the ready lane spawns.

State lives in ``task_events`` only — no schema change: ``auto_split`` counts the
generation, ``auto_continued`` after the last ``auto_split`` counts this
generation's continuations, and ``auto_split_queued`` marks a card whose split
the next tick must perform (replacing a flag column that a crash could strand).

Causes that are NOT the card's own (``rate_limited``, ``host_interrupted``,
``owner_lost``, ``infrastructure``) never reach the breaker at all; the
terminal-provider and systemic-wave trips carry ``force_trip``/``sticky`` and
keep their existing sticky behaviour — they are not a pure counter trip.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


def _json_dict(value) -> dict:
    """Parse a JSON payload column; ``{}`` for NULL/garbage/unexpected types."""
    from hermes_cli.kanban_db import _json_dict as _impl
    return _impl(value)


DEFAULT_AUTO_CONTINUE = True
DEFAULT_AUTO_CONTINUE_MAX_SPLITS = 2

# Outcomes whose trip is a card-internal budget/worker condition.
_ELIGIBLE_OUTCOMES = frozenset({"timed_out", "crashed", "reclaimed"})

# Reason stamped on the block when every split generation is spent.
EXHAUSTED_REASON = "auto_continue_exhausted"

# Events that carry the automation's own state for one card.
_STATE_EVENTS = ("auto_continued", "auto_split", "auto_split_queued", "unblocked")


@dataclass(frozen=True)
class AutoContinueSettings:
    """``kanban.auto_continue`` / ``kanban.auto_continue_max_splits``."""

    enabled: bool = DEFAULT_AUTO_CONTINUE
    max_splits: int = DEFAULT_AUTO_CONTINUE_MAX_SPLITS


def resolve_settings() -> AutoContinueSettings:
    """Read the policy from config; documented defaults when it cannot be read.

    Fails to the *documented* default rather than to "off": the owner's rule is
    that a card must not park on a counter alone, and an unreadable config is not
    evidence that the operator disabled the feature. A malformed value is warned
    about and replaced individually.
    """
    try:
        from hermes_cli.config import load_config_readonly
        kanban_cfg = (load_config_readonly() or {}).get("kanban", {})
    except Exception:
        logger.debug("kanban auto-continue: config unavailable; using defaults", exc_info=True)
        return AutoContinueSettings()
    if not isinstance(kanban_cfg, dict):
        return AutoContinueSettings()

    enabled = kanban_cfg.get("auto_continue", DEFAULT_AUTO_CONTINUE)
    if not isinstance(enabled, bool):
        logger.warning(
            "kanban auto-continue: kanban.auto_continue=%r is not a bool; using %s",
            enabled, DEFAULT_AUTO_CONTINUE,
        )
        enabled = DEFAULT_AUTO_CONTINUE

    raw_splits = kanban_cfg.get("auto_continue_max_splits", DEFAULT_AUTO_CONTINUE_MAX_SPLITS)
    try:
        max_splits = int(raw_splits)
    except (TypeError, ValueError):
        logger.warning(
            "kanban auto-continue: kanban.auto_continue_max_splits=%r is not an int; using %d",
            raw_splits, DEFAULT_AUTO_CONTINUE_MAX_SPLITS,
        )
        max_splits = DEFAULT_AUTO_CONTINUE_MAX_SPLITS
    if max_splits < 0:
        logger.warning(
            "kanban auto-continue: kanban.auto_continue_max_splits=%r is negative; using 0",
            raw_splits,
        )
        max_splits = 0
    return AutoContinueSettings(enabled=bool(enabled), max_splits=max_splits)


def ineligible_reason(outcome: str, extra: Optional[dict]) -> Optional[str]:
    """Why this trip is NOT a pure card-internal counter trip (None = eligible)."""
    extra = extra or {}
    if extra.get("terminal_provider"):
        # A revoked credential or a missing model: continuing or splitting all
        # hit the same wall. Keep the sticky block.
        return "terminal_provider"
    if extra.get("sticky"):
        # A systemic same-error wave (many identical fingerprints this tick) is
        # a host/environment condition; automation must not fan it out.
        return "systemic"
    if extra.get("infrastructure"):
        return "infrastructure"
    if outcome not in _ELIGIBLE_OUTCOMES:
        # ``spawn_failed`` (workspace resolution, host refused the spawn) and
        # ``blocked`` are not the card's own budget: neither continuing nor
        # splitting can help a host that will not start the worker.
        return f"outcome:{outcome}"
    return None


@dataclass
class FailurePlan:
    """What the breaker does with a tripped card.

    ``mode``: ``"block"`` (pre-breaker behaviour), ``"continue"`` (back to the
    source phase, event ``auto_continued``) or ``"split"`` (stay in the source
    phase, event ``auto_split_queued``; the dispatcher tick performs the split).
    """

    mode: str
    reason: str = ""
    payload: dict = field(default_factory=dict)


def plan_after_failure(
    *,
    outcome: str,
    extra: Optional[dict],
    failures: int,
    effective_limit: int,
    limit_source: str,
    generation: int,
    continuations: int,
    split_failures: int = 0,
    settings: Optional[AutoContinueSettings] = None,
) -> FailurePlan:
    """Decide block / continue / split for a tripped card (pure, no I/O)."""
    settings = settings or resolve_settings()
    if not settings.enabled:
        return FailurePlan("block", "auto_continue_disabled")

    blocked_cause = ineligible_reason(outcome, extra)
    if blocked_cause is not None:
        return FailurePlan("block", blocked_cause)

    base = {
        "trigger_outcome": outcome,
        "failures": failures,
        "effective_limit": effective_limit,
        "limit_source": limit_source,
        "generation": generation,
        "max_splits": settings.max_splits,
    }
    if continuations == 0:
        # First burn of this generation: the card gets its own extra run before
        # anything is subdivided. ``consecutive_failures`` stays at the limit, so
        # the NEXT burn lands in the branches below.
        return FailurePlan(
            "continue",
            "counter_exhausted",
            {**base, "attempt": continuations + 1},
        )
    if generation >= settings.max_splits:
        # A continuation in this generation burned AND every split generation is
        # spent: surface it to a human explicitly instead of looping.
        return FailurePlan(
            "block",
            EXHAUSTED_REASON,
            {**base, "exhausted": True, "attempts_continued": continuations},
        )
    if split_failures >= max(settings.max_splits, 1):
        # The card kept working because the decomposer could not be reached, once
        # per split attempt. Keeping it moving was right (a flaky aux call is not
        # the card's fault), but a permanent outage must not become an unbounded
        # retry loop: the split budget is spent, so a human is told why.
        return FailurePlan(
            "block",
            EXHAUSTED_REASON,
            {
                **base,
                "exhausted": True,
                "attempts_continued": continuations,
                "decomposer_unavailable": True,
                "split_failures": split_failures,
            },
        )
    # A continuation burned in this generation: the work is objectively bigger
    # than one worker's run, so subdivide it.
    return FailurePlan("split", "repeat_after_continue", dict(base))


# ---------------------------------------------------------------------------
# Event-derived state (no schema change)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoState:
    """This card's automation progress, read from its events."""

    generation: int = 0
    continuations: int = 0
    split_pending: bool = False
    split_failures: int = 0


def read_state(conn: sqlite3.Connection, task_id: str) -> AutoState:
    """Generation / continuations / pending split for ``task_id`` from its events.

    One pass, newest first, so the query cost stays flat as the log grows: the
    first ``auto_split`` closes the generation count, every newer
    ``auto_continued`` is a continuation *in the current generation*, and the
    newest state event decides whether a split is still owed. An ``unblocked``
    by an operator wipes the slate (a human stepping in restarts the automation
    budget, exactly as it resets ``consecutive_failures``).

    ``split_failures`` counts continuations whose reason is ``split_unavailable``:
    the card kept working because the decomposer could not be reached. That path
    must keep the card moving (a flaky aux call is not the card's fault) but it
    is still a loop, so it is bounded by the same split budget.
    """
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN (?, ?, ?, ?) "
        "ORDER BY id DESC",
        (task_id, *_STATE_EVENTS),
    ).fetchall()
    generation = 0
    continuations = 0
    split_failures = 0
    split_pending = False
    count_continuations = True
    for idx, row in enumerate(rows):
        kind = row["kind"]
        if kind == "unblocked":
            break
        if kind == "auto_split":
            generation += 1
            # Older continuations belong to the previous generation.
            count_continuations = False
        elif kind == "auto_continued":
            if count_continuations:
                continuations += 1
                if _json_dict(row["payload"]).get("reason") == "split_unavailable":
                    split_failures += 1
        elif kind == "auto_split_queued" and idx == 0:
            split_pending = True
    return AutoState(
        generation=generation, continuations=continuations,
        split_pending=split_pending, split_failures=split_failures,
    )


def plan_for_trip(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    extra: Optional[dict],
    failures: int,
    effective_limit: int,
    limit_source: str,
    force_trip: bool,
    retry_status: str = "ready",
) -> FailurePlan:
    """Plan for a card that just reached its failure limit (reads its events)."""
    if force_trip:
        # The caller applied its own bounded policy (protocol-violation streak,
        # terminal provider, systemic wave): that block is a human decision, and
        # neither continuing nor splitting can substitute for it.
        return FailurePlan("block", "force_trip")
    state = read_state(conn, task_id)
    plan = plan_after_failure(
        outcome=outcome,
        extra=extra,
        failures=failures,
        effective_limit=effective_limit,
        limit_source=limit_source,
        generation=state.generation,
        continuations=state.continuations,
        split_failures=state.split_failures,
    )
    if plan.mode == "split" and retry_status != "ready":
        # A review run failed: the work product exists and only its verdict is
        # outstanding. There is nothing to subdivide, so continue the review.
        return FailurePlan("continue", "review_lane", dict(plan.payload))
    return plan


def _restore_claim_state(
    conn: sqlite3.Connection, task_id: str, *, retry_status: str, error: str,
    failures: int, release_claim: bool,
) -> None:
    """Put the card back into its work lane, keeping the failure evidence."""
    if release_claim:
        conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
            "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status = 'running'",
            (retry_status, failures, error, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET consecutive_failures = ?, last_failure_error = ? WHERE id = ?",
            (failures, error, task_id),
        )


def commit_continue(
    conn: sqlite3.Connection, task_id: str, plan: FailurePlan, *,
    retry_status: str, error: str, failures: int, release_claim: bool,
) -> None:
    """Return the card to its work lane instead of blocking it.

    Runs inside the breaker's own write transaction. ``consecutive_failures``
    deliberately STAYS at the limit: the next failure is then a second trip in
    this generation, which is exactly the signal that the work does not fit one
    worker run (-> split). Resetting it here would hide that signal behind
    another ``failure_limit`` attempts.
    """
    from hermes_cli import kanban_db as _kb
    _restore_claim_state(
        conn, task_id, retry_status=retry_status, error=error,
        failures=failures, release_claim=release_claim,
    )
    kind = "auto_split_queued" if plan.mode == "split" else "auto_continued"
    if kind == "auto_split_queued" and read_state(conn, task_id).split_pending:
        # A split is already owed for this generation and the tick has not
        # performed it yet (slow aux call, gateway restart): one queued split is
        # the whole decision, so a second failure must not queue another.
        return
    payload = {"reason": plan.reason, "error": error[:300], **plan.payload}
    _kb._append_event(conn, task_id, kind, payload)


def commit_exhausted(
    conn: sqlite3.Connection, task_id: str, plan: FailurePlan, *,
    retry_status: str, error: str, failures: int, release_claim: bool,
) -> None:
    """Park the card for a human, typed, once every split generation is spent.

    ``block_kind='needs_input'`` makes the block sticky for ``recompute_ready``
    (a plain counter block would be promoted straight back and loop), and the
    reason ``auto_continue_exhausted`` says the *automation* reached its limit —
    not that the card hit an external wall.
    """
    from hermes_cli import kanban_db as _kb
    conn.execute(
        "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input', "
        "block_recurrences = COALESCE(block_recurrences, 0) + 1, "
        + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
           "worker_started_at = NULL, " if release_claim else "")
        + "consecutive_failures = ?, last_failure_error = ? "
        "WHERE id = ? AND status IN ('running', 'ready', 'review')",
        (failures, error, task_id),
    )
    _kb._append_event(
        conn, task_id, "blocked",
        {"kind": "needs_input", "reason": EXHAUSTED_REASON,
         "error": error[:300], "retry_status": retry_status, **plan.payload},
    )


def apply_trip(
    conn: sqlite3.Connection, task_id: str, plan: FailurePlan, *,
    outcome: str, error: str, failures: int, effective_limit: int, limit_source: str,
    retry_status: str, release_claim: bool, end_run: bool,
    extra: Optional[dict] = None, sticky: bool = False,
) -> bool:
    """Execute ``plan`` inside the breaker's write txn; ``True`` = the card is parked.

    Lives here, not in the dispatcher: ``kanban_db_dispatch.py`` is already over
    its file-length ratchet and may only shrink, and this is policy, not
    plumbing. Every branch still closes the failed attempt with its own outcome,
    so the run history never loses the reason the worker stopped.
    """
    from hermes_cli import kanban_db as _kb
    base = {
        "failures": failures,
        "trigger_outcome": outcome,
        "effective_limit": effective_limit,
        "limit_source": limit_source,
    }
    if plan.mode == "block" and plan.reason != EXHAUSTED_REASON:
        # The caller's own block (disabled, infrastructure, terminal provider,
        # force_trip): keep the legacy transition and its ``gave_up`` event.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
               "worker_started_at = NULL, " if release_claim else "")
            + "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review')",
            (failures, error, task_id),
        )
        payload = {**base, "error": error, "retry_status": retry_status,
                   "auto_continue_blocked": plan.reason}
        run_id = None
        if end_run:
            run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata={**base, "retry_status": retry_status},
            )
        if sticky:
            # The caller applied its own bounded policy, so the counter cannot
            # judge this block: ``recompute_ready`` holds it for an operator.
            payload["sticky"] = True
        if extra:
            payload.update(extra)
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=run_id)
        return True

    if plan.mode != "block":
        commit_continue(
            conn, task_id, plan, retry_status=retry_status, error=error,
            failures=failures, release_claim=release_claim,
        )
        if end_run:
            # The attempt itself is over: close it with its own outcome so the
            # run history still reads "timed out"/"crashed". No ``gave_up``: the
            # card was not given up on, it continues.
            _kb._end_run(
                conn, task_id, outcome=outcome, status=outcome, error=error,
                metadata={**base, "auto_continue": plan.mode,
                          "auto_continue_reason": plan.reason},
            )
        return False

    commit_exhausted(
        conn, task_id, plan, retry_status=retry_status, error=error,
        failures=failures, release_claim=release_claim,
    )
    if end_run:
        _kb._end_run(
            conn, task_id, outcome="blocked", status="blocked", error=error,
            metadata={**base, "auto_continue_reason": plan.reason},
        )
    return True


# ---------------------------------------------------------------------------
# Dispatcher-side split phase
# ---------------------------------------------------------------------------


def pending_split_ids(conn: sqlite3.Connection) -> list[str]:
    """Cards whose newest automation event is a queued, unfinished split."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'ready' AND id IN ("
        "  SELECT task_id FROM task_events WHERE kind = 'auto_split_queued'"
        ") ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows if read_state(conn, r["id"]).split_pending]


def _mark_split(conn: sqlite3.Connection, task_id: str, child_ids: list[str], generation: int) -> None:
    """Record a completed split (the generation counter lives in the event)."""
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        _kb._append_event(
            conn, task_id, "auto_split",
            {"child_ids": child_ids, "generation": generation, "at": int(time.time())},
        )


def _mark_split_unavailable(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    """The decomposer could not run: continue the card instead of parking it."""
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        _kb._append_event(
            conn, task_id, "auto_continued",
            {
                "reason": "split_unavailable",
                "detail": reason[:300],
                "attempt": read_state(conn, task_id).continuations + 1,
                "at": int(time.time()),
            },
        )


def run_split_phase(
    conn: sqlite3.Connection, *, per_tick: int = 1, decompose_fn=None,
) -> tuple[list[str], list[str]]:
    """Split up to ``per_tick`` pending cards; returns ``(split, failed)``.

    Runs OUTSIDE any write transaction: the decomposer is an auxiliary LLM call.
    On any failure the card is returned to work (``auto_continued`` carrying the
    reason) — an unavailable decomposer never parks a card and never discards
    work already done in its workspace.
    """
    settings = resolve_settings()
    if not settings.enabled:
        return [], []

    split: list[str] = []
    failed: list[str] = []
    for task_id in pending_split_ids(conn):
        if len(split) + len(failed) >= max(per_tick, 1):
            break
        generation = read_state(conn, task_id).generation + 1
        if decompose_fn is None:
            from hermes_cli import kanban_decompose as _decomp
            outcome = _decomp.decompose_failed_card(task_id, generation=generation)
        else:
            outcome = decompose_fn(task_id, generation=generation)
        ok = bool(getattr(outcome, "ok", False))
        child_ids = list(getattr(outcome, "child_ids", None) or [])
        if ok and child_ids:
            _mark_split(conn, task_id, child_ids, generation)
            split.append(task_id)
            logger.info(
                "kanban auto-continue: %s split into %d children (generation %d)",
                task_id, len(child_ids), generation,
            )
        else:
            _mark_split_unavailable(conn, task_id, str(getattr(outcome, "reason", "") or "no children"))
            failed.append(task_id)
            logger.info(
                "kanban auto-continue: %s could not be split (%s); continuing",
                task_id, getattr(outcome, "reason", "") or "no children",
            )
    return split, failed


__all__ = [
    "DEFAULT_AUTO_CONTINUE",
    "DEFAULT_AUTO_CONTINUE_MAX_SPLITS",
    "EXHAUSTED_REASON",
    "AutoContinueSettings",
    "AutoState",
    "FailurePlan",
    "ineligible_reason",
    "pending_split_ids",
    "plan_after_failure",
    "read_state",
    "resolve_settings",
    "run_split_phase",
]
