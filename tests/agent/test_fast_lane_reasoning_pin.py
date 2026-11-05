"""Regression: a profile-owned fast-lane reasoning pin is honoured (t_71dd1bfc).

The defect: the D3 fast-path floor (medium routine / high complex) rewrote the
executing profile's own reasoning level, so profile ``11-fast-answers`` — pinned
by the owner at ``low`` via ``fast_lane.reasoning_effort`` — had every
api_server turn escalated to medium/high, a level the declared letovo domain
does not serve.

Contract under test:
  * ``resolve_fast_reasoning_pin`` reads ``fast_lane.reasoning_effort`` of the
    config it is handed (the EXECUTING profile's config), ignores unknown
    values and YAML booleans;
  * ``resolve_fast_path(..., reasoning_pin=...)`` lets that pin beat the class
    default on the FAST lane only — a slow request keeps full-lane semantics
    (effort high, review NOT exempt) regardless of the pin;
  * with no pin the code default applies (low routine, high complex), and the
    level is clamped onto the letovo wire domain.
"""

from agent.fast_lane import (
    FAST_REASONING_COMPLEX,
    FAST_REASONING_ROUTINE,
    decide_slow_mode,
    fast_lane_marker_for_session,
    resolve_fast_path,
    resolve_fast_reasoning_pin,
)

ROUTINE_TEXT = "Ответь одним словом: тест"
COMPLEX_TEXT = "проанализируй лог и сравни с baseline"


# ---------------------------------------------------------------------------
# resolve_fast_reasoning_pin — reads only this profile's pin
# ---------------------------------------------------------------------------

def test_pin_absent_returns_none():
    assert resolve_fast_reasoning_pin(None) is None
    assert resolve_fast_reasoning_pin({}) is None
    assert resolve_fast_reasoning_pin({"enabled": True}) is None


def test_pin_is_normalized():
    assert resolve_fast_reasoning_pin({"reasoning_effort": "low"}) == "low"
    assert resolve_fast_reasoning_pin({"reasoning_effort": " LOW "}) == "low"
    assert resolve_fast_reasoning_pin({"reasoning_effort": "high"}) == "high"


def test_pin_ignores_unknown_and_boolean_values():
    # A typo must never reach the wire.
    assert resolve_fast_reasoning_pin({"reasoning_effort": "banana"}) is None
    # YAML booleans are the agent.reasoning_effort disable form, not a pin.
    assert resolve_fast_reasoning_pin({"reasoning_effort": False}) is None
    assert resolve_fast_reasoning_pin({"reasoning_effort": True}) is None
    assert resolve_fast_reasoning_pin({"reasoning_effort": ""}) is None


def test_pin_does_not_read_agent_section():
    """The pin lives under ``fast_lane``, not ``agent.reasoning_effort``.

    Callers pass the ``fast_lane`` section itself; an unrelated ``agent``
    mapping handed in by mistake must not be treated as a pin.
    """
    assert resolve_fast_reasoning_pin({"reasoning_overrides": {"x": "low"}}) is None


# ---------------------------------------------------------------------------
# resolve_fast_path — pin beats the D3 default on the fast lane
# ---------------------------------------------------------------------------

def test_no_pin_keeps_code_defaults():
    d = resolve_fast_path(decide_slow_mode(text=ROUTINE_TEXT), ROUTINE_TEXT)
    assert d.reasoning_effort == FAST_REASONING_ROUTINE == "low"
    d = resolve_fast_path(decide_slow_mode(text=COMPLEX_TEXT), COMPLEX_TEXT)
    assert d.reasoning_effort == FAST_REASONING_COMPLEX == "high"


def test_no_pin_clamps_to_letovo_wire_domain():
    """Without a pin the resolved level is still wire-safe on letovo."""
    d = resolve_fast_path(
        decide_slow_mode(text=ROUTINE_TEXT),
        ROUTINE_TEXT,
        wire_base_url="https://llm.letovo.site/openai",
        wire_model="deepseek-v4-flash",
    )
    assert d.reasoning_effort == "low"
    d = resolve_fast_path(
        decide_slow_mode(text=COMPLEX_TEXT),
        COMPLEX_TEXT,
        wire_base_url="https://llm.letovo.site/openai",
        wire_model="deepseek-v4-flash",
    )
    assert d.reasoning_effort == "high"


def test_medium_pin_is_clamped_away_on_letovo():
    """A pin of `medium` is not served by letovo — it must not reach the wire."""
    d = resolve_fast_path(
        decide_slow_mode(text=ROUTINE_TEXT),
        ROUTINE_TEXT,
        reasoning_pin="medium",
        wire_base_url="https://llm.letovo.site/openai",
        wire_model="deepseek-v4-flash",
    )
    assert d.reasoning_effort == "high"  # declared mapping medium -> high
    assert d.reasoning_effort not in ("medium", "minimal")


def test_minimal_pin_is_clamped_away_on_letovo():
    d = resolve_fast_path(
        decide_slow_mode(text=ROUTINE_TEXT),
        ROUTINE_TEXT,
        reasoning_pin="minimal",
        wire_base_url="https://llm.letovo.site/openai",
        wire_model="deepseek-v4-flash",
    )
    assert d.reasoning_effort == "low"  # declared mapping minimal -> low
    assert d.reasoning_effort not in ("medium", "minimal")


def test_low_pin_survives_routine_fast_request():
    d = resolve_fast_path(
        decide_slow_mode(text=ROUTINE_TEXT), ROUTINE_TEXT, reasoning_pin="low"
    )
    assert d.slow_mode is False
    assert d.complexity == "routine"
    assert d.reasoning_effort == "low"
    # The rest of the fast-path contract is untouched by the pin.
    assert d.run_budget_seconds == 300
    assert d.review_exempt is True


def test_low_pin_survives_complex_fast_request():
    """``high`` class work under a ``low`` pin stays low — the profile wins."""
    d = resolve_fast_path(
        decide_slow_mode(text=COMPLEX_TEXT), COMPLEX_TEXT, reasoning_pin="low"
    )
    assert d.complexity == "high"
    assert d.reasoning_effort == "low"
    assert d.review_exempt is True


def test_high_pin_raises_routine_fast_request():
    d = resolve_fast_path(
        decide_slow_mode(text=ROUTINE_TEXT), ROUTINE_TEXT, reasoning_pin="high"
    )
    assert d.reasoning_effort == "high"


def test_slow_lane_is_never_rewritten_by_a_pin():
    d = resolve_fast_path(
        decide_slow_mode(text="подумай над этим"),
        "подумай над этим",
        reasoning_pin="low",
    )
    assert d.slow_mode is True
    assert d.reasoning_effort == "high"
    assert d.review_exempt is False
    assert d.run_budget_seconds is None


def test_pinned_marker_records_pinned_effort():
    d = resolve_fast_path(
        decide_slow_mode(text=ROUTINE_TEXT), ROUTINE_TEXT, reasoning_pin="low"
    )
    m = fast_lane_marker_for_session(d)
    assert m["reasoning_effort"] == "low"
    assert m["fast_lane"] is True
    assert m["review_exempt"] is True
