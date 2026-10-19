"""L1 — per-card and auxiliary reasoning pins (adaptive-reasoning-effort, W3).

Level 1 (see design/effort-policy-v1.md §2 rows 7/8/11): minimal-sufficient
reasoning_effort applied at *pinning* time, gated behind
``agent.reasoning_class_routing`` (DEFAULT OFF). While the master switch is
off, every public function in this module returns its "no route" value and the
running system is byte-identical to the base revision:

  * :func:`resolve_card_effort` -> None (no per-card pin applied)
  * :func:`resolve_aux_effort`  -> None (no aux-task pin applied)
  * :func:`reasoning_aux_pins`  -> {} (no pins consulted)

The per-card helper wraps ``tasks.reasoning_effort`` (kanban per-task thinking
depth) — it does NOT own the model override: model stays
deepseek-v4-flash@custom:letovo (pinned at card creation), only the effort
level may be lowered/raised.

Effort values are clamped to the letovo wire domain
``{none, low, medium, high, max}`` (agent.reasoning_effort.LETOVO_EFFORTS),
so a mis-typed pin can never reach the wire as a 400. ``medium`` stays
``medium`` and ``xhigh`` maps to ``max`` per the experiment-v4-verified
DeepSeek mapping (REPORT_v4.md; W1's "medium→high" assumption was
disproved — the wire distinguishes medium as its own step).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent.reasoning_effort import (
    LETOVO_EFFORTS,
    LETOVO_OVERRIDES,
    clamp_effort,
)

_log = logging.getLogger(__name__)

__all__ = [
    "LETOVO_DOMAIN",
    "clamp_to_letovo",
    "resolve_card_effort",
    "resolve_aux_effort",
    "reasoning_aux_pins",
    "pin_card_effort",
    "resolve_card_lane_effort",
    "apply_card_lane_effort",
]

#: The letovo wire domain repeated here as a cheap import-free tuple for
#: callers that only need the domain, not the full clamp machinery.
LETOVO_DOMAIN: tuple[str, ...] = LETOVO_EFFORTS

#: Profiles that act as the independent gate on a writing card. A card
#: assigned to one of these is a REVIEW card and pins ``max``; a writing
#: card gains ``medium`` when it owns a direct child card assigned to one of
#: these (design/medium-review-scheme-v1.md — «написание → medium, ревью → max»).
REVIEWER_PROFILES: frozenset[str] = frozenset(
    {"09-independent-reviewer", "15-sre-qa"}
)


def clamp_to_letovo(effort: Optional[str]) -> Optional[str]:
    """Clamp a requested effort onto the letovo wire domain.

    ``medium`` stays ``medium``; ``minimal`` -> ``low``; ``xhigh`` -> ``max``;
    ``ultra`` -> ``max`` (overrides from experiment v4's verified DeepSeek
    mapping). Unknown bespoke values pass through unchanged (custom provider
    may use its own names).
    """
    return clamp_effort(effort, LETOVO_EFFORTS, LETOVO_OVERRIDES)


def _agent_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve the agent config dict, loading live config when *cfg* is None.

    Matches the aux-task config seam (auxiliary_client._get_auxiliary_task_config)
    which reads the real config when the caller passes nothing — so the L1
    gating behaves identically in-process whether the caller already holds a
    config or expects the helper to read it.
    """
    if cfg is not None:
        return (cfg or {}).get("agent") or {}
    try:
        from hermes_cli.config import load_config_readonly

        loaded = load_config_readonly()
    except Exception:
        return {}
    return (loaded or {}).get("agent") or {}


def reasoning_class_routing_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Master switch for L1/L2 (agent.reasoning_class_routing, DEFAULT FALSE)."""
    from utils import is_truthy_value  # local import: no hard dep

    return is_truthy_value(_agent_cfg(cfg).get("reasoning_class_routing"), default=False)


def reasoning_aux_pins(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Resolve gated L1 aux-task pins, or {} when routing is disabled.

    Returns ``{aux_task: clamped_effort}`` for every entry in
    ``agent.reasoning_aux_pins``, each clamped to the letovo domain. Empty
    when the master switch is off (L1 inert) or the pin map is absent.
    """
    if not reasoning_class_routing_enabled(cfg):
        return {}
    agent_cfg = _agent_cfg(cfg)
    pins = agent_cfg.get("reasoning_aux_pins") or {}
    if not isinstance(pins, dict):
        _log.warning(
            "agent.reasoning_aux_pins is not a dict (%r); ignoring", type(pins).__name__
        )
        return {}
    out: Dict[str, str] = {}
    for task, effort in pins.items():
        if not isinstance(task, str) or not task:
            continue
        clamped = clamp_to_letovo(effort)
        # clamp_effort passes through bespoke/unknown names unchanged (custom
        # providers may use their own), but a Hermes aux pin must be one of
        # the recognized effort levels — anything else is a config typo and is
        # skipped rather than reaching the wire.
        level = str(effort or "").strip().lower()
        from agent.reasoning_effort import EFFORT_LADDER

        if level not in EFFORT_LADDER:
            _log.warning(
                "reasoning_aux_pins value for %r (%r) is not a recognized level; "
                "skipping pin", task, effort,
            )
            continue
        if clamped is None:
            _log.warning(
                "reasoning_aux_pins value for %r (%r) is not a recognized level; "
                "skipping pin", task, effort,
            )
            continue
        out[task] = clamped
    return out


def resolve_aux_effort(
    task: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the gated L1 effort pin for one auxiliary task, or None.

    Inert by default: returns None while ``agent.reasoning_class_routing`` is
    off (the whole L1 aux-pin layer is disabled). When on, returns the clamped
    pin for *task* if one is configured, else None. A None result means the
    caller applies its existing behavior — never an error.
    """
    if not task or not reasoning_class_routing_enabled(cfg):
        return None
    pins = reasoning_aux_pins(cfg)
    return pins.get(task)


def resolve_card_effort(
    task_class: Optional[str],
    profile: str = "",
    cfg: Optional[Dict[str, Any]] = None,
    *,
    wired_effort: Optional[str] = None,
) -> Optional[str]:
    """Resolve the L1 per-card reasoning effort, or None when not applicable.

    Gated: returns None while ``agent.reasoning_class_routing`` is off — the
    per-card pin helper is a no-op and the card runs at its profile's own
    effort.

    When on, this is the *operator* seam between the class-detector output
    (W3) and ``tasks.reasoning_effort``: it returns the clamped letovo effort
    for a detected task class. The default class->effort policy lives in
    ``agent.reasoning_class_router.DEFAULT_CLASS_EFFORT`` (complex -> max,
    routine -> low); this helper stays model- and wire-agnostic and only adds
    the letovo clamp so L1 pins can never 400 on deepseek-v4-flash.

    ``wired_effort`` is the effort already pinned by the operator (e.g. an
    explicit ``tasks.reasoning_effort`` value). When provided and non-None it
    wins — an explicit operator pin is never overridden by routing.
    """
    if wired_effort is not None:
        return clamp_to_letovo(wired_effort)
    if not reasoning_class_routing_enabled(cfg):
        return None
    if not task_class:
        return None
    from agent.reasoning_class_router import DEFAULT_CLASS_EFFORT, detect_task_class

    cls = task_class if task_class in DEFAULT_CLASS_EFFORT else detect_task_class(
        text=task_class, profile=profile
    )
    return clamp_to_letovo(DEFAULT_CLASS_EFFORT.get(cls))


def pin_card_effort(
    conn: Any,
    task_id: str,
    effort: Optional[str],
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Gated per-card effort pin — a thin wrapper over ``tasks.reasoning_effort``.

    Writes ``effort`` to ``tasks.reasoning_effort`` for *task_id* via
    ``kanban_db.set_reasoning_effort``, clamping to the letovo wire domain so a
    pin can never 400 on deepseek-v4-flash. The model is NOT touched — the card
    keeps its pinned deepseek-v4-flash@custom:letovo (priority -3, set at card
    creation); only the thinking depth is adjusted.

    Gated: when ``agent.reasoning_class_routing`` is false this is a NO-OP —
    ``False`` is returned, nothing is written, and the card keeps whatever
    effort it already has. This is the L1 default state.

    ``effort=None`` / empty clears the pin (falls back to the profile's own
    ``agent.reasoning_effort``) — like ``set_reasoning_effort``, ``"none"`` is a
    real value (thinking OFF), not a clear.
    """
    if not reasoning_class_routing_enabled(cfg):
        return False
    from hermes_cli import kanban_db

    clamped = None if effort in (None, "", "unset") else clamp_to_letovo(effort)
    return kanban_db.set_reasoning_effort(conn, task_id, clamped)


def _card_assignee_is_reviewer(assignee: Optional[str]) -> bool:
    """True when *assignee* names an independent-review gate profile."""
    if not assignee:
        return False
    return str(assignee).strip().lower() in REVIEWER_PROFILES


def _card_graph_context(
    conn: Any,
    task_id: str,
) -> tuple[bool, bool]:
    """Return ``(is_review_card, has_reviewer_child)`` for one card.

    ``is_review_card`` — the card's own assignee is an independent gate
    (09/15). ``has_reviewer_child`` — the card owns at least one direct child
    whose assignee is an independent gate (the «план/есть child-ревью»
    signal the medium scheme keys on).

    Distinguishes three outcomes:

    * task not found (or no conn/task) → raises ``LookupError`` so the
      caller's Ж1 fallback resolves it as uncertainty (high) rather than a
      definite no-gate card — a *missing* graph is not proof there is no
      gate;
    * DB/graph exception → raised so the caller's Ж1 fallback degrades to
      ``high`` (never ``medium`` on an unknown/gated card);
    * healthy read → ``(is_review_card, has_reviewer_child)``.
    """
    if not conn or not task_id:
        raise LookupError("no card graph context (missing conn/task)")
    from hermes_cli import kanban_db as _kb

    task = _kb.get_task(conn, str(task_id))
    if task is None:
        raise LookupError(f"task {task_id} not found in board")
    is_review_card = _card_assignee_is_reviewer(task.assignee)
    has_reviewer_child = False
    for child_id in _kb.child_ids(conn, str(task_id)):
        child = _kb.get_task(conn, child_id)
        if child is not None and _card_assignee_is_reviewer(child.assignee):
            has_reviewer_child = True
            break
    return is_review_card, has_reviewer_child


def resolve_card_lane_effort(
    conn: Any,
    task_id: str,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    wired_effort: Optional[str] = None,
) -> Optional[str]:
    """Resolve the per-card reasoning effort in the «medium-review» scheme.

    This is the new-scheme resolver (design/medium-review-scheme-v1.md,
    table §1) that replaces the class-based card default:

    * ``wired_effort`` (an explicit operator pin in ``tasks.reasoning_effort``)
      always wins and is only clamped to the letovo domain — routing never
      overrides an operator choice.
    * REVIEW card (assignee = 09-independent-reviewer / 15-sre-qa) → ``max``.
    * Writing card that owns a direct child review card → ``medium``.
    * Everything else (routine/short cards without a gate child) → ``None``
      (no pin; the worker inherits its profile's ``agent.reasoning_effort``,
      which is ``high`` for the 02/03 writing profiles).
    * Uncertainty / graph-read failure → ``high`` (Ж1: never fall below high
      for gated work; never ``medium`` on a gate or review).

    Gated: returns ``None`` while ``agent.reasoning_class_routing`` is off —
    the whole L1 per-card layer is inert and cards run at their profile
    default.
    """
    if wired_effort is not None:
        return clamp_to_letovo(wired_effort)
    if not reasoning_class_routing_enabled(cfg):
        return None
    try:
        is_review_card, has_reviewer_child = _card_graph_context(conn, task_id)
    except Exception:  # pragma: no cover - Ж1 fallback on uncertainty
        # Graph uncertainty (missing conn/task, DB failure) degrades UP to
        # high — never medium below a gated/review card, never an error.
        _log.warning(
            "card graph uncertainty for %r; degrading lane effort to high (Ж1)",
            task_id,
        )
        return "high"
    if is_review_card:
        return "max"
    if has_reviewer_child:
        return "medium"
    # No gate anywhere: leave the card to its profile default (NULL pin).
    return None


def apply_card_lane_effort(
    conn: Any,
    task_id: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Seed the new-scheme reasoning pin for one card, best-effort.

    Resolves the card's lane via :func:`resolve_card_lane_effort` and writes
    it to ``tasks.reasoning_effort`` (the same column :func:`pin_card_effort`
    targets). It is a SEED, never a clobber: an already-pinned
    ``tasks.reasoning_effort`` (operator/previous decision) is left untouched
    so the pinner is idempotent on repeated create/link calls.

    Gated no-op: while ``agent.reasoning_class_routing`` is off nothing is
    written (returns False). Any DB failure is swallowed — the pin is
    cosmetic (dispatch would run at the profile/class default anyway) and
    must never break card creation or linking.
    """
    if not reasoning_class_routing_enabled(cfg):
        return False
    try:
        from hermes_cli import kanban_db

        existing = kanban_db.get_task(conn, str(task_id))
        if existing is None:
            return False
        if existing.reasoning_effort:  # already pinned → never clobber
            return False
        effort = resolve_card_lane_effort(conn, str(task_id), cfg)
        if effort is None:
            return False
        return bool(
            kanban_db.set_reasoning_effort(conn, str(task_id), effort)
        )
    except Exception:  # pragma: no cover - best-effort seed
        _log.exception("lane effort seed failed for %s; leaving unpinned", task_id)
        return False
