r"""Fast-lane core, slice A3(1): slow_mode marker parsing (design §3).

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
    ``session_slow_mode(db, session_id)`` (or the async variant
    ``session_slow_mode_async``; the raw key is ``model_config.slow_mode``);
  * logger line (fast_lane=SLOW/FAST ... source=...).

Session stickiness (t_e0350c47)
-------------------------------
Once a session has been slowed by a marker, EVERY later message in that
session runs on the slow lane even when it carries no marker of its own. The
persisted marker is the carrier: call sites read it with ``session_slow_mode``
and pass the value into ``decide_slow_mode(..., session_slow=...)``, where
``apply_session_sticky`` promotes a marker-less decision to
``slow_mode=True`` / ``source="session-sticky"``. When the request carries its
OWN slow marker as well, the source is composite
(``"session-sticky+tg-prefix"`` / ``"session-sticky+phrase"`` /
``"session-sticky+api-deepthink"``) so the ``fast_lane=SLOW|ROUTE`` line always
shows the sticky reason; the concrete marker stays in
``debug["sticky_with_source"]``. An explicit client fast
escape (``model_options["slow_mode"] = false``) still wins. The write side
uses ``preserve_sticky=True`` so a later marker-less turn can never downgrade
the session back to fast: sticky lives until the session row itself goes away
(/reset or session expiry).
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Marker constants (design §3)
# ---------------------------------------------------------------------------

# Reasoning efforts that mean "think seriously" -> slow (marker 1).
_SLOW_REASONING_EFFORTS = frozenset({"high", "xhigh", "max", "ultra"})

# Fast-path defaults (design §2.4/§2.5, §4.2 Track S, §5).
FAST_MAX_ITERATIONS = 16       # ≤16 LLM calls per fast request (§2.4)
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


def _text_view(content: Any) -> str:
    """Best-effort plain-text view of message content for marker checks.

    api_server multimodal messages (OWUI/PWA with an attached file/image)
    yield ``content`` as a *list* of parts (``{type: text|file|image_url}``)
    rather than a plain string. The RGZ/fast-lane markers must still fire on
    such messages, so every marker predicate normalises its input through this
    helper. Mirrors ``context_compressor._content_text_for_contains`` semantics
    (string passthrough, list -> concat of text parts, else str()).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def _probe_fragments(text: str) -> list[str]:
    """Split a fast-lane marker probe into message fragments.

    api_server aggregates the texts of ALL user messages in a multi-message
    request (OWUI/PWA split a text+attachment post into separate user
    messages) into a single newline-joined probe string. A marker predicate
    that only checks the very START of that joined string loses every
    marker that opens a LATER message («подумай …» after an earlier plain
    message or a file message) — the exact owner regression. Splitting the
    probe back into per-message fragments and checking the prefix on *each*
    fragment restores «в начале своего сообщения, но не сработало».

    A single-fragment probe (normal TG/CLI message) returns ``[text]`` and
    behaves exactly as before.
    """
    if not text:
        return []
    return [f for f in str(text).split("\n") if f.strip()]



# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_MARKER_TRUE = {"true", "1", "yes", "on", "да", "slow"}
_MARKER_FALSE = {"false", "0", "no", "off", "нет", "fast"}


def _as_marker_bool(value: Any) -> Optional[bool]:
    """Strictly normalize a PERSISTED marker value; ``None`` = no opinion.

    Deliberately narrower than :func:`_as_bool`: that helper is a lenient
    "is this flag on?" coercion for config/request payloads where a false-y
    answer is harmless. For the session marker a wrong ``False`` is NOT
    harmless — it reads as "this session explicitly wants the fast lane" and
    silently cancels stickiness — so anything unrecognized maps to ``None``
    (no opinion) instead of ``False``.

    Accepted (card item 3): bool; ``'true'/'1'`` -> True, ``'false'/'0'`` ->
    False (plus the equally unambiguous yes/no/on/off/slow/fast spellings and
    their Russian pairs); ints 1/0. Every other scalar — ``2``, ``-1``, ``0.0``,
    ``2.5``, ``'garbage'``, ``''`` — is unrecognized -> ``None``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _MARKER_TRUE:
            return True
        if token in _MARKER_FALSE:
            return False
        return None
    # float / proxy / dict / mock: no opinion (never a silent False).
    return None


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
    """Marker 2: True when a user message opens with the TG «подумай» marker.

    Checks the prefix on EVERY message fragment of the probe, not just the
    very start of the joined string. api_server aggregates all user messages
    of a multi-message request (OWUI/PWA text+attachment split) into one
    newline-joined probe; a «подумай» that opens a LATER message must still
    fire (owner regression: «в начале своего сообщения, но не сработало»).
    A single-message probe behaves exactly as before (strict prefix only —
    «я подумаю», «надо подумать» never fire).

    Multimodal content (list of parts) is normalised to its text view first,
    so a «подумай» prefix in a message that also carries an attachment still
    fires.
    """
    if not text:
        return False
    view = _text_view(text)
    return any(bool(_TG_SLOW_RE.match(f)) for f in _probe_fragments(view))


def strip_telegram_slow_prefix(text: Optional[str]) -> str:
    """Remove the leading «подумай»/«думай» marker and trailing whitespace.

    Also consumes an optional trailing ``:`` or ``,`` so the cleaned text
    reads naturally («подумай: разбери это» -> «разбери это»). When the
    marker is absent, returns the input unchanged (stripped). Used so the
    model never sees the UI directive. Multimodal content is normalised to
    its text view before stripping (the marker can only live in a text part).
    """
    if not text:
        return (text or "").strip()
    cleaned = _TG_SLOW_RE.sub("", _text_view(text)).lstrip()
    if cleaned[:1] in (":", ","):
        cleaned = cleaned[1:].lstrip()
    return cleaned


_ALWAYS_SLOW_RE = re.compile(
    r"гигантск\w+\s+(?:" + "|".join(re.escape(s) for s in _ALWAYS_SLOW_SUBJECTS) + r")",
    re.IGNORECASE,
)


def contains_always_slow_phrase(text: Optional[str]) -> bool:
    """Marker 3: True when ``text`` contains a NIGHT-PLAN always-slow phrase.

    Multimodal content (list of parts) is normalised to its text view first,
    so an RGZ phrase in a message that also carries an attachment still fires.
    """
    if not text:
        return False
    return bool(_ALWAYS_SLOW_RE.search(_text_view(text)))


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
    max_iterations: Optional[int] = None  # fast-path cap (≤16)
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
      and **high** for complex work; ``max_iterations`` capped at 16;
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
    # Owner decision (variant 2, 2026-10-19): the fast lane bounds fast tasks by
    # WALL-CLOCK TIME, not by LLM-call count. A hard `max_iterations` cap is what
    # caused "нелепые обрывы" of perfectly normal answers (a task that had done
    # real work got cut at call 16 and replaced with a forced one-call summary).
    # So for EVERY fast request (routine AND complex) we now clear max_iterations
    # (never hard-cut by call count) and keep only the soft 5-minute time budget
    # `run_budget_seconds=300`. The time budget is deliberately SOFT (it injects a
    # wrap-up notice at 80% and nudges the model to deliver from current state —
    # run_agent._maybe_inject_run_budget_wrapup — rather than forcibly killing the
    # turn), so a normal answer is never truncted mid-word; a genuinely stuck task
    # still self-terminates via capped stale-timeouts. Reasoning stays medium
    # (routine) / high (complex). Slow requests are unaffected (handled above).
    run_budget = (
        float(run_budget_seconds) if run_budget_seconds else FAST_RUN_BUDGET_SECONDS
    )
    return FastLaneDecision(
        slow_mode=False,
        mode="fast",
        fast_lane=True,
        source=decision.source,
        reason=decision.reason,
        complexity=complexity,
        reasoning_effort=effort,
        max_iterations=None,          # never hard-cut by LLM-call count
        run_budget_seconds=run_budget,  # soft 5-minute wall-clock orienteer
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


# ``AsyncSessionDB`` -> ``SessionDB`` unwrap bound. Real stacks are 1-2 deep;
# a larger number means we are walking an auto-vivifying stub, so stop.
_MAX_DB_UNWRAP_DEPTH = 4


def _resolve_sync_db(db: Any) -> Any:
    """Unwrap async DB doors down to the sync ``SessionDB`` (or give up).

    ``gateway/run.py`` hands callers an ``AsyncSessionDB`` (a generic
    coroutine forwarder over ``SessionDB``), while ``api_server`` hands a
    plain ``SessionDB``. The fast-lane marker helpers are synchronous by
    contract (they run inside sync call paths such as ``_create_agent``), so
    an ``AsyncSessionDB`` must be unwrapped through its ``_db`` back-pointer —
    otherwise ``getattr(db, "patch_session_model_config")`` returns an
    un-awaited coroutine factory and the write silently vanishes
    (t_e0350c47: 0 of 5 telegram sessions ever carried ``slow_mode`` while
    every OWUI session did).

    The unwrap is best-effort and BOUNDED: it stops after
    ``_MAX_DB_UNWRAP_DEPTH`` hops, at the first object without a ``_db``
    back-pointer, and at any auto-vivifying stub. The depth cap is essential —
    an unbounded walk over a ``MagicMock`` (tests, or a hostile fake) would
    loop forever, because every ``getattr(mock, "_db")`` returns a NEW child
    mock with a fresh ``id()`` and the identity-cycle guard never trips.
    """
    seen = set()
    current = db
    for _ in range(_MAX_DB_UNWRAP_DEPTH):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        # unittest.mock auto-creates attribute children on access; walking
        # those is meaningless and unbounded, so stop at the stub itself.
        if type(current).__module__.startswith("unittest.mock"):
            break
        try:
            inner = getattr(current, "_db", None)
        except Exception:
            break
        if inner is None:
            break
        current = inner
    return current


def session_slow_mode(db: Any, session_id: Optional[str]) -> Optional[bool]:
    """Read the persisted session slow_mode marker. Never raises (§6.1).

    Returns:

    - ``True`` when the session row carries ``slow_mode: true`` (the session
      was slowed by a marker at some earlier point -> sticky, see
      :func:`apply_session_sticky`);
    - ``False`` when the row explicitly carries ``slow_mode: false``;
    - ``None`` when there is no row, no marker key, no DB handle, no getter,
      or the read failed. ``None`` always means "no session opinion", so the
      per-request marker decision applies unchanged.

    Reads only ``model_config.slow_mode`` — the same key
    ``patch_session_fast_lane_marker`` writes — so writer/reader cannot drift.

    Strictly best-effort by contract (card t_aca69ccc item 2): no DB handle,
    no session id, no getter, a missing row, a ``None`` value, an
    un-unwrappable async door, an unrecognized stored value, or ANY exception
    all degrade to ``None`` ("no session opinion" — the per-request marker
    decision then applies unchanged).

    Logging levels (deliberate, reconciled with the parent card's AC5): the
    benign no-opinion paths log NOTHING at all — an unmarked session is the
    normal state of every request, so even a debug line per turn is pure noise
    in a hot path. Only a genuinely broken read logs, and it logs at WARNING
    (``exc_info`` included) because parent card t_e0350c47 criterion 5 requires
    a *visible* warning when the DB read degrades. It never propagates.
    """
    if not session_id or db is None:
        return None
    try:
        target = _resolve_sync_db(db)
        getter = getattr(target, "get_session_model_config_value", None)
        if not callable(getter) or inspect.iscoroutinefunction(getter):
            return None
        value = getter(session_id, "slow_mode", None)
        if inspect.isawaitable(value):
            # An async door we could not unwrap: close the coroutine so it
            # cannot emit an "never awaited" warning, and report "no opinion".
            close = getattr(value, "close", None)
            if callable(close):
                close()
            return None
        if value is None:
            return None
        return _as_marker_bool(value)
    except Exception:  # §6.1 — a broken marker read never breaks the request
        logger.warning(
            "fast_lane sticky read failed session=%s — treating as no session "
            "opinion",
            session_id,
            exc_info=True,
        )
        return None


async def session_slow_mode_async(
    db: Any, session_id: Optional[str]
) -> Optional[bool]:
    """Async-safe :func:`session_slow_mode` for loop-thread call sites.

    ``api_server`` request handlers run on the aiohttp loop and must not
    block it on SQLite (that is why ``_ensure_session_db_async`` exists), so
    async call sites await this variant; it offloads the read to a thread via
    ``AsyncSessionDB`` when given one, and falls back to the sync read
    otherwise. Never raises (same contract as the sync reader).
    """
    if not session_id or db is None:
        return None
    try:
        getter = getattr(db, "get_session_model_config_value", None)
        if callable(getter) and inspect.iscoroutinefunction(getter):
            value = await getter(session_id, "slow_mode", None)
            if value is None:
                return None
            return _as_marker_bool(value)
    except Exception:
        logger.warning(
            "fast_lane sticky read failed (async) session=%s — treating as no "
            "session opinion",
            session_id,
            exc_info=True,
        )
        return None
    return session_slow_mode(db, session_id)


def session_slow_mode_from_db_file(
    db_path: Any, session_id: Optional[str], *, timeout: float = 1.0
) -> Optional[bool]:
    """Read the sticky marker straight from a ``state.db`` FILE, read-only.

    The api_server request handler needs the marker BEFORE ``_create_agent``
    runs, and at that point the profile's ``SessionDB`` may not be open yet.
    Constructing one there is NOT acceptable: ``SessionDB(__init__)`` runs the
    full schema init (DDL, FTS probes, pragmas) and measured ~3 s on a fresh
    ``HERMES_HOME`` — paid on the first ``/v1/runs`` of every process for a
    flag that reads ``None`` anyway (a session can only be sticky AFTER a
    marker turn wrote it, which itself opens the DB). A cache-only read would
    fix the latency but lose stickiness across restarts, so this probe reads
    the committed row over a ``mode=ro`` URI instead: no DDL, no write lock,
    no contention with a live backend, sub-millisecond.

    Contract matches :func:`session_slow_mode`: ``True``/``False`` for a
    persisted marker, ``None`` for a missing file, a missing table/row, a
    missing key, an unrecognized value, or ANY exception — never raises,
    because a broken marker read must not break the request (§6.1).
    """
    if not session_id or db_path is None:
        return None
    try:
        path = Path(db_path)
        if not path.exists():
            return None
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=timeout, isolation_level=None
        )
        try:
            row = conn.execute(
                "SELECT model_config FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            # A state.db that EXISTS but has no ``sessions`` table is a benign
            # "no opinion" state, not a broken read: any sqlite opener (a fresh
            # HERMES_HOME touched before SessionDB's schema init, a profile
            # restored from a backup without schema, an unrelated *.db handed
            # in by mistake) produces exactly this. The contract above lists
            # "a missing table" among the no-opinion paths, and this probe runs
            # on EVERY /v1/runs before a marker can possibly exist — logging it
            # at WARNING with a traceback would spam the hot path with a
            # condition that resolves itself on the first marker turn
            # (t_ca558566). Genuinely broken files (not a database, corrupt,
            # locked out) still fall through to the warning below.
            if "no such table" not in str(exc).lower():
                raise
            return None
        finally:
            conn.close()
        if not row:
            return None
        raw = row[0]
        config: Dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                config = parsed
        elif isinstance(raw, dict):
            config = raw
        value = config.get("slow_mode")
        if value is None:
            return None
        return _as_marker_bool(value)
    except Exception:
        logger.warning(
            "fast_lane sticky read (db file) failed session=%s — treating as no "
            "session opinion",
            session_id,
            exc_info=True,
        )
        return None


def explicit_fast_escape(
    model_options: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when the CLIENT explicitly asked for the fast lane this request.

    The only escape hatch that exists in the codebase is the explicit
    ``model_options["slow_mode"]`` / ``model_options["slow"]`` override
    honoured by :func:`model_options_mark_slow` (api_server payload). When a
    client sends one of those keys with a false-y value it is deliberately
    overriding the lane, so a sticky session must not silently pull the
    request back to the slow lane (t_e0350c47 §B).

    An ABSENT key is not an escape: OWUI never sends these keys on ordinary
    turns, so absence must keep sticky in force. Non-dict/None -> False.
    """
    if not isinstance(model_options, dict):
        return False
    for key in ("slow_mode", "slow"):
        if key in model_options and model_options.get(key) is not None:
            return not _as_bool(model_options.get(key))
    return False


def _isolated_debug(decision: FastLaneDecision) -> Dict[str, Any]:
    """Deep-enough copy of ``decision.debug`` for a derived decision.

    ``dict(decision.debug or {})`` is NOT enough: it shares the nested
    ``reasons`` list, so appending to the copy also appends to the ORIGINAL
    decision's debug (verified: a caller that logged the pre-sticky decision
    afterwards saw ``session-sticky (already slow)`` in it). Copy every
    mutable container one level down so applying stickiness is side-effect
    free for the input, as the docstring promises.
    """
    debug: Dict[str, Any] = {}
    for key, value in dict(decision.debug or {}).items():
        if isinstance(value, list):
            debug[key] = list(value)
        elif isinstance(value, dict):
            debug[key] = dict(value)
        elif isinstance(value, set):
            debug[key] = set(value)
        else:
            debug[key] = value
    return debug


def apply_session_sticky(
    decision: FastLaneDecision,
    session_slow: Optional[bool],
    *,
    model_options: Optional[Dict[str, Any]] = None,
) -> FastLaneDecision:
    """Promote a fast decision to slow when the SESSION was slowed before.

    Sticky rule (t_e0350c47 §B) — precedence, highest first:

    1. an explicit client fast escape (:func:`explicit_fast_escape`) wins over
       sticky — the request stays as decided;
    2. ``session_slow is True`` and the decision has no marker of its own
       -> ``slow_mode=True`` with ``source="session-sticky"``;
    3. ``session_slow is True`` AND the request carries its own slow marker
       -> stays slow and the source becomes COMPOSITE
       (``"session-sticky+api-deepthink"`` / ``"session-sticky+phrase"`` /
       ``"session-sticky+tg-prefix"``) so every ``fast_lane=SLOW|ROUTE`` log
       line shows BOTH reasons, not just the per-request marker. The original
       marker stays available as ``debug["sticky_with_source"]``;
    4. ``session_slow`` is ``False``/``None`` (or an unrecognized value) ->
       the decision is returned unchanged (no session opinion never changes
       behavior).

    ``session_slow`` is normalised through :func:`_as_marker_bool` before the
    rule runs, so a RAW persisted value also works: ``1`` / ``'true'`` /
    ``'yes'`` / ``'slow'`` mean "session is slow", while ``'false'`` / ``'0'``
    mean "no session opinion". A bare ``session_slow is True`` check used to
    drop every non-bool truthy form, which would silently un-stick a session
    whenever a caller passed the unparsed marker instead of going through
    :func:`session_slow_mode` — the exact silent-failure the strict
    ``_as_marker_bool`` contract exists to prevent.

    Pure and non-mutating: no I/O, and the returned object never shares a
    mutable ``debug`` container with the input (a shallow ``dict()`` copy would
    keep the same ``reasons`` list, so a caller that logged the original
    decision afterwards would silently see the sticky reason appended to it).
    The caller reads the persisted marker (``session_slow_mode``) and passes the
    value in, so :func:`decide_slow_mode` stays a non-I/O function.
    """
    if _as_marker_bool(session_slow) is not True:
        return decision
    if explicit_fast_escape(model_options):
        debug = _isolated_debug(decision)
        debug.setdefault("sticky_overridden_by", "explicit-fast-escape")
        return FastLaneDecision(
            slow_mode=decision.slow_mode,
            mode=decision.mode,
            fast_lane=decision.fast_lane,
            source=decision.source,
            reason=decision.reason,
            complexity=decision.complexity,
            reasoning_effort=decision.reasoning_effort,
            max_iterations=decision.max_iterations,
            run_budget_seconds=decision.run_budget_seconds,
            review_exempt=decision.review_exempt,
            debug=debug,
        )
    if decision.slow_mode:
        # Both the session and this request say slow: the source is COMPOSITE
        # (``session-sticky+<marker>``) so the fast_lane=SLOW/ROUTE log line
        # shows the sticky reason as well as the per-request marker — card item
        # 3: "источник ... session-sticky+api-deepthink, чтобы в
        # fast_lane=ROUTE/SLOW строке было видно причину". The concrete marker
        # stays readable as debug["sticky_with_source"].
        debug = _isolated_debug(decision)
        debug["sticky_with_source"] = decision.source
        debug.setdefault("reasons", []).append("session-sticky (already slow)")
        marker_source = decision.source or ""
        if marker_source.startswith("session-sticky"):
            # Idempotent: applying stickiness twice must not build
            # "session-sticky+session-sticky".
            composite = marker_source
        else:
            composite = (
                f"session-sticky+{marker_source}"
                if marker_source
                else "session-sticky"
            )
        return FastLaneDecision(
            slow_mode=True,
            mode="slow",
            fast_lane=False,
            source=composite,
            reason=decision.reason,
            complexity=decision.complexity,
            reasoning_effort=decision.reasoning_effort,
            max_iterations=decision.max_iterations,
            run_budget_seconds=decision.run_budget_seconds,
            review_exempt=decision.review_exempt,
            debug=debug,
        )
    reasons = list((decision.debug or {}).get("reasons") or [])
    reasons.append("session-sticky (session was slowed by an earlier marker)")
    # A promoted decision is a SLOW decision, so it must carry full-lane
    # activation semantics — never the fast path's budget / iteration cap /
    # review exemption. This mirrors resolve_fast_path's slow branch, and makes
    # the promotion safe even if a caller applies it to an already-resolved
    # decision (fields are normalised rather than inherited).
    return FastLaneDecision(
        slow_mode=True,
        mode="slow",
        fast_lane=False,
        source="session-sticky",
        reason="; ".join(reasons) or "session-sticky",
        complexity=decision.complexity or "high",
        reasoning_effort=FAST_REASONING_COMPLEX,
        max_iterations=None,
        run_budget_seconds=None,
        review_exempt=False,
        debug={**_isolated_debug(decision), "reasons": reasons},
    )


def decide_slow_mode(
    text: Optional[str] = None,
    model_options: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    session_slow: Optional[bool] = None,
) -> FastLaneDecision:
    """Compute ``slow_mode`` from markers (design §3). Never raises.

    ``text`` is the raw user message (any channel); ``model_options`` is the
    api_server payload (OWUI deepthink). ``config`` may carry extra always-slow
    phrases (``slow_phrases``) - the feature flag is NOT consulted here by
    design (that is t_09bf5ceb's gate at the call site).

    ``session_slow`` is the SESSION-level sticky opinion read by the call site
    via :func:`session_slow_mode` (``True`` = this session was slowed by an
    earlier marker, ``False``/``None`` = no session opinion). Reading it here
    would turn this function into an I/O function, so the caller passes the
    value and :func:`apply_session_sticky` applies the precedence.

    Precedence (highest first): explicit model_options slow_mode/slow escape
    hatch > OWUI deepthink (enabled+high effort) > always-slow phrases >
    TG «подумай» prefix > **session-sticky** > absence of all markers
    (slow_mode=False). The explicit fast escape still beats session-sticky.
    """
    try:
        reasons: list[str] = []
        slow = False
        source = "no-marker"

        if model_options_mark_slow(model_options):
            slow, source = True, "api-deepthink"
            reasons.append("OWUI deepthink (reasoning enabled + effort>=high)")

        if text:
            # Normalise multimodal content (list of parts) to its plain-text
            # view so «подумай»/«гигантская задача» markers fire on messages
            # that also carry attachments (api_server). `_text_view` is a
            # no-op for plain strings.
            text = _text_view(text)
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
        decision = FastLaneDecision(
            slow_mode=slow,
            mode=mode,
            fast_lane=not slow,
            source=source,
            reason="; ".join(reasons) or "no markers",
            debug={"reasons": reasons},
        )
        return apply_session_sticky(
            decision, session_slow, model_options=model_options
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
    session_slow: Optional[bool] = None,
    **kwargs: Any,
) -> FastLaneDecision:
    """Alias for :func:`decide_slow_mode` (name kept for call-site stability).

    ``session_slow`` is forwarded so call sites that use this historical name
    get session stickiness too; unknown extra kwargs are still ignored, exactly
    as before.
    """
    return decide_slow_mode(
        text,
        model_options=model_options,
        config=config,
        session_slow=session_slow,
    )


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
    db: Any,
    session_id: str,
    decision: FastLaneDecision,
    *,
    preserve_sticky: bool = False,
) -> None:
    """Persist the slow_mode marker onto a session row, best-effort.

    Uses ``patch_session_model_config`` merge discipline so lineage markers
    (``_branched_from`` / ``_delegate_from``, ``browser_model_lock``) survive.
    No-op on any failure (missing row, locked DB, absent method) - the flag is
    advisory and must never break the request path.

    ``db`` may be a plain ``SessionDB`` (api_server) or an ``AsyncSessionDB``
    (gateway/run.py). The async door is unwrapped to its sync core first: a
    sync call through the door returns an un-awaited coroutine, which used to
    make this write a SILENT NO-OP on every messenger turn (t_e0350c47 — 0 of
    5 telegram sessions carried ``slow_mode`` while OWUI sessions did).

    ``preserve_sticky=True`` makes the marker MONOTONIC: a session already
    marked slow is never downgraded back to fast by a later fast decision, so
    the sticky state lives until the session row itself goes away (/reset or
    session expiry — design §D). Call sites pass True; the default stays False
    so the historical "write exactly what was decided" contract is available
    for callers that need it.
    """
    if not session_id or db is None:
        return
    try:
        target = _resolve_sync_db(db)
        patch = getattr(target, "patch_session_model_config", None)
        if patch is None or not callable(patch):
            return
        if inspect.iscoroutinefunction(patch):
            return
        if preserve_sticky and not decision.slow_mode:
            if session_slow_mode(target, session_id) is True:
                # Sticky latch: keep the session slow; only refresh the
                # request-level diagnostics keys that do not gate the lane.
                result = patch(session_id, {"fast_lane_last_request": "fast"})
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                return
        result = patch(session_id, fast_lane_marker_for_session(decision))
        if inspect.isawaitable(result):
            # Defensive: an async door slipped through the unwrap. Closing it
            # keeps the loop warning-free; the marker stays advisory.
            close = getattr(result, "close", None)
            if callable(close):
                close()
    except Exception:
        pass


def patch_session_fast_lane_degraded(db: Any, session_id: str) -> None:
    """Write the §6.1 degradation marker onto a session row, best-effort.

    Called when a fast-path error made us fall back to the full lane, so the
    fallback is observable (design §6.1: "в сессию пишется пометка
    fast_lane_degraded=true"). Never raises, never breaks the request path.
    Unwraps an ``AsyncSessionDB`` door to its sync core for the same reason as
    :func:`patch_session_fast_lane_marker`.
    """
    if not session_id or db is None:
        return
    try:
        target = _resolve_sync_db(db)
        patch = getattr(target, "patch_session_model_config", None)
        if patch is None or not callable(patch):
            return
        if inspect.iscoroutinefunction(patch):
            return
        result = patch(session_id, {"fast_lane_degraded": True})
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
    except Exception:
        pass
