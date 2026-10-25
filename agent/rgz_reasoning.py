"""RGZ reasoning guarantee — mechanical enforcement (owner decision 2026-10-20).

Sole purpose: card creation / RGZ-plan preparation must ALWAYS run at
``reasoning_effort=max``, enforced by code (not just by SOUL/skill text).

Two mechanical hooks live behind these helpers:

1. RGZ trigger (session-level override)
   ------------------------------
   When an inbound message contains an RGZ plan trigger phrase
   («гигантский план» / «гигантская задача» / «гигантская миссия» /
   «гигантский проект»), the whole session is pinned to ``max`` — not just
   the first turn. The gateway calls :func:`apply_rgz_session_override`
   before the first LLM request of the turn; the persisted session override
   keeps the effort at ``max`` for every subsequent turn in that session
   (the conversation in 01 runs the full plan prep Ф0..Ф3 until the owner
   sees the plan). The override is the same field ``/reasoning --session``
   writes, so an operator can clear it the same way.

2. kanban_create hook (in-session raise)
   -------------------------------------
   Any ``kanban_create`` call from a live session raises that session's
   reasoning effort to ``max`` starting with the NEXT LLM request (the
   decomposition work happens in the turns after the tool call). The hook
   runs at tool-invocation time (agent/tool_executor
   ``_run_agent_tool_execution_middleware`` — the single chokepoint both the
   sequential and concurrent dispatch paths share) by calling
   :func:`ensure_max_for_agent`. The prior config is snapshotted on the agent
   and restored by :func:`release_agent_max` at the end of the run
   (run_agent.run_conversation outer finally), so the raise never leaks into
   a later turn of a reused cached agent.

Both hooks are pure/mechanical — no LLM involved. They never violate the
rule «не ниже high» (max >= high). The profile ``11-fast-answers`` and the
execution pinner (L1) are untouched by design.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Attribute on the agent that holds the pre-raise config while the
#: kanban_create raise is active. Present only between the raise and the
#: end-of-run release.
_RAISED_ATTR = "_rgz_saved_reasoning_config"

#: Sentinel meaning "the raise is not active" — distinguished from a
#: legitimate ``None`` snapshot (an agent whose reasoning_config was unset
#: before the raise).
_RAISED_SENTINEL = object()

#: The reasoning config that means "max think". Must match
#: :func:`hermes_constants.parse_reasoning_effort("max")` so the value is
#: byte-identical to what explicit config/slash-command produces.
MAX_REASONING_CONFIG: Dict[str, Any] = {"enabled": True, "effort": "max"}


def rgz_trigger_detected(text: Optional[str]) -> bool:
    """True when *text* contains an RGZ plan-trigger phrase.

    Reuses the fast-lane marker contract (agent/fast_lane.py: Marker 3,
    «always-slow» phrases) so there is exactly ONE phrase list in the
    codebase: ``гигантский план / задача / миссия / проект``
    (regex ``гигантск\\w+\\s+(план|задача|миссия|проект)``,
    case-insensitive). Any message that already triggers the slow/full lane
    for planning must also pin the session to max.

    Multimodal content (a list of parts, as api_server emits for a message
    that also carries an attachment) is normalised to its plain-text view
    before matching, so the RGZ phrase fires on such messages too.
    """
    if not text:
        return False
    try:
        from agent.fast_lane import contains_always_slow_phrase, _text_view
        return contains_always_slow_phrase(_text_view(text))
    except Exception:  # pragma: no cover — fast_lane import must not break the turn
        logger.debug("rgz_reasoning: fast_lane unavailable", exc_info=True)
        return False


def apply_rgz_session_override(runner: Any, session_key: Optional[str], text: Optional[str]) -> bool:
    """Pin *session_key* to ``max`` when *text* carries an RGZ trigger.

    Returns True when a session override was applied (or already present).
    Best-effort: any failure degrades to False (the per-model/global config
    still applies; the next turn re-checks the trigger).
    """
    if not session_key or not rgz_trigger_detected(text):
        return False
    try:
        setter = getattr(runner, "_set_session_reasoning_override", None)
        if setter is None:
            return False
        setter(session_key, dict(MAX_REASONING_CONFIG))
        logger.info(
            "rgz_reasoning: RGZ trigger — session %s pinned to reasoning=max until cleared",
            session_key,
        )
        return True
    except Exception:  # pragma: no cover — must never break a live turn
        logger.warning(
            "rgz_reasoning: failed to pin session %s to max", session_key, exc_info=True
        )
        return False


def ensure_max_for_agent(agent: Any) -> bool:
    """Raise a live agent's reasoning config to ``max`` (in-session hook).

    Called when ``kanban_create`` is invoked inside a live agent run. The
    next LLM request in this session then builds its API kwargs from
    ``agent.reasoning_config`` = max, so the decomposition turns that follow
    a card-creation call run at max.

    The pre-raise config is snapshotted to ``agent._rgz_saved_reasoning_config``
    (first raise only) so :func:`release_agent_max` can restore it exactly at
    the end of the run. Returns True on success (including when already max).
    """
    try:
        if agent is None:
            return False
        current = getattr(agent, "reasoning_config", None)
        if current == MAX_REASONING_CONFIG:
            return True  # already max — nothing to change or snapshot
        # Preserve enabled:true, force effort=max. A disabled-thinking
        # config ({enabled: False}) is deliberately NOT overridden to max —
        # an explicit "thinking off" stays honored (same as /reasoning).
        if isinstance(current, dict) and current.get("enabled") is False:
            return False
        # Snapshot the pre-raise config only once per raise window so the
        # release restores the ORIGINAL config, not an intermediate one.
        if getattr(agent, _RAISED_ATTR, _RAISED_SENTINEL) is _RAISED_SENTINEL:
            setattr(agent, _RAISED_ATTR, dict(current) if isinstance(current, dict) else None)
        agent.reasoning_config = dict(MAX_REASONING_CONFIG)
        logger.info(
            "rgz_reasoning: kanban_create hook — session %s reasoning -> max",
            getattr(agent, "session_id", "") or "",
        )
        return True
    except Exception:  # pragma: no cover — hook must never break the tool call
        logger.warning(
            "rgz_reasoning: kanban_create hook failed to raise effort", exc_info=True
        )
        return False


def _gateway_runner_ref():
    """Return a resolver for the live :class:`gateway.run.GatewayRunner`.

    The agent runs in a worker thread spawned by the gateway; the owning
    GatewayRunner is reachable through ``gateway.run._gateway_runner_ref``
    (a weakref). Imported lazily so this module stays importable in
    standalone/CLI processes with no live gateway (the resolver then yields
    None). Exposed as a module-level seam so tests can patch it.
    """
    try:
        from gateway.run import _gateway_runner_ref as _gref
    except Exception:  # pragma: no cover — no gateway module in standalone runs
        return lambda: None
    return (lambda: _gref() if callable(_gref) else None)


def release_rgz_session_override(agent: Any) -> bool:
    """Clear a session-level RGZ reasoning override at the end of a turn.

    Called from ``run_agent.run_conversation``'s outer finally (next to
    :func:`release_agent_max`). The RGZ trigger pins the *session* to
    ``max`` for the trigger turn; without a release the override persists on
    the long-lived session (e.g. a PWA/yadisk_bridge conversation) and every
    later ordinary request keeps resolving to ``reasoning=max`` — the
    «залипание» defect. Clearing it here means the *next* turn starts from
    the normal per-model/global config (fast lane for markerless PWA
    requests), while the trigger turn itself still ran at max.

    The agent reaches the owning GatewayRunner through the module-level
    :func:`_gateway_runner_ref` seam (safe when the live process has no
    gateway — CLI/standalone runs — or when not running under a gateway).

    Returns True when a session override was cleared, False otherwise.
    Best-effort: teardown must never raise.
    """
    try:
        if agent is None:
            return False
        session_key = getattr(agent, "_gateway_session_key", None)
        if not session_key:
            return False
        resolve = _gateway_runner_ref()
        runner = resolve() if callable(resolve) else None
        if runner is None:
            return False
        setter = getattr(runner, "_set_session_reasoning_override", None)
        if setter is None:
            return False
        setter(session_key, None)
        logger.info(
            "rgz_reasoning: released session reasoning override for %s",
            session_key,
        )
        return True
    except Exception:  # pragma: no cover — release must never break run teardown
        logger.warning(
            "rgz_reasoning: session override release failed", exc_info=True
        )
        return False


def release_agent_max(agent: Any) -> bool:
    """Restore a live agent's reasoning config after a kanban_create raise.

    Called at the end of a run (run_agent.run_conversation outer finally).
    If a raise is active for *agent*, the pre-raise config snapshot is
    restored and the snapshot cleared, so the raise never leaks into a later
    turn of a reused cached agent. Returns True when a release happened.
    Harmless no-op otherwise.
    """
    try:
        if agent is None:
            return False
        saved = getattr(agent, _RAISED_ATTR, _RAISED_SENTINEL)
        if saved is _RAISED_SENTINEL:
            return False  # never raised — nothing to release
        try:
            agent.reasoning_config = dict(saved) if isinstance(saved, dict) else None
        finally:
            try:
                delattr(agent, _RAISED_ATTR)
            except Exception:
                pass
        logger.info(
            "rgz_reasoning: kanban_create release — session %s reasoning restored",
            getattr(agent, "session_id", "") or "",
        )
        return True
    except Exception:  # pragma: no cover — release must never break run teardown
        logger.warning(
            "rgz_reasoning: kanban_create release failed to restore effort", exc_info=True
        )
        return False
