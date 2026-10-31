"""L3 — per-step reasoning router (adaptive-reasoning-effort mission, W3).

Level 3 (see design/effort-policy-v1.md §3, §4): a per-step router picks the
minimal sufficient reasoning effort on each agent step from observed signals:

  * tool-call type (read-only vs mutating/coding vs review)
  * context length (token estimate / message count)
  * task class (from the card metadata / L2)
  * step history (consecutive tool failures -> escalation, ARES-RTL patience)
  * final step (terminal answer / complete / handoff)

Gated by ``agent.reasoning_step_routing`` (DEFAULT FALSE). While the master
switch is off every public function returns its "no route" value and the live
system stays byte-identical to the base revision:

  * :func:`step_effort_override` -> None (no per-step effort applied)
  * :func:`effort_for_step` -> None (router inert)
  * :func:`step_routing_enabled` -> False

Effort values are returned in the letovo wire domain ``{none, low, high, xhigh,
max}`` (``agent.reasoning_effort.LETOVO_EFFORTS``) and clamped with
:func:`clamp_to_letovo`, so a router decision can never 400 on
deepseek-v4-flash@letovo. Live-verified 2026-10-31: the backend rejects
``medium`` and ``minimal`` and serves ``xhigh`` as distinct from ``max``.
Mapping: ``medium``/``minimal`` collapse to ``high``/``low``, ``xhigh`` is
preserved, ``ultra`` -> ``max``.

Design rules (§4, order matters — conservative policy, never below ``high``
unless routine is proven):

  1. task class (strongest)      : complex -> high/max, routine -> low/none
  2. final step                  : always >= high (gate steps -> max)
  3. failures (escalation)       : >=2 consecutive tool failures:
                                     low->high, high->max
  4. context                     : >~150K tokens OR >40 messages -> >= high
                                   <~20K tokens AND <=6 calls -> routine may go low/none
  5. tool type                   : down-only for routine; never below high otherwise
  default (uncertainty)          : high  (Ж1)

Any conflict/ambiguity resolves to the highest candidate; nothing proven
non-routine ever drops below ``high``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.reasoning_effort_l1 import LETOVO_DOMAIN, clamp_to_letovo

__all__ = [
    "LETOVO_DOMAIN",
    "step_routing_enabled",
    "step_effort_override",
    "effort_for_step",
    "STEP_CONFIG_DEFAULTS",
]

#: Default per-signal decision thresholds (§4 table). All values are
#: wire-domain (letovo) efforts, kept here so the W4 A/B harness can override
#: the whole table without touching the router code.
STEP_CONFIG_DEFAULTS: Dict[str, Any] = {
    # class -> effort (complex classes pull from DEFAULT_CLASS_EFFORT via
    # effort_for_class when class routing is also enabled; this is the
    # fallback for step-only operation)
    "class_complex": "max",
    "class_routine": "low",
    "class_undef": None,  # None -> uncertainty default (high)
    # context thresholds
    "ctx_token_high": 150_000,
    "ctx_msgs_high": 40,
    "ctx_token_low": 20_000,
    "ctx_calls_low": 6,
    "ctx_escalate_to": "high",
    # patience / escalation
    "fail_threshold": 2,
    "escalate_low_to": "high",
    "escalate_high_to": "max",
    # final step
    "final_step_effort": "high",
    "final_step_gate_effort": "max",
    # tool-type pull toward low/none (routine only); mutating -> high
    "routine_down_effort": "low",
    "mutating_effort": "high",
    "review_effort": "high",
    # uncertainty default (Ж1)
    "default": "high",
}

#: Estimation budget when token count is unknown: conservative ~4 chars/token.
_CHARS_PER_TOKEN = 4.0


def _cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve the agent config dict for gating."""
    if cfg is not None:
        return (cfg or {}).get("agent") or {}
    try:
        from hermes_cli.config import load_config_readonly

        loaded = load_config_readonly()
    except Exception:
        return {}
    return (loaded or {}).get("agent") or {}


def step_routing_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Master switch for L3 (agent.reasoning_step_routing, DEFAULT FALSE)."""
    from utils import is_truthy_value  # local import: no hard dep

    return is_truthy_value(_cfg(cfg).get("reasoning_step_routing"), default=False)


def _step_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Effective step-router config table (overridable via
    ``agent.reasoning_step_config``, defaulting to STEP_CONFIG_DEFAULTS)."""
    agent_cfg = _cfg(cfg)
    override = agent_cfg.get("reasoning_step_config") or {}
    merged = dict(STEP_CONFIG_DEFAULTS)
    if isinstance(override, dict):
        merged.update(override)
    return merged


def _step_history_failures(history: Any) -> int:
    """Count consecutive trailing tool failures from the step history.

    Accepts a list of outcome tokens (``"ok"`` / ``"fail"`` / ``"error"`` /
    meaningful strings containing ``"fail"``/``"error"``), or a ``dict`` with a
    ``consecutive_failures`` key (the shape an agent would maintain), or an
    int (already a count). Anything else -> 0 (no failure signal).
    """
    if history is None:
        return 0
    if isinstance(history, int):
        return max(0, int(history))
    if isinstance(history, dict):
        return max(0, int(history.get("consecutive_failures", 0) or 0))
    if isinstance(history, (list, tuple)):
        count = 0
        for item in reversed(list(history)):
            if item is None:
                continue
            s = str(item).strip().lower()
            if s in ("fail", "error", "failed", "error", "err"):
                count += 1
                continue
            if s and s != "ok":
                # Unknown token — treat as neutral and stop the run (the
                # failure signal must be explicit).
                break
            if s == "ok":
                break
        return count
    return 0


def _tool_pulls_low(step: Dict[str, Any], cfg: Optional[Dict[str, Any]]) -> bool:
    """Design §4 tool-type signal: read-only tools pull toward low/none, but
    only as a *down* vote for routine work — never below high otherwise.

    Returns True only when the router may consider a down-effort for this step
    (read-only tool AND routine class). Callers still enforce the floor.
    """
    tools = step.get("tools") or []
    if not isinstance(tools, (list, tuple)) or not tools:
        return False
    read_only = {
        "web_extract", "search_files", "read_file", "browser_navigate",
        "kanban_show", "session_search", "tool_view", "skill_view",
        "skills_list", "browser_get_images", "web_search",
    }
    mutating = {
        "terminal", "patch", "write_file", "execute_code", "skill_manage",
        "kanban_complete", "kanban_create", "kanban_comment", "tool_call",
    }
    any_ro = any(str(t).split(".")[-1] in read_only for t in tools)
    any_mut = any(str(t).split(".")[-1] in mutating for t in tools)
    if any_mut:
        return False
    return any_ro


def _is_mutating_step(step: Dict[str, Any]) -> bool:
    tools = step.get("tools") or []
    mutating = {
        "terminal", "patch", "write_file", "execute_code", "skill_manage",
        "kanban_complete", "kanban_create", "kanban_comment", "tool_call",
    }
    return any(str(t).split(".")[-1] in mutating for t in tools)


def _is_review_step(step: Dict[str, Any]) -> bool:
    tools = step.get("tools") or []
    review = {"kanban_request_review", "kanban_request_changes", "kanban_complete"}
    return any(str(t).split(".")[-1] in review for t in tools)


def _estimate_tokens(messages: Any) -> Optional[int]:
    """Token estimate for the current context, or None when unknown.

    Accepts a list of message dicts (``len * rough chars``) or an int
    (already a token estimate/count). Rough char/token at 4 chars/token.
    """
    if messages is None:
        return None
    if isinstance(messages, int):
        return max(0, int(messages))
    if not isinstance(messages, (list, tuple)):
        return None
    if not messages:
        return 0
    total = 0
    for m in messages:
        content = ""
        if isinstance(m, dict):
            content = str(m.get("content") or "")
        elif hasattr(m, "content"):
            content = str(m.content or "")
        total += len(content)
    return int(total / _CHARS_PER_TOKEN)


def _step_class(step: Dict[str, Any]) -> Optional[str]:
    """Task class from the step/card metadata (L2 seam) or undef."""
    cls = step.get("task_class") or step.get("cls")
    if cls:
        return str(cls)
    # Profile hint fallback (engineering profiles -> complex).
    profile = step.get("profile") or ""
    if profile:
        from agent.reasoning_class_router import _PROFILE_HINTS

        for pfx, pcls in _PROFILE_HINTS.items():
            if str(profile).startswith(pfx):
                return pcls
    return "undef"


def effort_for_step(
    step: Optional[Dict[str, Any]],
    messages: Any = None,
    history: Any = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the routed per-step effort, or None when inert/disabled.

    ``step`` carries the observed signals for this step:
      * ``tools``: list of tool names being called this step
      * ``task_class`` / ``cls``: task class from card metadata
      * ``profile``: executor profile (hint)
      * ``is_final``: True when this step is the terminal answer
      * ``is_gate``: True for mission gates (F3-red-team / F6-acceptance)

    ``messages``: message list (or token estimate int) for the context signal.
    ``history``: consecutive tool-failure count (or dict/list of outcomes).

    Returns the letovo-domain effort, or None when the router is disabled or
    no override is warranted (caller keeps its existing behavior).
    """
    if not step_routing_enabled(cfg):
        return None
    conf = _step_config(cfg)
    step = step or {}

    # 1. task class (strongest)
    cls = _step_class(step)
    if cls in ("code", "math", "logic", "multi_constraint"):
        base = clamp_to_letovo(conf.get("class_complex"))
    elif cls == "routine":
        base = clamp_to_letovo(conf.get("class_routine"))
    else:
        base = None  # undef -> uncertainty default below

    # 2. final step (always >= high; gate -> max)
    if step.get("is_final"):
        if step.get("is_gate"):
            return clamp_to_letovo(conf.get("final_step_gate_effort", "max"))
        return clamp_to_letovo(conf.get("final_step_effort", "high"))

    # 3. failures — escalate (patience, ARES-RTL): low->high, high->max
    fails = _step_history_failures(history)
    fail_thr = int(conf.get("fail_threshold", 2))
    if fails >= fail_thr:
        if base in ("none", "low"):
            return clamp_to_letovo(conf.get("escalate_low_to", "high"))
        if base == "high":
            return clamp_to_letovo(conf.get("escalate_high_to", "max"))

    # 4. context
    if 100 <= (fails or 0) < fail_thr:
        pass  # (guard branch; unreachable — kept for state-machine clarity)
    tokens = _estimate_tokens(messages)
    n_msgs = len(messages) if isinstance(messages, (list, tuple)) else None
    ctx_high = tokens is not None and tokens > int(conf.get("ctx_token_high", 150_000))
    msgs_high = n_msgs is not None and n_msgs > int(conf.get("ctx_msgs_high", 40))
    if ctx_high or msgs_high:
        # >150K tokens OR >40 messages -> at least high (context-loss risk).
        cur = base or clamp_to_letovo(conf.get("default", "high"))
        from agent.reasoning_effort import EFFORT_LADDER

        want = clamp_to_letovo(conf.get("ctx_escalate_to", "high"))
        if EFFORT_LADDER.index(cur) < EFFORT_LADDER.index(want):
            return want
        return cur
    ctx_low = tokens is not None and tokens < int(conf.get("ctx_token_low", 20_000))
    calls_low = step.get("step_count", 0) <= int(conf.get("ctx_calls_low", 6))
    if (
        ctx_low and calls_low and cls == "routine"
        and not _is_mutating_step(step) and not _is_review_step(step)
    ):
        # <20K tokens AND <=6 calls AND read-only routine -> may go low/none.
        return clamp_to_letovo(conf.get("routine_down_effort", "low"))

    # 5. tool type — up-signals first (review/mutating never go down);
    #    down is only for read-only routine work and stays within the floor.
    if _is_review_step(step):
        return clamp_to_letovo(conf.get("review_effort", "high"))
    if _is_mutating_step(step):
        # Mutating/coding tools pull toward high (code paths need thought);
        # never below high unless the class is already higher.
        cur = base or clamp_to_letovo(conf.get("default", "high"))
        from agent.reasoning_effort import EFFORT_LADDER

        want = clamp_to_letovo(conf.get("mutating_effort", "high"))
        if EFFORT_LADDER.index(cur) < EFFORT_LADDER.index(want):
            return want
        return cur
    if cls == "routine" and _tool_pulls_low(step, cfg):
        # Read-only routine step with small context may down-pin.
        return clamp_to_letovo(conf.get("routine_down_effort", "low"))

    # 6. uncertainty default (Ж1) — never below high unless proven routine.
    if base is None:
        return clamp_to_letovo(conf.get("default", "high"))
    return base


def step_effort_override(
    reasoning_config: Optional[Dict[str, Any]],
    step: Optional[Dict[str, Any]],
    messages: Any = None,
    history: Any = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Apply the L3 per-step effort to an existing reasoning_config.

    Returns a NEW reasoning config dict with the routed effort, or ``None``
    when the router is disabled / no override applies (caller keeps its
    existing config untouched — byte-identical when off).

    ``reasoning_config`` must be a dict (e.g. ``{"enabled": True, "effort":
    "high"}``). The returned dict preserves ``enabled`` and drops nothing.
    """
    if not step_routing_enabled(cfg):
        return None
    if not isinstance(reasoning_config, dict):
        return None
    routed = effort_for_step(step, messages, history, cfg=cfg)
    if routed is None:
        return None
    out = dict(reasoning_config)
    out["effort"] = routed
    return out


def per_step_agent_reasoning_config(
    agent: Any,
    api_messages: Any,
    tools_for_api: Any = None,
    *,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Live L3 seam for the API-kwargs boundary (build_api_kwargs).

    Builds the per-step reasoning config for an in-flight agent request.
    Reads signals directly off the agent:

      * current tool-call -> ``tools_for_api`` (or ``agent._step_tools``)
      * task class        -> ``agent._step_task_class`` (card metadata when the
        kanban layer stamps it; None/'' -> undef -> uncertainty default high)
      * context           -> ``api_messages`` (list length / char estimate)
      * step failures     -> ``agent._consecutive_step_failures`` (when the
        loop maintains it; default off)

    Return None while ``agent.reasoning_step_routing`` is false OR when the
    base reasoning_config is absent/no override — caller keeps its existing
    config untouched (byte-identical off-switch).
    """
    if not (getattr(agent, "_reasoning_step_routing", False) or step_routing_enabled(cfg)):
        return None
    rc = getattr(agent, "reasoning_config", None)
    if not isinstance(rc, dict):
        return None
    tools = tools_for_api
    if not tools:
        tools = getattr(agent, "_step_tools", None)
    task_cls = getattr(agent, "_step_task_class", None) or ""
    is_final = bool(getattr(agent, "_step_final", False))
    is_gate = bool(getattr(agent, "_step_gate", False))
    fails = getattr(agent, "_consecutive_step_failures", None)
    step = {
        "tools": list(tools) if tools else [],
        "task_class": task_cls or None,
        "is_final": is_final,
        "is_gate": is_gate,
        "step_count": int(getattr(agent, "_step_count", 1) or 1),
    }
    # Enabled gate already passed above (agent attr or cfg). Resolve effort
    # directly; when the live agent carries the flag (config may not have it
    # loaded / be unset), feed the router a minimal enabled cfg so the value
    # resolves off the agent signal rather than a re-loaded config.
    _eff_cfg = cfg
    if _eff_cfg is None and getattr(agent, "_reasoning_step_routing", False):
        _eff_cfg = {"agent": {"reasoning_step_routing": True}}
    routed = effort_for_step(step, api_messages, fails, cfg=_eff_cfg)
    if routed is None:
        return None
    out = dict(rc)
    out["effort"] = routed
    return out
