"""Fast-lane core, slice A3(1): slow_mode marker parsing (design §3).

Design contract: durable/rgz/system-speed-2x/design/fast-lane-design.md (§3).

THIS SLICE ONLY computes the ``slow_mode`` flag from markers. It deliberately
does NOT implement the fast path (reasoning/max_iterations/budget activation -
t_ae312f32), the ``fast_lane.enabled`` feature flag + degradation (t_09bf5ceb),
or the review-forcer changes (t_09bf5ceb). Callers compute the flag, log it and
persist the session marker; behavior of the existing working path is unchanged
unless a downstream slice activates it.

Marker contract (§3, single source of truth)
--------------------------------------------
1. OWUI deepthink (api_server payload ``model_options``): slow iff
   ``reasoning.enabled=true`` AND ``effort`` in {high, xhigh, max, ultra}.
   Enabled-only (no high effort) => NOT slow (medium -> fast lane). This is the
   orchestrator decision from §3: включённое reasoning само по себе не замедляет.
2. TG «подумай» (telegram adapter, text prefix): the message starts with
   ``подумай`` / ``думай`` (case-insensitive, word boundary - covers
   «подумай:», «подумай,», «думай …»/«думай: …»).
3. Phrases «гигантский план» / «гигантская задача» (any channel, in text):
   regex ``гигантск\w+\s+(план|задача|миссия|проект)`` - strictly per the
   NIGHT-PLAN list.
4. Absence of every marker => fast by default (slow_mode=False).

The computed flag is exposed three ways so the next stages can consume it:
  * ``FastLaneDecision.slow_mode`` (bool) - primary field;
  * session ``model_config`` marker (``slow_mode`` + ``slow_mode_source``) via
    ``patch_session_fast_lane_marker`` / read back with
    ``get_session_model_config_value(session_id, "slow_mode")``;
  * logger line (fast_lane=SLOW/FAST ... source=...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Marker constants (design §3)
# ---------------------------------------------------------------------------

# Reasoning efforts that mean "think seriously" -> slow (marker 1).
_SLOW_REASONING_EFFORTS = frozenset({"high", "xhigh", "max", "ultra"})

# Fast-path defaults (design §2.4/§2.5, §4.2 Track S, §5).
FAST_MAX_ITERATIONS = 8        # ≤8 LLM calls per fast request (§2.4)
FAST_RUN_BUDGET_SECONDS = 300  # wall-clock budget for fast requests (§2.4)
FAST_REASONING_ROUTINE = "medium"  # routine class A -> medium (§5)
FAST_REASONING_COMPLEX = "high"    # complex/critical/giant -> high (§5)
# "low" is NEVER used on the fast path (D3) — medium is the floor.
_REASONING_FLOOR = "medium"
# Reasoning levels that must route to the SLOW lane even when other markers
# are absent (complex/legal/irreversible work belongs to the full lane).
_COMPLEX_ALWAYS_SLOW = frozenset({"critical", "xlarge"})

# Telegram slow prefixes (marker 2). Word-boundary match after the word, so
# «подумай:», «подумай,», «подумай …», «думай …» all count and «подумайка»
# does not. The trailing lookahead accepts any separator EXCEPT a letter or
# hyphen: «подумай-ка» (the hyphenated particle form of «подумайка») is NOT
# a marker, only the imperative at the start of the message is.
_TG_SLOW_PREFIXES = ("подумай", "думай")

# Always-slow phrases (marker 3) - strictly the NIGHT-PLAN list.
_ALWAYS_SLOW_SUBJECTS = ("план", "задача", "миссия", "проект")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "да", "slow"}
    return False


def fast_lane_enabled(user_config: Optional[Dict[str, Any]]) -> bool:
    """Feature gate: fast-lane is active only when ``fast_lane.enabled`` is true.

    Design §6.2: default is **false** -> fast-lane is fully inactive and every
    request stays on the full lane (current working path, unchanged). When
    ``fast_lane.enabled: true`` is set in the gateway config, fast requests
    (slow_mode=False) activate the fast path (reasoning medium/high, budget
    cap, review-exempt marker).

    Deliberately lenient: missing section, non-dict, or absent ``enabled``
    all mean "off". The flag controls *activation only* — the marker
    computation (decide_slow_mode) and logging still run so operators can see
    what the decision would be, but the fast-path overrides are inert.
    """
    if not isinstance(user_config, dict):
        return False
    section = user_config.get("fast_lane")
    if not isinstance(section, dict):
        return False
    return _as_bool(section.get("enabled"))


def _reasoning_effort_from_model_options(
    model_options: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Pull a normalized reasoning effort name from an api_server payload.

    Mirrors ``api_server._request_reasoning_config`` shapes: either a
    structured ``reasoning: {enabled, effort}`` object or a flat
    ``reasoning_effort`` scalar.
    """
    if not isinstance(model_options, dict):
        return None
    reasoning = model_options.get("reasoning")
    effort = model_options.get("reasoning_effort")
    if isinstance(reasoning, dict):
        nested = reasoning.get("effort")
        if nested is not None:
            effort = nested
    if isinstance(effort, str):
        norm = effort.strip().lower()
        if norm:
            return norm
    return None


def model_options_mark_slow(model_options: Optional[Dict[str, Any]]) -> bool:
    """Marker 1 (OWUI deepthink): slow iff enabled AND high effort.

    Per design §3: ``enabled=true`` alone is NOT slow (medium -> fast lane).
    Explicit ``slow_mode`` / ``slow`` client flags are honored as an escape
    hatch, but the authoritative OWUI deepthink signal is high reasoning effort.
    """
    if not isinstance(model_options, dict):
        return False
    # Explicit client override escape hatch (not part of the §3 contract, but
    # harmless and unambiguous when a client sends it deliberately).
    if "slow_mode" in model_options and model_options.get("slow_mode") is not None:
        return _as_bool(model_options.get("slow_mode"))
    if "slow" in model_options and model_options.get("slow") is not None:
        return _as_bool(model_options.get("slow"))
    reasoning = model_options.get("reasoning")
    enabled: Any = None
    if isinstance(reasoning, dict):
        enabled = reasoning.get("enabled")
    if enabled is None and "reasoning" not in model_options:
        # Flat ``reasoning_effort`` (no reasoning object) - still evaluate.
        effort = _reasoning_effort_from_model_options(model_options)
        return effort in _SLOW_REASONING_EFFORTS
    if enabled is False:
        return False
    effort = _reasoning_effort_from_model_options(model_options)
    if effort in _SLOW_REASONING_EFFORTS:
        return True
    # Enabled-but-moderate (medium/lower/absent) => not slow (§3 orchestrator
    # decision: enabled alone does not slow down).
    return False


_TG_SLOW_RE = re.compile(
    r"^\s*(?:"
    + "|".join(re.escape(p) for p in _TG_SLOW_PREFIXES)
    + r")(?![\w-])",
    re.IGNORECASE,
)


def telegram_prefix_marks_slow(text: Optional[str]) -> bool:
    """Marker 2: True when ``text`` opens with the TG «подумай» marker."""
    if not text:
        return False
    return bool(_TG_SLOW_RE.match(text))


def strip_telegram_slow_prefix(text: Optional[str]) -> str:
    """Remove the leading «подумай»/«думай» marker and trailing whitespace.

    Also consumes an optional trailing ``:`` or ``,`` so the cleaned text
    reads naturally («подумай: разбери это» -> «разбери это»). When the
    marker is absent, returns the input unchanged (stripped). Used so the
    model never sees the UI directive.
    """
    if not text:
        return (text or "").strip()
    cleaned = _TG_SLOW_RE.sub("", text).lstrip()
    if cleaned[:1] in (":", ","):
        cleaned = cleaned[1:].lstrip()
    return cleaned


_ALWAYS_SLOW_RE = re.compile(
    r"гигантск\w+\s+(?:" + "|".join(re.escape(s) for s in _ALWAYS_SLOW_SUBJECTS) + r")",
    re.IGNORECASE,
)


def contains_always_slow_phrase(text: Optional[str]) -> bool:
    """Marker 3: True when ``text`` contains a NIGHT-PLAN always-slow phrase."""
    if not text:
        return False
    return bool(_ALWAYS_SLOW_RE.search(text))


# ---------------------------------------------------------------------------
# Complexity classification (design §5 / D3 — reasoning split, no "low")
# ---------------------------------------------------------------------------

# Tool-verb / analysis markers that make a request "complex" (high reasoning).
# Mirrors the adaptive model router's "large"/"xlarge"/"critical" signals
# (§5: «проанализируй/сравни/создай/исправь» and friends).
_COMPLEX_VERBS = re.compile(
    r"\b(?:проанализируй|проанализируйте|проанализировать|сравни|сравните|"
    r"создай|создайте|создать|исправь|исправьте|исправить|найди|найдите|"
    r"найти|поищи|поищите|исследуй|исследуйте|исследовать|проверь|проверьте|"
    r"проверить|настрой|настройте|настроить|установи|установите|разверни|"
    r"разверните|почини|почините|починить|напиши|напишите|составь|составьте|"
    r"подготовь|подготовьте|реализуй|реализуйте|внедри|внедрите|"
    r"find|search|research|compare|analyze|create|build|modify|fix|deploy|"
    r"configure|install|implement|write|draft)\b",
    re.IGNORECASE,
)
# Legal / irreversible / compliance work is ALWAYS complex (routes slow).
_CRITICAL_COMPLEX = re.compile(
    r"\b(?:юридическ|правов|норматив|договор|необратим|удали(?:ть|те)?\b|"
    r"опубликуй|перевод\w*\s+денег|подпиш\w*\s+(?:договор|контракт)|"
    r"legal|regulatory|contract|irreversible|delete\b|publish\b)\b",
    re.IGNORECASE,
)

# Fast-lane low threshold: shorter-than-this messages (plus no tool verbs and
# no legal/irreversible tokens and no attachments) count as "routine" class A.
_FAST_ROUTINE_MAX_CHARS = 200


def classify_complexity(text: Optional[str]) -> str:
    """Return ``"routine"`` or ``"high"`` for one request (design §5 / D3).

    - routine: short (≤~200 chars), no tool-verbs, no legal/irreversible
      tokens -> reasoning **medium** on the fast path.
    - high: contains tool/task verbs, analysis/refactoring/deploy work, or
      legal/irreversible compliance terms -> reasoning **high**.

    Criteria reuse ``adaptive_model_router.classify_request`` semantics so the
    fast-lane split and adaptive routing stay consistent. ``"low"`` is never
    a returned value (D3): medium is the floor.
    """
    if not text or not str(text).strip():
        return "routine"
    normalized = str(text).strip()
    if _CRITICAL_COMPLEX.search(normalized):
        return "high"
    if _COMPLEX_VERBS.search(normalized):
        return "high"
    if len(normalized) > _FAST_ROUTINE_MAX_CHARS:
        return "high"
    return "routine"


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FastLaneDecision:
    """Result of a fast-lane evaluation for one request/session.

    ``slow_mode`` is the authoritative flag from design §3 (two-valued,
    default False). ``mode``/``fast_lane`` are derived conveniences so sibling
    slices (t_ae312f32 fast-path, t_09bf5ceb feature flag) have stable fields
    to consume. The fast-path activation fields (``reasoning_effort``,
    ``max_iterations``, ``run_budget_seconds``, ``review_exempt``) are
    populated by the fast-path implementation (this slice t_ae312f32) and are
    inert for slow requests.
    """

    slow_mode: bool = False
    mode: str = "fast"          # "fast" | "slow" (derived from slow_mode)
    fast_lane: bool = True      # == not slow_mode (derived convenience)
    source: str = "no-marker"   # which marker triggered slow (diagnostics)
    reason: str = ""            # human-readable why (for logs)
    complexity: str = "routine"  # routine | high (D3 reasoning split)
    reasoning_effort: str = "medium"  # medium | high (never low on fast path)
    max_iterations: Optional[int] = None  # fast-path cap (≤8)
    run_budget_seconds: Optional[float] = None  # 300s for fast requests
    review_exempt: bool = False  # D5: fast requests create no review card
    debug: Dict[str, Any] = field(default_factory=dict)


def resolve_fast_path(
    decision: FastLaneDecision,
    text: Optional[str] = None,
    *,
    max_iterations: int = FAST_MAX_ITERATIONS,
    run_budget_seconds: Optional[float] = FAST_RUN_BUDGET_SECONDS,
) -> FastLaneDecision:
    """Populate the fast-path activation fields for one decision (t_ae312f32).

    Design §4.2 Track S + §5 (D3):
    - slow requests: no overrides applied (reasoning stays as configured,
      budget stays unlimited, review stays non-exempt). The decision is
      returned with ``review_exempt=False`` and the fast fields cleared.
    - fast requests (slow_mode=False): reasoning **medium** for routine class A
      and **high** for complex work; ``max_iterations`` capped at 8;
      ``run_budget_seconds=300``; ``review_exempt=True`` (D5). ``low`` is
      never selected (D3).

    ``text`` is the request text used for the complexity split; when omitted
    the decision's ``complexity`` field is kept as-is.
    """
    if decision.slow_mode:
        return FastLaneDecision(
            slow_mode=True,
            mode="slow",
            fast_lane=False,
            source=decision.source,
            reason=decision.reason,
            complexity=decision.complexity or "high",
            reasoning_effort=FAST_REASONING_COMPLEX,
            max_iterations=None,        # slow lane keeps its own budget
            run_budget_seconds=None,
            review_exempt=False,        # slow requests are NOT exempt (D5)
            debug=decision.debug,
        )
    complexity = (
        classify_complexity(text) if text is not None else decision.complexity
    )
    effort = (
        FAST_REASONING_COMPLEX if complexity == "high" else FAST_REASONING_ROUTINE
    )
    return FastLaneDecision(
        slow_mode=False,
        mode="fast",
        fast_lane=True,
        source=decision.source,
        reason=decision.reason,
        complexity=complexity,
        reasoning_effort=effort,
        max_iterations=int(max_iterations or FAST_MAX_ITERATIONS),
        run_budget_seconds=(
            float(run_budget_seconds)
            if run_budget_seconds
            else FAST_RUN_BUDGET_SECONDS
        ),
        review_exempt=True,             # D5: fast requests -> no review card
        debug=dict(decision.debug),
    )


FAST_PROFILE = "11-fast-answers"  # fast requests execute under this profile (§4.2 Track M)
SLOW_PROFILE = "01-orchestrator"  # slow requests stay on the full lane profile (§4.4)


def route_profile(
    decision: FastLaneDecision,
    *,
    fast_profile: str = FAST_PROFILE,
    slow_profile: Optional[str] = None,
) -> Optional[str]:
    """Return the profile that should EXECUTE a request (design §4.2 Track M).

    The fast-lane decision (``slow_mode``) determines the executor profile:

    - ``slow_mode=False`` (fast, markerless request) -> ``fast_profile``
      (default ``11-fast-answers``). The agent is built under that profile's
      home (SOUL/skills/config/state.db) so the fast lane runs there.
    - ``slow_mode=True`` (slow, „подумай“/„гигантская задача“/deepthink)
      -> ``slow_profile`` (default ``None`` = current/default profile,
      normally ``01-orchestrator``, the full lane).

    Returning ``None`` means "use the current request profile unchanged"
    (the full lane for slow requests, Tx; or — when routed from a context
    that already scoped a named profile — that named profile). This is the
    pure selector; the gateway call site decides whether routing is enabled
    (``fast_lane.route_to_profile``) and how to apply the result.
    """
    if not decision.slow_mode:
        return fast_profile or None
    return slow_profile or None


def decide_slow_mode(
    text: Optional[str] = None,
    model_options: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> FastLaneDecision:
    """Compute ``slow_mode`` from markers (design §3). Never raises.

    ``text`` is the raw user message (any channel); ``model_options`` is the
    api_server payload (OWUI deepthink). ``config`` may carry extra always-slow
    phrases (``slow_phrases``) - the feature flag is NOT consulted here by
    design (that is t_09bf5ceb's gate at the call site).

    Precedence (highest first): explicit model_options slow_mode/slow escape
    hatch > OWUI deepthink (enabled+high effort) > always-slow phrases >
    TG «подумай» prefix. Absence of all markers => slow_mode=False.
    """
    try:
        reasons: list[str] = []
        slow = False
        source = "no-marker"

        if model_options_mark_slow(model_options):
            slow, source = True, "api-deepthink"
            reasons.append("OWUI deepthink (reasoning enabled + effort>=high)")

        if text:
            lowered = text.lower()
            # Extra configured always-slow phrases (optional; NIGHT-PLAN base
            # is always evaluated).
            extra: list[str] = []
            if isinstance(config, dict):
                raw_extra = config.get("slow_phrases")
                if isinstance(raw_extra, (list, tuple, set)):
                    extra = [str(p).strip().lower() for p in raw_extra if str(p).strip()]
            if contains_always_slow_phrase(lowered) or any(
                p and p in lowered for p in extra
            ):
                slow, source = True, "phrase"
                reasons.append("always-slow phrase (гигантск+план/задача/миссия/проект)")
            elif telegram_prefix_marks_slow(text):
                slow, source = True, "tg-prefix"
                reasons.append("TG «подумай»")

        mode = "slow" if slow else "fast"
        return FastLaneDecision(
            slow_mode=slow,
            mode=mode,
            fast_lane=not slow,
            source=source,
            reason="; ".join(reasons) or "no markers",
            debug={"reasons": reasons},
        )
    except Exception:  # degradation - never crash on a decision
        return FastLaneDecision(
            slow_mode=False,
            mode="fast",
            fast_lane=True,
            source="error",
            reason="marker parse error -> default fast/slow_mode=False",
        )


# Backward-compatible alias matching the parent draft's call-site name; sibling
# slices may call either name.
def evaluate_request(
    text: Optional[str] = None,
    model_options: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> FastLaneDecision:
    """Alias for :func:`decide_slow_mode` (name kept for call-site stability)."""
    return decide_slow_mode(text, model_options=model_options, config=config)


# ---------------------------------------------------------------------------
# Session marker persistence (flag available for the next stages)
# ---------------------------------------------------------------------------

def fast_lane_marker_for_session(decision: FastLaneDecision) -> Dict[str, Any]:
    """Serialize a decision into the session ``model_config`` marker shape.

    Downstream stages (review forcer t_09bf5ceb, routing t_ae312f32) read
    ``slow_mode`` / ``slow_mode_source`` and the fast-path fields
    (``review_exempt`` / ``fast_lane`` / ``reasoning_effort``) from the
    session row. ``review_exempt=True`` is the D5 signal the forcer uses to
    skip fast sessions entirely (§7).
    """
    return {
        "slow_mode": bool(decision.slow_mode),
        "slow_mode_source": decision.source or "no-marker",
        "fast_lane": not bool(decision.slow_mode),
        "fast_lane_source": decision.source or "no-marker",
        "review_exempt": bool(decision.review_exempt),
        "reasoning_effort": decision.reasoning_effort or None,
        "fast_lane_complexity": decision.complexity or None,
    }


def session_is_fast_lane(marker: Optional[Dict[str, Any]]) -> bool:
    """True when a session ``model_config`` marker marks a fast session (D5).

    ``marker`` is the parsed ``model_config`` dict (or the ``model_config``
    cell from a sessions row). The forcer (t_09bf5ceb) uses this to exclude
    fast sessions from review-card creation: ``fast_lane=True`` OR
    ``review_exempt=True`` means the session produced no review card (§7).
    Defaults to False (single-profile gateways unaffected).
    """
    if not isinstance(marker, dict):
        return False
    return bool(
        marker.get("review_exempt")
        or marker.get("fast_lane")
    )


def patch_session_fast_lane_marker(
    db: Any, session_id: str, decision: FastLaneDecision
) -> None:
    """Persist the slow_mode marker onto a session row, best-effort.

    Uses ``patch_session_model_config`` merge discipline so lineage markers
    (``_branched_from`` / ``_delegate_from``, ``browser_model_lock``) survive.
    No-op on any failure (missing row, locked DB, absent method) - the flag is
    advisory and must never break the request path.
    """
    if not session_id or db is None:
        return
    try:
        patch = getattr(db, "patch_session_model_config", None)
        if patch is None:
            return
        patch(session_id, fast_lane_marker_for_session(decision))
    except Exception:
        pass


def patch_session_fast_lane_degraded(db: Any, session_id: str) -> None:
    """Write the §6.1 degradation marker onto a session row, best-effort.

    Called when a fast-path error made us fall back to the full lane, so the
    fallback is observable (design §6.1: "в сессию пишется пометка
    fast_lane_degraded=true"). Never raises, never breaks the request path.
    """
    if not session_id or db is None:
        return
    try:
        patch = getattr(db, "patch_session_model_config", None)
        if patch is None:
            return
        patch(session_id, {"fast_lane_degraded": True})
    except Exception:
        pass
