"""P1 M6/M7 — reasoning-class routing (token-optimization rollout, W2).

Gated by ``agent.reasoning_class_routing`` (DEFAULT FALSE). While disabled,
every public call returns its "no route" value (``None`` / unchanged) and the
live system stays byte-identical to the base revision. Nothing here is applied
at init or per-request unless the config flag is flipped true; the A/B gate
drives this module *externally* by feeding the per-class effort table
(:func:`class_effort_table`) into ``harness.py --effort <level>`` per class
group — see research/p1-implementation.md (mission token-optimization-rollout).

Routing policy (W0a corpus-classes.md, plan M14/M11):
  - M7 (rollout): complex classes (code/math/logic/multi-constraint) route to
    ``max`` — selective reasoning upgrade. Never ``ultra`` (400): the internal
    ladder accepts ``max`` on every OpenAI-compatible wire, ``ultra`` does not.
  - M6 (rollout): ``routine`` routes to ``low`` (down-reason for routine).
  - ``ops`` / ``undef``: no route — leave the global reasoning_effort untouched
    (ops is non-user service traffic per W0a §1; undef has ambiguous cost).
  - Real class shares are embedded here (W0a §3.1, unique tasks n=2557) so the
    A/B harness can build class-proportional samples instead of uniform ones.

The per-class map is overridable via ``agent.reasoning_class_efforts``
(``{class: level}``); overridden values are clamped to the safe wire ladder
(minimal…max), and ``ultra`` is refused with a warning. With the flag off the
map is never consulted.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# W0a real class shares (unique tasks, n = 2557; corpus-classes.tsv). Used to
# build class-proportional A/B samples and to report expected call mix.
# ---------------------------------------------------------------------------
W0A_SHARES_PCT: Dict[str, float] = {
    "code": 44.11,
    "logic": 35.00,
    "math": 2.11,
    "routine": 10.95,
    "ops": 7.00,
    "undef": 0.82,
}
W0A_COMPLEX_TOTAL_PCT = 81.23  # code+math+logic summed (95% CI 79.67–82.69)
W0A_N_TASKS = 2557

# ---------------------------------------------------------------------------
# Class taxonomy (polygon classes = W0a complex + routine; ops/undef unmapped)
# ---------------------------------------------------------------------------
COMPLEX_CLASSES = frozenset({"code", "math", "logic", "multi_constraint"})
ROUTINE_CLASSES = frozenset({"routine"})
# Polygon class -> W0a share (multi_constraint lives inside W0a "logic" per
# W0a §1; we give it the logic share for sample weighting).
POLYGON_CLASS_SHARE: Dict[str, float] = {
    "code": 44.11,
    "math": 2.11,
    "logic": 35.00,
    "multi_constraint": 35.00,  # within logic per W0a; A/B splits logic by its own plan
    "routine": 10.95,
}

# Default routing table (M6 + M7 rollout). Values must never exceed the wire-safe
# ladder; "ultra" is explicitly excluded.
#
# Grading (adaptive-reasoning-effort, letovo domain): the complex classes sit on
# ``xhigh`` rather than all four pinning ``max``. The letovo backend serves
# low/high/xhigh/max as distinct levels, so a flat ``max`` for code AND math AND
# logic buys no differentiation and spends the top tier on every card. ``xhigh``
# is the graded "hard work" default; the final-step/escalation gates in
# reasoning_step_router still raise to ``max`` when a step actually needs it.
DEFAULT_CLASS_EFFORT: Dict[str, str] = {
    "code": "xhigh",
    "math": "xhigh",
    "logic": "xhigh",
    "multi_constraint": "xhigh",
    "routine": "low",
    # ops / undef intentionally absent -> no route (leave global unchanged)
}
#: Levels that exist in Hermes' internal ladder and can be routed. This is the
#: *input* vocabulary; every value is then clamped to the letovo wire domain
#: ({none, low, high, xhigh, max}) on the way out, so ``medium``/``minimal`` are
#: accepted in config but never reach the backend (which 400s on them).
SAFE_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
FORBIDDEN_EFFORTS = frozenset({"ultra"})

# ---------------------------------------------------------------------------
# Heuristic detector (compact port of W0a's "profile + strong text markers").
# W0a's original classify_corpus.py was cleaned with its workspace; this is a
# documented re-implementation covering the same strong markers. It is a
# routing *hint* only — A/B uses the polygon's explicit per-task class label,
# not this detector.
# ---------------------------------------------------------------------------
_MARKERS: Dict[str, tuple] = {
    # Order matters: checked top-to-bottom. ``routine`` sits BEFORE ``math``
    # because "суммаризируй" (summarize, routine) contains the "суммар" math
    # substring; routine must win on summarize-type tasks.
    "routine": (
        "справк", "статус", "форматируй", "переведи", "кратко", "дайджест",
        "создай файл", "заголовок", "напоминани", "суммаризируй", "суммариз",
        "резюме",
    ),
    "code": (
        "python", "код", "реализуй", "реализовать", "напиши функцию", "функц",
        "скрипт", "endpoint", "pwa", "репозитори", "git", "сборк", "деплой",
        "отлад", "тест", "pytest", "tdd", "refactor", "рефакторинг", "claude",
        "hermes", "api", "интеграци",
    ),
    "math": (
        "вычисли", "расчёт", "формул", "статистик", "токены", "процент",
        "среднее", "суммар", "умножь", "раздели", "сколько составляет",
    ),
    "logic": (
        "ревью", "аудит", "приёмк", "анализ", "диагностик", "исследовани",
        "методологи", "план", "планировани", "многошагов", "мультиконстрейнт",
        "проверь", "оцени", "сравни", "вывод", "структур",
    ),
}

_PROFILE_HINTS = {
    # (detected profile executor prefixes) -> class. Same spirit as W0a's
    # "профиль исполнителя" feature: engineering profiles are dominated by
    # complex logic/code work.
    "01-orchestrator": "logic",
    "02-hermes-platform": "code",
    "04-web-researcher": "logic",
    "05-rag-knowledge-engineer": "logic",
    "06-contest-methodologist": "logic",
    "07-document-producer": "routine",
    "08-visual-media-designer": "routine",
    "09-independent-reviewer": "logic",
    "10-skill-librarian": "logic",
}


def detect_task_class(
    text: str = "",
    profile: str = "",
    source: str = "",
    task_title: str = "",
) -> str:
    """Heuristic task-class hint (W0a marker port).

    Order: profile hint (kanban cards are ~99.5% complex per W0a, so the
    profile hint for card-driven work is strong) -> title markers -> body
    markers. Returns one of ``code|math|logic|routine|ops|undef``. This is a
    routing HINT; A/B uses the polygon's explicit class label.
    """
    hay = f"{task_title} {text}".lower()
    # Strong markers first on the free text.
    for cls, keys in _MARKERS.items():
        if any(k.lower() in hay for k in keys):
            return cls
    # Ops (service traffic) — header/title-only, no user task body.
    if not text.strip() and (source or "").lower() in {"cron", "ops", "health"}:
        return "ops"
    if profile:
        for pfx, cls in _PROFILE_HINTS.items():
            if str(profile).startswith(pfx):
                return cls
    return "undef"


def class_effort_table(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[str]]:
    """Machine-readable per-class effort table for the A/B harness (W3).

    Returns ``{polygon_class: effort_or_None}`` for all five polygon classes
    plus ``ops``/``undef``. W3 feeds the non-None values into ``harness.py
    --classes <cls> --effort <effort>`` for the after-arm, while the before-arm
    keeps the global effort. Always respects config overrides + the safe-ladder
    clamp; returns all-``None`` when routing is disabled.
    """
    if not _routing_enabled(cfg):
        return {c: None for c in (*POLYGON_CLASS_SHARE, "ops", "undef")}
    overrides = _overrides(cfg)
    out: Dict[str, Optional[str]] = {}
    for cls in (*POLYGON_CLASS_SHARE, "ops", "undef"):
        if cls in overrides:
            out[cls] = _clamp(overrides[cls])
        else:
            out[cls] = _clamp(DEFAULT_CLASS_EFFORT.get(cls) or "")
    return out


def effort_for_class(
    cls: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the routed effort for one class, or None when unrouted/disabled."""
    if not _routing_enabled(cfg):
        return None
    overrides = _overrides(cfg)
    if cls in overrides:
        return _clamp(overrides[cls])
    default = DEFAULT_CLASS_EFFORT.get(cls)
    return _clamp(default) if default else None


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _routing_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    agent_cfg = (cfg or {}).get("agent") or {}
    return bool(agent_cfg.get("reasoning_class_routing", False))


def _overrides(cfg: Optional[Dict[str, Any]]) -> Dict[str, str]:
    agent_cfg = (cfg or {}).get("agent") or {}
    o = agent_cfg.get("reasoning_class_efforts") or {}
    return o if isinstance(o, dict) else {}


def _clamp(effort: str) -> Optional[str]:
    val = str(effort or "").strip().lower()
    if not val:
        return None
    if val in FORBIDDEN_EFFORTS:
        import logging
        logging.getLogger(__name__).warning(
            "reasoning_class_efforts refuses '%s' (ultra is not a safe wire "
            "effort); dropping route", val,
        )
        return None
    if val not in SAFE_EFFORTS:
        import logging
        logging.getLogger(__name__).warning(
            "reasoning_class_efforts unknown level '%s'; dropping route", val,
        )
        return None
    # Route output is a letovo pin in every deployment this layer serves, and
    # the backend rejects medium/minimal with HTTP 400. Clamp here so no caller
    # can bypass the domain by asking this module directly.
    from agent.reasoning_effort import LETOVO_EFFORTS, LETOVO_OVERRIDES, clamp_effort

    return clamp_effort(val, LETOVO_EFFORTS, LETOVO_OVERRIDES, strict=True)


__all__ = [
    "W0A_SHARES_PCT",
    "W0A_COMPLEX_TOTAL_PCT",
    "COMPLEX_CLASSES",
    "ROUTINE_CLASSES",
    "DEFAULT_CLASS_EFFORT",
    "detect_task_class",
    "class_effort_table",
    "effort_for_class",
]
