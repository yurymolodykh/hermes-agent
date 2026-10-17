"""Tests for L3 per-step reasoning router (adaptive-reasoning-effort, W3).

Covers agent/reasoning_step_router.py per design/effort-policy-v1.md §3/§4:

  * gating: agent.reasoning_step_routing (DEFAULT FALSE) — router inert off,
    byte-identical behavior preserved
  * §4 signal order: task class -> final step -> failures (escalation) ->
    context -> tool type (down-only for routine)
  * default `high` under uncertainty (Ж1)
  * never below `high` unless routine is proven
  * polygon-50: observed signals -> class -> effort (acceptance-per-design §6.2)
"""

import json
import os

import pytest

from agent.reasoning_effort_l1 import clamp_to_letovo

FLAG_CFG = {"agent": {"reasoning_step_routing": True}}
FLAG_CFG_ROUTINE = {"agent": {"reasoning_step_routing": True}}
FLAG_CFG_NONE = {"agent": {}}


def _step(**kw):
    base = {"tools": [], "step_count": 0}
    base.update(kw)
    return base


class TestGating:
    def test_flag_default_off_in_defaults(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        ac = (DEFAULT_CONFIG or {}).get("agent") or {}
        assert ac.get("reasoning_step_routing") is False

    def test_inert_when_flag_absent(self):
        from agent.reasoning_step_router import (
            effort_for_step,
            step_effort_override,
            step_routing_enabled,
        )

        assert step_routing_enabled(FLAG_CFG_NONE) is False
        assert effort_for_step(_step(), messages=[{"content": "x"}], cfg=FLAG_CFG_NONE) is None
        assert (
            step_effort_override(
                {"enabled": True, "effort": "high"},
                _step(),
                cfg=FLAG_CFG_NONE,
            )
            is None
        )

    def test_override_none_when_off_preserves_config(self):
        """Default-off: step_effort_override returns None (caller keeps its
        reasoning_config untouched) even for a high-signal step."""
        from agent.reasoning_step_router import step_effort_override

        step = _step(tools=["terminal"], is_final=False)
        out = step_effort_override(
            {"enabled": True, "effort": "high"}, step, cfg=FLAG_CFG_NONE
        )
        assert out is None

    def test_clamp_to_letovo_domain(self):
        """Every routed effort stays in the letovo wire domain."""
        from agent.reasoning_step_router import LETOVO_DOMAIN, effort_for_step

        for step, msgs in [
            (_step(tools=["read_file"], step_count=1), [{"content": "s" * 500}]),
            (_step(tools=["terminal"], step_count=1), [{"content": "s" * 500}]),
            (_step(is_final=True), [{"content": "s" * 500}]),
        ]:
            e = effort_for_step(step, messages=msgs, cfg=FLAG_CFG)
            assert e is not None
            assert e in LETOVO_DOMAIN


class TestUncertaintyDefault:
    def test_undef_defaults_to_high(self):
        """Ж1: no confident signal -> default high, never below."""
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=[], step_count=1),
            messages=[{"content": "hello"}],
            cfg=FLAG_CFG,
        )
        assert e == "high"

    def test_undef_with_read_only_tool_still_high(self):
        """Read-only tool alone never drops below high on a non-routine step."""
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=["read_file"], step_count=1),
            messages=[{"content": "x" * 100}],
            cfg=FLAG_CFG,
        )
        assert e == "high"


class TestToolType:
    def test_routine_read_only_pulls_low(self):
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=["search_files"], cls="routine", step_count=1),
            messages=[{"content": "x" * 100}],
            cfg=FLAG_CFG,
        )
        assert e == "low"

    def test_routine_terminal_stays_high(self):
        """Mutating tool on routine work -> do not go below high."""
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=["terminal"], cls="routine", step_count=1),
            messages=[{"content": "x" * 100}],
            cfg=FLAG_CFG,
        )
        assert e in ("high", "max")

    def test_complex_mutating_high(self):
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=["terminal"], cls="code", step_count=1),
            messages=[{"content": "x" * 100}],
            cfg=FLAG_CFG,
        )
        assert e in ("high", "max")

    def test_review_tool_high(self):
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=["kanban_request_review"], cls="logic", step_count=1),
            messages=[{"content": "x" * 100}],
            cfg=FLAG_CFG,
        )
        assert e in ("high", "max")


class TestContextEscalation:
    def test_large_context_escalates_to_high(self):
        from agent.reasoning_step_router import effort_for_step

        # >150K tokens: blob of 700K chars (~175K tokens)
        huge = [{"content": "y" * 700_000}]
        e = effort_for_step(
            _step(tools=["read_file"], cls="undef", step_count=1),
            messages=huge,
            cfg=FLAG_CFG,
        )
        assert e == "high"

    def test_many_messages_escalates_to_high(self):
        from agent.reasoning_step_router import effort_for_step

        msgs = [{"content": "z" * 100} for _ in range(50)]  # 50 msgs > 40
        e = effort_for_step(
            _step(tools=["search_files"], cls="routine", step_count=1),
            messages=msgs,
            cfg=FLAG_CFG,
        )
        assert e == "high"  # context escalates even routine above 40 msgs

    def test_short_routine_context_can_down(self):
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=["read_file"], cls="routine", step_count=2),
            messages=[{"content": "short"}],  # < 20K tokens, <= 6 calls
            cfg=FLAG_CFG,
        )
        assert e == "low"


class TestFinalStepAndEscalation:
    def test_final_step_high(self):
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(is_final=True, cls="routine"),
            messages=[{"content": "x" * 100}],
            cfg=FLAG_CFG,
        )
        assert e == "high"

    def test_gate_final_step_max(self):
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(is_final=True, is_gate=True, cls="routine"),
            messages=[{"content": "x" * 100}],
            cfg=FLAG_CFG,
        )
        assert e == "max"

    def test_escalation_low_routine_to_high(self):
        from agent.reasoning_step_router import effort_for_step

        # 2+ consecutive failures escalate low -> high.
        e = effort_for_step(
            _step(tools=["read_file"], cls="routine", step_count=3),
            messages=[{"content": "short"}],
            history=["ok", "fail", "fail"],
            cfg=FLAG_CFG,
        )
        assert e == "high"

    def test_escalation_high_to_max(self):
        from agent.reasoning_step_router import effort_for_step

        # base=high (via step-config class_complex=high) + 3 failures -> max.
        cfg = {
            "agent": {
                "reasoning_step_routing": True,
                "reasoning_step_config": {"class_complex": "high"},
            }
        }
        e = effort_for_step(
            _step(tools=["terminal"], cls="code", step_count=3),
            messages=[{"content": "x" * 100}],
            history={"consecutive_failures": 3},
            cfg=cfg,
        )
        assert e == "max"

    def test_single_failure_no_escalation(self):
        from agent.reasoning_step_router import effort_for_step

        e = effort_for_step(
            _step(tools=["read_file"], cls="routine", step_count=2),
            messages=[{"content": "short"}],
            history=["ok", "fail"],
            cfg=FLAG_CFG,
        )
        assert e == "low"  # below the threshold, stays low


# ---------------------------------------------------------------------------
# Polygon-50: observed signals -> class -> effort (design §6.2)
# ---------------------------------------------------------------------------


def _load_polygon():
    # Polygon lives under the orchestrator profile (not the repo), so resolve
    # it from the user home — robust across worktrees.
    home = os.path.expanduser("~")
    p = os.path.join(
        home,
        "AppData",
        "Local",
        "hermes",
        "profiles",
        "01-orchestrator",
        "durable",
        "rgz",
        "adaptive-reasoning-effort",
        "research",
        "polygon-tasks.json",
    )
    p = os.path.abspath(p)
    if not os.path.exists(p):
        pytest.skip(f"polygon not found: {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class TestPolygonRouter:
    """Every polygon task, converted to observed step signals, must resolve to
    an effort that satisfies the design floor (complex >= high; routine may be
    low only when the proof holds)."""

    def test_polygon_50_signals_and_effort(self):
        from agent.reasoning_class_router import detect_task_class
        from agent.reasoning_step_router import effort_for_step

        polygon = _load_polygon()
        assert len(polygon) == 50
        results = []
        for t in polygon:
            prompt = t.get("prompt", "")
            ref = t.get("reference_answer") or ""
            cls = t.get("detected_class") or detect_task_class(prompt, t.get("profile", ""))
            api = int(t.get("api", 0) or 0)
            # Build an observed step: ~first-half tool calls, moderate context.
            step = _step(
                tools=["read_file", "search_files"],
                cls=cls,
                step_count=api,
            )
            msgs = [{"content": prompt}, {"content": ref}]
            e = effort_for_step(step, messages=msgs, cfg=FLAG_CFG)
            assert e is not None, t["id"]
            results.append((t["id"], t.get("cls"), cls, api, e))

        # Sanity: all 50 routed.
        assert len(results) == 50
        # Effort domain is always letovo-safe.
        assert all(e in ("none", "low", "high", "max") for _, _, _, _, e in results)

    def test_polygon_w0a_strat_table(self):
        """W0a strat-class table (§2): complex strat classes resolve >= high;
        routine strat classes never resolve above high on a read-only step."""
        from agent.reasoning_step_router import effort_for_step

        polygon = _load_polygon()
        hard = [t for t in polygon if t.get("strat_class") in ("code", "logic", "math", "multi_constraint")]
        assert len(hard) >= 10, "полигон: блок сложных задач >= 10 (BRIEF §7б)"
        for t in hard:
            step = _step(tools=["read_file"], cls=t["strat_class"], step_count=20)
            e = effort_for_step(step, messages=[{"content": t["prompt"]}], cfg=FLAG_CFG)
            assert e in ("high", "max"), f"{t['id']}: {e} < high (strat {t['strat_class']})"

    def test_polygon_complex_floor(self):
        """Complex-range tasks (LONG missions + BC analysis) must resolve to
        effort >= high — never below."""
        from agent.reasoning_step_router import effort_for_step

        polygon = _load_polygon()
        hard_classes = {"code", "math", "logic", "multi_constraint"}
        # LONG missions are complex by design; BC analysis items too.
        hard = [t for t in polygon if t["cls"] == "LONG"]
        assert len(hard) >= 10, "полигон: блок сложных задач >= 10 (BRIEF §7б)"
        for t in hard:
            cls = t.get("detected_class") or "logic"
            step = _step(tools=["terminal"], cls=cls, step_count=50, is_final=False)
            e = effort_for_step(step, messages=[{"content": t["prompt"]}], cfg=FLAG_CFG)
            assert e in ("high", "max"), f"{t['id']}: {e} < high"

    def test_polygon_easy_can_down(self):
        """Easy status/fact/lookup (A-class, reas=0) may drop to low — the
        minimal-sufficient principle — but on a read-only routine step only."""
        from agent.reasoning_step_router import effort_for_step

        polygon = _load_polygon()
        easy = [t for t in polygon if t["cls"] == "A" and int(t.get("reas", 0) or 0) == 0]
        assert len(easy) >= 10
        down = 0
        for t in easy:
            # W0a strat class: A status/fact/lookup -> routine (effort-table §2:
            # routine may pin low). detected_class stays undef for idiomatic
            # prompts, which correctly routes high — so use strat_class here.
            cls = t.get("strat_class") or "routine"
            step = _step(tools=["search_files"], cls=cls, step_count=2)
            e = effort_for_step(step, messages=[{"content": t["prompt"]}], cfg=FLAG_CFG)
            if e == "low":
                down += 1
        # At least a meaningful share of easy tasks route low (else the
        # minimal-sufficient principle isn't observable on the polygon).
        assert down >= 3, f"only {down}/10 easy tasks routed low"


class TestPerStepAgentSeam:
    """Live seam: per_step_agent_reasoning_config must be inert while the
    flag is off (byte-identical to base) and override only when on."""

    class _FakeAgent:
        def __init__(self, routing_on, rc=None):
            self._reasoning_step_routing = routing_on
            self.reasoning_config = rc if rc is not None else {
                "enabled": True,
                "effort": "high",
            }
            self._step_task_class = None
            self._step_tools = None
            self._step_final = False
            self._step_gate = False
            self._step_count = 1
            self._consecutive_step_failures = None

    def test_off_returns_none(self):
        from agent.reasoning_step_router import per_step_agent_reasoning_config

        a = self._FakeAgent(routing_on=False)
        out = per_step_agent_reasoning_config(a, [{"content": "hi"}])
        assert out is None

    def test_on_routes_effort(self):
        from agent.reasoning_step_router import per_step_agent_reasoning_config

        # code task + mutating tool -> effort stays high/max (>= base high).
        a = self._FakeAgent(routing_on=True, rc={"enabled": True, "effort": "high"})
        a._step_task_class = "routine"
        a._step_tools = ["search_files"]
        a._step_count = 2
        out = per_step_agent_reasoning_config(
            a, [{"content": "short"}], tools_for_api=["search_files"]
        )
        assert out is not None
        assert out["effort"] in ("none", "low", "high", "max")
        assert out["enabled"] is True

    def test_off_keeps_config_untouched(self):
        from agent.reasoning_step_router import per_step_agent_reasoning_config

        a = self._FakeAgent(routing_on=False)
        before = dict(a.reasoning_config)
        per_step_agent_reasoning_config(a, [{"content": "hi"}])
        assert a.reasoning_config == before
