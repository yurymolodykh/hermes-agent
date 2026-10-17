"""Unit tests for the L1 adaptive-reasoning-effort layer (adaptive-reasoning-effort
mission, W3 — card t_90a6a1ca).

Covers the two L1 contracts from design/effort-policy-v1.md:

1. The effort **ladder** — clamping onto the letovo wire domain
   ``{none, low, high, max}``: invalid values slice to the nearest valid
   (never escalate cost above the ask, never invert the ladder; ``medium``/
   ``xhigh`` collapse to ``high`` per the W1-verified DeepSeek mapping,
   ``ultra`` -> ``max``, ``minimal`` -> ``low``).

2. The **feature-flag gating** — while ``agent.reasoning_class_routing`` is
   false (the default) every L1 public entry point is inert: per-card pins are
   not applied, aux pins are not consulted, nothing is written. When true, the
   helpers act but still clamp to the letovo domain so a pin can never 400.
"""

from unittest.mock import MagicMock, patch

from agent.reasoning_effort import EFFORT_LADDER, LETOVO_EFFORTS, LETOVO_OVERRIDES, clamp_effort
from agent.reasoning_effort_l1 import (
    LETOVO_DOMAIN,
    clamp_to_letovo,
    pin_card_effort,
    reasoning_aux_pins,
    reasoning_class_routing_enabled,
    resolve_aux_effort,
    resolve_card_effort,
)


# ---------------------------------------------------------------------------
# 1. Effort ladder: letovo wire-domain clamping
# ---------------------------------------------------------------------------


class TestLetovoClamp:
    def test_domain_is_exactly_letovo_wire_set(self):
        assert LETOVO_DOMAIN == ("none", "low", "high", "max")
        assert LETOVO_EFFORTS == ("none", "low", "high", "max")

    def test_supported_levels_pass_through(self):
        for level in LETOVO_EFFORTS:
            assert clamp_to_letovo(level) == level

    def test_medium_collapses_to_high(self):
        # W1 §3.1: DeepSeek has no medium tier — medium → high (not low).
        assert clamp_to_letovo("medium") == "high"

    def test_xhigh_collapses_to_high(self):
        assert clamp_to_letovo("xhigh") == "high"

    def test_ultra_clamps_to_max(self):
        # Hermes-internal; letovo has no ultra tier → max.
        assert clamp_to_letovo("ultra") == "max"

    def test_minimal_clamps_to_low(self):
        assert clamp_to_letovo("minimal") == "low"

    def test_none_is_a_real_value_not_a_clear(self):
        assert clamp_to_letovo("none") == "none"

    def test_high_and_max_pass_through(self):
        assert clamp_to_letovo("high") == "high"
        assert clamp_to_letovo("max") == "max"

    def test_none_and_empty_return_unchanged(self):
        assert clamp_to_letovo(None) is None
        assert clamp_to_letovo("") == ""

    def test_ladder_membership(self):
        for level in LETOVO_EFFORTS:
            assert level in EFFORT_LADDER, level

    def test_override_map_is_consistent_with_clamp(self):
        # Every declared override must be inside the letovo domain.
        for source, mapped in LETOVO_OVERRIDES.items():
            assert mapped in LETOVO_EFFORTS, (source, mapped)
            assert clamp_to_letovo(source) == mapped, source


class TestClampNeverEscalates:
    def test_nearest_weaker_never_escalates_cost(self):
        # xhigh against low..high → high (weaker), never max.
        assert clamp_effort("xhigh", ("low", "medium", "high", "max")) == "high"

    def test_floor_when_nothing_weaker(self):
        assert clamp_effort("minimal", ("low", "medium")) == "low"

    def test_none_is_never_a_degradation_target(self):
        # minimal against {none, low}: clamping to none would silently
        # disable thinking — must take low.
        assert clamp_effort("minimal", ("none", "low", "high")) == "low"

    def test_none_still_passes_through_when_requested(self):
        assert clamp_effort("none", ("none", "low", "high")) == "none"

    def test_monotonic_over_full_ladder(self):
        enabled_ladder = [l for l in EFFORT_LADDER if l != "none"]
        prev_rank = -1
        for level in enabled_ladder:
            out = clamp_to_letovo(level)
            rank = EFFORT_LADDER.index(out)
            assert rank >= prev_rank, (level, out)
            prev_rank = rank


# ---------------------------------------------------------------------------
# 2. Feature-flag gating: everything inert while routing is OFF
# ---------------------------------------------------------------------------


class TestGatingOff:
    def test_master_switch_defaults_false(self):
        assert reasoning_class_routing_enabled({"agent": {}}) is False
        assert reasoning_class_routing_enabled(None) is False

    def test_aux_pins_empty_when_off(self):
        cfg = {"agent": {"reasoning_aux_pins": {"web_extract": "none"}}}
        assert reasoning_aux_pins(cfg) == {}

    def test_resolve_aux_effort_none_when_off(self):
        cfg = {"agent": {"reasoning_aux_pins": {"web_extract": "none"}}}
        assert resolve_aux_effort("web_extract", cfg) is None

    def test_resolve_card_effort_none_when_off(self):
        assert resolve_card_effort("03", "03-software-engineer", {"agent": {}}) is None

    def test_pin_card_effort_is_noop_when_off(self):
        conn = MagicMock()
        assert pin_card_effort(conn, "t_123", "low", {"agent": {}}) is False
        conn.execute.assert_not_called()

    def test_explicit_wired_effort_still_wins_even_when_off(self):
        # An operator-set tasks.reasoning_effort is never overridden by
        # routing — it flows through the clamp regardless of the master switch.
        assert resolve_card_effort(
            "08", "08-document-producer", {"agent": {}},
            wired_effort="ultra",
        ) == "max"


# ---------------------------------------------------------------------------
# 3. Feature-flag ON: helpers act, but still clamp to the letovo domain
# ---------------------------------------------------------------------------


class TestGatingOn:
    def test_aux_pins_applied_and_clamped_when_on(self):
        cfg = {
            "agent": {
                "reasoning_class_routing": True,
                "reasoning_aux_pins": {
                    "web_extract": "none",
                    "compression": "low",
                    "indexing": "medium",  # collapses to high
                },
            }
        }
        pins = reasoning_aux_pins(cfg)
        assert pins == {"web_extract": "none", "compression": "low", "indexing": "high"}

    def test_resolve_aux_effort_when_on(self):
        cfg = {
            "agent": {
                "reasoning_class_routing": True,
                "reasoning_aux_pins": {"web_extract": "none"},
            }
        }
        assert resolve_aux_effort("web_extract", cfg) == "none"
        assert resolve_aux_effort("compression", cfg) is None

    def test_pin_card_effort_writes_when_on(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.set_reasoning_effort") as mock_set:
            mock_set.return_value = True
            ok = pin_card_effort(
                conn, "t_123", "medium",
                {"agent": {"reasoning_class_routing": True}},
            )
        assert ok is True
        # medium → high (letovo wire), model untouched.
        mock_set.assert_called_once_with(conn, "t_123", "high")

    def test_pin_card_effort_clear_when_on(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.set_reasoning_effort") as mock_set:
            mock_set.return_value = True
            ok = pin_card_effort(
                conn, "t_123", None,
                {"agent": {"reasoning_class_routing": True}},
            )
        assert ok is True
        mock_set.assert_called_once_with(conn, "t_123", None)

    def test_pin_card_effort_none_is_a_real_value(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.set_reasoning_effort") as mock_set:
            mock_set.return_value = True
            ok = pin_card_effort(
                conn, "t_123", "none",
                {"agent": {"reasoning_class_routing": True}},
            )
        assert ok is True
        # "none" is a real value (thinking OFF), not a clear.
        mock_set.assert_called_once_with(conn, "t_123", "none")
