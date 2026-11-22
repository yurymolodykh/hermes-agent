"""One-line summaries of a dispatcher tick: why it held ready work back.

Split out of ``hermes_cli.kanban_db_dispatch`` (which may only shrink) so the
tick's explanations live with their own tests. The results are duck-typed:
callers pass whatever ``dispatch_once`` returned, and a missing field reads as
"no hold", never as an error.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol


class _TickResult(Protocol):
    """The fields of ``DispatchResult`` this summary reads (duck-typed on purpose)."""

    respawn_guarded: list
    rate_limited: list
    skipped_keypool_capped: list
    skipped_env_unready: list
    skipped_locked: object
    memory_pressure: Optional[str]
    skipped_per_profile_capped: Optional[list]
    budget_hold: Optional[str]


def describe_suppression(results: Iterable[Optional[_TickResult]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. Feeds the "dispatcher stuck" warnings of the
    CLI daemon and the embedded gateway dispatcher, which otherwise report a
    bare zero-spawn count while ``hermes kanban tail`` is the only place the
    guard reason is written (#111910).
    """
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.skipped_keypool_capped:  # a key hold reads as "stuck" otherwise
            counts["keypool_capped"] = counts.get("keypool_capped", 0) + len(res.skipped_keypool_capped)
        per_profile = getattr(res, "skipped_per_profile_capped", None) or []
        if per_profile:  # same for an assignee at its own cap
            counts["per_profile_capped"] = counts.get("per_profile_capped", 0) + len(per_profile)
        if res.skipped_env_unready:  # a host condition, named so it is not read as a stall
            counts["env_unready"] = counts.get("env_unready", 0) + len(res.skipped_env_unready)
        budget_hold = getattr(res, "budget_hold", None)
        if budget_hold:  # a full host/per-board cap: saturation, not a stall
            counts[f"budget_hold:{budget_hold}"] = counts.get(f"budget_hold:{budget_hold}", 0) + 1
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)


__all__ = ["describe_suppression"]
