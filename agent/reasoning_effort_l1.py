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
``{none, low, high, max}`` (agent.reasoning_effort.LETOVO_EFFORTS), so a
mis-typed pin can never reach the wire as a 400. ``medium``/``xhigh`` collapse
to ``high`` per the W1-verified DeepSeek mapping.
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
]

#: The letovo wire domain repeated here as a cheap import-free tuple for
#: callers that only need the domain, not the full clamp machinery.
LETOVO_DOMAIN: tuple[str, ...] = LETOVO_EFFORTS


def clamp_to_letovo(effort: Optional[str]) -> Optional[str]:
    """Clamp a requested effort onto the letovo wire domain.

    ``medium``/``xhigh`` -> ``high``; ``minimal`` -> ``low``; ``ultra`` -> ``max``
    (overrides from W1's verified DeepSeek mapping). Unknown bespoke values
    pass through unchanged (custom provider may use its own names).
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
