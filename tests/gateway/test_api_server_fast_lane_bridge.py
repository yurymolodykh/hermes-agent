"""A6 bridge: Open WebUI top-level ``reasoning_effort`` reaches the fast-lane core.

Open WebUI 0.11.4 sends chat-completions parameters via
``apply_model_params_to_body_openai`` — i.e. ``reasoning_effort`` lands as a
TOP-LEVEL key of the request body (the deepthink / reasoning toggle in the UI),
while the gateway core reads slow-mode markers from ``model_options`` only.
Without a bridge, an OWUI ``reasoning_effort=high`` never became
``slow_mode=true``, so the A6 OWUI channel was dead end-to-end.

These tests lock in the bridge::

    body["reasoning_effort"] = "high"  ->  model_options.reasoning.effort = "high"
                                            model_options.reasoning_effort = "high"
                                            -> decide_slow_mode(...) => slow_mode=True
                                            -> _request_reasoning_config(...) => high

D1 semantics are preserved: ``medium`` (the OWUI default toggle value) stays
FAST, exactly like the pre-existing OWUI tab already behaved; explicit client
``model_options`` always win over the top-level scalar.
"""

from gateway.platforms.api_server import (
    _bridge_top_level_reasoning_effort,
    _merge_model_options,
    _request_agent_overrides,
    _request_reasoning_config,
)
from agent.fast_lane import decide_slow_mode


class TestBridgeTopLevelReasoningEffort:
    def test_absent_effort_is_noop(self):
        assert _bridge_top_level_reasoning_effort({}) == {}
        assert _bridge_top_level_reasoning_effort(
            {"model": "x", "messages": []}
        ) == {}

    def test_empty_string_is_noop(self):
        assert _bridge_top_level_reasoning_effort(
            {"reasoning_effort": "   "}
        ) == {}

    def test_non_dict_body_is_noop(self):
        assert _bridge_top_level_reasoning_effort(None) == {}
        assert _bridge_top_level_reasoning_effort(["reasoning_effort"]) == {}

    def test_high_effort_bridges_both_shapes(self):
        out = _bridge_top_level_reasoning_effort({"reasoning_effort": "high"})
        assert out == {
            "reasoning_effort": "high",
            "reasoning": {"enabled": True, "effort": "high"},
        }

    def test_effort_is_normalized_lowercase(self):
        out = _bridge_top_level_reasoning_effort({"reasoning_effort": "HIGH"})
        assert out["reasoning_effort"] == "high"
        assert out["reasoning"]["effort"] == "high"


class TestMergeModelOptions:
    def test_merge_creates_options_from_nothing(self):
        merged = _merge_model_options(None, {"reasoning_effort": "high"})
        assert merged == {"reasoning_effort": "high"}

    def test_client_model_options_win_over_bridge(self):
        merged = _merge_model_options(
            {"reasoning_effort": "low"},
            {"reasoning_effort": "high"},
        )
        # explicit client value wins; bridge does not overwrite
        assert merged["reasoning_effort"] == "low"

    def test_structured_reasoning_merges_subkeys(self):
        merged = _merge_model_options(
            {"reasoning": {"enabled": True}},
            {"reasoning": {"enabled": True, "effort": "high"}},
        )
        assert merged["reasoning"] == {"enabled": True, "effort": "high"}

    def test_does_not_mutate_input(self):
        original = {"reasoning_effort": "medium"}
        _merge_model_options(original, {"reasoning_effort": "high"})
        assert original == {"reasoning_effort": "medium"}


class TestRequestAgentOverridesBridge:
    def test_top_level_high_effort_adds_model_options(self):
        overrides = _request_agent_overrides(
            {"reasoning_effort": "high", "messages": []},
            virtual_model="hermes-agent",
        )
        assert overrides["model_options"]["reasoning_effort"] == "high"
        assert overrides["model_options"]["reasoning"] == {
            "enabled": True,
            "effort": "high",
        }

    def test_request_agent_overrides_high_effort_slow_core(self):
        overrides = _request_agent_overrides(
            {"reasoning_effort": "high", "messages": []},
            virtual_model="hermes-agent",
        )
        decision = decide_slow_mode(model_options=overrides["model_options"])
        assert decision.slow_mode is True
        assert decision.source == "api-deepthink"
        assert _request_reasoning_config(overrides["model_options"]) == {
            "enabled": True,
            "effort": "high",
        }

    def test_medium_effort_stays_fast(self):
        # D1: medium (OWUI default toggle value) is FAST, exactly as before.
        overrides = _request_agent_overrides(
            {"reasoning_effort": "medium", "messages": []},
            virtual_model="hermes-agent",
        )
        decision = decide_slow_mode(model_options=overrides["model_options"])
        assert decision.slow_mode is False
        assert _request_reasoning_config(overrides["model_options"]) == {
            "enabled": True,
            "effort": "medium",
        }

    def test_no_effort_no_model_options_key(self):
        # No top-level effort -> no model_options added at all (fast default).
        overrides = _request_agent_overrides(
            {"messages": []},
            virtual_model="hermes-agent",
        )
        assert "model_options" not in overrides

    def test_explicit_model_options_wins_over_top_level(self):
        # Client that already expresses reasoning via model_options is never
        # overridden by the top-level scalar bridge.
        overrides = _request_agent_overrides(
            {
                "model_options": {"reasoning_effort": "low"},
                "reasoning_effort": "high",
                "messages": [],
            },
            virtual_model="hermes-agent",
        )
        assert overrides["model_options"]["reasoning_effort"] == "low"

    def test_low_effort_never_slow(self):
        overrides = _request_agent_overrides(
            {"reasoning_effort": "low", "messages": []},
            virtual_model="hermes-agent",
        )
        decision = decide_slow_mode(model_options=overrides["model_options"])
        assert decision.slow_mode is False
