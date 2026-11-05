"""Fast-path + reasoning-split tests for agent/fast_lane.py (t_ae312f32).

Covers design §4.2 Track S, the reasoning split (t_0563b054: routine -> ``low``,
complex -> ``high``) and §7 (D5 review-exempt marker) for fast requests
(slow_mode=False).

Acceptance matrix exercised here:
  * fast request  -> reasoning low (routine) / high (complex);
  * a profile pin (``fast_lane.reasoning_effort``) beats the class default;
  * the resolved level is clamped onto the letovo wire domain, so ``medium``
    can never reach a letovo turn;
  * fast request -> review_exempt=True (no review card);
  * slow request  -> untouched (budget unlimited, effort as configured,
    review_exempt=False);
  * classify_complexity returns only the labels "routine"/"high".
"""
from __future__ import annotations

import pytest

from agent.fast_lane import (
    FAST_MAX_ITERATIONS,
    FAST_REASONING_COMPLEX,
    FAST_REASONING_ROUTINE,
    FAST_RUN_BUDGET_SECONDS,
    FastLaneDecision,
    classify_complexity,
    decide_slow_mode,
    fast_lane_marker_for_session,
    resolve_fast_path,
    session_is_fast_lane,
)

# ---------------------------------------------------------------------------
# classify_complexity — D3 reasoning split (never "low")
# ---------------------------------------------------------------------------


def test_classify_routine_short_question():
    assert classify_complexity("сколько будет 2+2?") == "routine"
    assert classify_complexity("Какой статус задачи t_123?") == "routine"
    assert classify_complexity("что известно о model_routes?") == "routine"


def test_classify_routine_empty_and_whitespace():
    assert classify_complexity("") == "routine"
    assert classify_complexity(None) == "routine"
    assert classify_complexity("   ") == "routine"


def test_classify_high_tool_verbs():
    assert classify_complexity("найди и сравни два отчёта") == "high"
    assert classify_complexity("проанализируй лог и создай отчёт") == "high"
    assert classify_complexity("исправь баг в api_server") == "high"
    assert classify_complexity("deploy the service") == "high"


def test_classify_high_legal_or_irreversible():
    assert classify_complexity("составь юридическое заключение") == "high"
    assert classify_complexity("удали все файлы в каталоге") == "high"
    assert classify_complexity("подпиши договор") == "high"


def test_classify_high_long_message():
    assert classify_complexity("x" * 300) == "high"


def test_classify_never_returns_low():
    # D3: "low" is never a valid fast-path classification.
    samples = [
        "сколько будет 2+2?",
        "привет",
        "проанализируй всё",
        "x" * 500,
        "",
        "юридическое заключение по договору",
    ]
    for s in samples:
        assert classify_complexity(s) in ("routine", "high")
        assert classify_complexity(s) != "low"


# ---------------------------------------------------------------------------
# resolve_fast_path — fast requests get budget + reasoning + review_exempt
# ---------------------------------------------------------------------------


def test_fast_request_routine_low_budget_review_exempt():
    d = resolve_fast_path(
        decide_slow_mode(text="сколько будет 2+2?"), "сколько будет 2+2?"
    )
    assert d.slow_mode is False
    assert d.mode == "fast"
    assert d.complexity == "routine"
    assert d.reasoning_effort == FAST_REASONING_ROUTINE == "low"
    # Variant 2 (owner decision): fast lane bounds by TIME, not call count. Even
    # routine fast requests are never hard-cut by max_iterations; they keep only
    # the soft 5-minute run_budget (300s).
    assert d.max_iterations is None
    assert d.run_budget_seconds == FAST_RUN_BUDGET_SECONDS == 300
    assert d.review_exempt is True  # D5


def test_fast_request_complex_high_reasoning():
    d = resolve_fast_path(
        decide_slow_mode(text="проанализируй лог и сравни с baseline"),
        "проанализируй лог и сравни с baseline",
    )
    assert d.slow_mode is False
    assert d.complexity == "high"
    assert d.reasoning_effort == FAST_REASONING_COMPLEX == "high"
    # Variant 2 (owner decision, 2026-10-19): complex fast work gets the SAME
    # time-bound as routine — max_iterations=None (never hard-cut by call count,
    # which caused "нелепые обрывы") and a soft 5-minute run_budget (300s).
    assert d.max_iterations is None
    assert d.run_budget_seconds == FAST_RUN_BUDGET_SECONDS == 300
    assert d.review_exempt is True


def test_slow_request_untouched_by_fast_path():
    d = resolve_fast_path(
        decide_slow_mode(text="подумай над этим"),
        "подумай над этим",
    )
    assert d.slow_mode is True
    assert d.mode == "slow"
    # Slow lane: full budget (no cap), review NOT exempt, effort high.
    assert d.max_iterations is None
    assert d.run_budget_seconds is None
    assert d.review_exempt is False
    assert d.reasoning_effort == "high"


def test_marker_phrases_route_slow_and_untouched():
    d = resolve_fast_path(
        decide_slow_mode(text="гигантская задача: спланируй миграцию"),
        "гигантская задача: спланируй миграцию",
    )
    assert d.slow_mode is True
    assert d.review_exempt is False
    assert d.max_iterations is None


def test_deep_think_routes_slow_and_untouched():
    mo = {"reasoning": {"enabled": True, "effort": "high"}}
    d = resolve_fast_path(
        decide_slow_mode(text="анализ", model_options=mo), "анализ"
    )
    assert d.slow_mode is True
    assert d.review_exempt is False


def test_no_medium_or_minimal_ever_on_letovo_fast_path():
    # t_0563b054: the fast lane must never put a letovo-unservable level on the
    # wire. `medium` is accepted by the backend today (measured 2026-11-05) but
    # is NOT in the declared domain, so the engine must map it away itself.
    for text in [
        "сколько будет 2+2?",
        "привет",
        "сравни два файла",
        "x" * 400,
    ]:
        d = resolve_fast_path(
            decide_slow_mode(text=text),
            text,
            wire_base_url="https://llm.letovo.site/openai",
            wire_model="deepseek-v4-flash",
        )
        assert d.reasoning_effort not in ("medium", "minimal")
        assert d.reasoning_effort in ("none", "low", "high", "xhigh", "max")


# ---------------------------------------------------------------------------
# review-exempt marker (D5) — the forcer's input
# ---------------------------------------------------------------------------


def test_fast_request_marker_is_review_exempt():
    d = resolve_fast_path(
        decide_slow_mode(text="какой статус t_001?"),
        "какой статус t_001?",
    )
    m = fast_lane_marker_for_session(d)
    assert m["fast_lane"] is True
    assert m["review_exempt"] is True
    assert m["slow_mode"] is False
    assert m["reasoning_effort"] == "low"
    assert session_is_fast_lane(m) is True


def test_slow_request_marker_not_review_exempt():
    d = resolve_fast_path(
        decide_slow_mode(text="гигантская задача: X"), "гигантская задача: X"
    )
    m = fast_lane_marker_for_session(d)
    assert m["slow_mode"] is True
    assert m["review_exempt"] is False
    assert m["fast_lane"] is False
    assert session_is_fast_lane(m) is False


def test_session_is_fast_lane_defaults_false():
    assert session_is_fast_lane(None) is False
    assert session_is_fast_lane({}) is False
    assert session_is_fast_lane("not a dict") is False
    assert session_is_fast_lane({"slow_mode": True}) is False


def test_fast_lane_marker_round_trip():
    # The marker survives as the canonical D5 signal: fast -> exempt.
    d = resolve_fast_path(decide_slow_mode(text="статус t_x"), "статус t_x")
    m = fast_lane_marker_for_session(d)
    assert session_is_fast_lane(m)
    # And an explicit review_exempt flag alone is enough for the forcer.
    assert session_is_fast_lane({"review_exempt": True}) is True


# ---------------------------------------------------------------------------
# fast_lane_enabled — feature gate (design §6.2, default FALSE)
# ---------------------------------------------------------------------------


def test_fast_lane_enabled_defaults_false():
    from agent.fast_lane import fast_lane_enabled

    assert fast_lane_enabled(None) is False
    assert fast_lane_enabled({}) is False
    assert fast_lane_enabled({"agent": {"reasoning_effort": "high"}}) is False
    assert fast_lane_enabled({"fast_lane": {}}) is False
    assert fast_lane_enabled({"fast_lane": {"enabled": False}}) is False
    assert fast_lane_enabled({"fast_lane": {"enabled": 0}}) is False
    assert fast_lane_enabled({"fast_lane": "not-a-dict"}) is False


def test_fast_lane_enabled_true_when_flag_on():
    from agent.fast_lane import fast_lane_enabled

    assert fast_lane_enabled({"fast_lane": {"enabled": True}}) is True
    assert fast_lane_enabled({"fast_lane": {"enabled": 1}}) is True
    assert fast_lane_enabled({"fast_lane": {"enabled": "true"}}) is True
    # Unknown keys in the section don't matter; only ``enabled`` is read.
    assert fast_lane_enabled(
        {"fast_lane": {"enabled": True, "max_iterations": 12}}
    ) is True


# ---------------------------------------------------------------------------
# patch_session_fast_lane_degraded — §6.1 observable degradation
# ---------------------------------------------------------------------------


def test_patch_session_fast_lane_degraded_writes_marker():
    from unittest.mock import MagicMock

    from agent.fast_lane import patch_session_fast_lane_degraded

    db = MagicMock()
    patch_session_fast_lane_degraded(db, "sess-1")
    assert db.patch_session_model_config.called
    assert db.patch_session_model_config.call_args[0][1] == {
        "fast_lane_degraded": True
    }


def test_patch_session_fast_lane_degraded_noop_on_garbage():
    from unittest.mock import MagicMock

    from agent.fast_lane import patch_session_fast_lane_degraded

    # No db / no session -> silent no-op (never raises).
    patch_session_fast_lane_degraded(None, "sess-1")
    patch_session_fast_lane_degraded(MagicMock(), None)
    patch_session_fast_lane_degraded(MagicMock(), "")


def test_patch_session_fast_lane_degraded_survives_db_error():
    from unittest.mock import MagicMock

    from agent.fast_lane import patch_session_fast_lane_degraded

    db = MagicMock()
    db.patch_session_model_config.side_effect = RuntimeError("db locked")
    patch_session_fast_lane_degraded(db, "sess-1")  # must not raise
