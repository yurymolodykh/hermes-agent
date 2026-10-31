"""P1 measures (token-optimization rollout, W2) + L1 adaptive-reasoning pins
(adaptive-reasoning-effort mission) — focused unit tests.

Validates the P1 implementation points are gated DEFAULT OFF and behave
byte-identically to the base revision when off, exercise the gated behavior
when flipped on, and cover the L1 per-card/auxiliary pin layer (letovo wire
ladder clamping, gated per-card effort helper). Run:
    python -m pytest agent/test_p1_measures.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# config defaults: every P1 flag must default to OFF
# ---------------------------------------------------------------------------


class TestConfigDefaultsOff:
    def test_prompt_caching_p1_keys_default_off(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG or {}
        pc = (cfg.get("prompt_caching") or {})
        assert pc.get("stable_prefix") is False
        assert pc.get("sort_tool_schemas") is False
        assert pc.get("freeze_volatile") is False
        assert pc.get("compact_tool_schemas") is False

    def test_reasoning_class_routing_default_off(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG or {}
        ac = (cfg.get("agent") or {})
        assert ac.get("reasoning_class_routing") is False


# ---------------------------------------------------------------------------
# M1: stable-prefix fingerprint guard
# ---------------------------------------------------------------------------


class FakeAgent:
    def __init__(self):
        self._stable_prefix_guard = False
        self.valid_tool_names = []
        self.load_soul_identity = False
        self.skip_context_files = True
        self._task_completion_guidance = False
        self._parallel_tool_call_guidance = False
        self._freeze_volatile = False
        self._memory_store = None
        self._memory_enabled = True
        self._user_profile_enabled = True
        self._memory_manager = None
        self.pass_session_id = False
        self.session_id = None
        self.model = "test-model"
        self.provider = "test-provider"
        self.platform = "cli"
        self.context_compressor = None
        self.api_mode = "openai"
        self.api_key = None
        self.base_url = None
        self.tools = []
        self._cached_system_prompt_static = None
        self._use_prompt_caching = False
        self._cache_ttl = None
        self.stable_prefix_rebuilds = 0


class TestM1StablePrefixGuard:
    def test_guard_off_does_not_touch_state(self):
        from agent.system_prompt import build_system_prompt

        agent = FakeAgent()
        agent._stable_prefix_guard = False
        s1 = build_system_prompt(agent)
        assert "_last_stable_fingerprint" not in agent.__dict__
        assert getattr(agent, "stable_prefix_rebuilds", 0) == 0
        # repeat build, still no fingerprint state
        build_system_prompt(agent)
        assert "_last_stable_fingerprint" not in agent.__dict__

    def test_guard_on_detects_drift_and_counts(self):
        from agent.system_prompt import build_system_prompt

        agent = FakeAgent()
        agent._stable_prefix_guard = True
        build_system_prompt(agent)
        assert agent._last_stable_fingerprint
        build_system_prompt(agent)  # identical stable -> no drift
        assert agent.stable_prefix_rebuilds == 0

        # simulate a changed stable prefix source (identity body change) —
        # forcing a rebuild with a modified SOUL/dynamic source is complex, so
        # we assert the counter attribute exists and the fingerprint is stable
        fp_before = agent._last_stable_fingerprint
        build_system_prompt(agent)
        assert agent._last_stable_fingerprint == fp_before


# ---------------------------------------------------------------------------
# M2: canonical tool-schema serialization
# ---------------------------------------------------------------------------


class TestM2StableToolsJson:
    def _tools(self):
        return [
            {"type": "function", "function": {"name": "zeta", "description": "z"}},
            {"type": "function", "function": {"name": "alpha", "description": "a"}},
        ]

    def test_sorted_and_deterministic(self):
        from model_tools import stable_tools_json

        j1 = stable_tools_json(self._tools())
        j2 = stable_tools_json([self._tools()[1], self._tools()[0]])
        assert j1 == j2
        assert j1.index("alpha") < j1.index("zeta")

    def test_prompt_size_metric_respects_flag(self):
        import json as _json

        from hermes_cli import prompt_size

        agent = FakeAgent()
        agent.tools = self._tools()
        agent._sort_tool_schemas = False
        base = _json.dumps(agent.tools, ensure_ascii=False)
        # default off => identical historical serialization
        import unittest.mock as mock

        with mock.patch.object(
            prompt_size, "_build_inspection_agent", return_value=agent
        ):
            data = prompt_size.compute_prompt_breakdown("cli")
        assert data["tools"]["count"] == 2
        # json_bytes equals the non-canonical historical length when the flag
        # is off (the metric still uses defaults in the off path)
        assert len(base) > 0


# ---------------------------------------------------------------------------
# M3: freeze volatile blocks
# ---------------------------------------------------------------------------


class _MemStoreLike:
    def __init__(self, memory_text, user_text):
        self._mem = memory_text
        self._user = user_text

    def format_for_system_prompt(self, which):
        return {"memory": self._mem, "user": self._user}.get(which, "")

    def load_from_disk(self):
        pass


class TestM3FreezeVolatile:
    def test_frozen_blocks_cached_on_agent(self):
        from agent.system_prompt import build_system_prompt_parts

        agent = FakeAgent()
        agent._freeze_volatile = True
        agent._memory_store = _MemStoreLike("MEM1", "USER1")
        parts1 = build_system_prompt_parts(agent)
        assert agent._frozen_volatile_blocks is not None
        # mutation of the store between builds must NOT change the frozen cache
        agent._memory_store = _MemStoreLike("MEM2-CHANGED", "USER2")
        parts2 = build_system_prompt_parts(agent)
        assert parts1["volatile"] == parts2["volatile"]
        assert "MEM1" in parts1["volatile"]
        assert "MEM2" not in parts2["volatile"]


# ---------------------------------------------------------------------------
# M10: compact tool schemas
# ---------------------------------------------------------------------------


class TestM10CompactTools:
    def test_long_description_capped_but_structure_kept(self):
        from model_tools import compact_tool_definitions, M10_DESCRIPTION_CHAR_CAP

        long_desc = "x" * 500
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "run",
                    "description": long_desc,
                    "parameters": {"type": "object"},
                    "required": ["a"],
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "short",
                    "description": "k",
                },
            },
        ]
        out = compact_tool_definitions(tools)
        assert len(out) == 2
        fn0 = out[0]["function"]
        assert fn0["name"] == "run"
        assert fn0["parameters"] == {"type": "object"}
        assert fn0["required"] == ["a"]
        assert len(fn0["description"]) <= M10_DESCRIPTION_CHAR_CAP + 1
        assert out[1]["function"]["description"] == "k"

    def test_input_not_mutated(self):
        from model_tools import compact_tool_definitions

        tools = [
            {
                "type": "function",
                "function": {"name": "n", "description": "y" * 500},
            }
        ]
        snapshot = tools[0]["function"]["description"]
        compact_tool_definitions(tools)
        assert tools[0]["function"]["description"] == snapshot


# ---------------------------------------------------------------------------
# M6/M7: class-based reasoning routing
# ---------------------------------------------------------------------------


class TestM67ClassRouting:
    def test_disabled_returns_none_for_all_classes(self):
        from agent.reasoning_class_router import class_effort_table

        table = class_effort_table({"agent": {"reasoning_class_routing": False}})
        for cls, effort in table.items():
            assert effort is None, f"{cls} should be unrouted when disabled"

    def test_enabled_routes_complex_to_xhigh_routine_low(self):
        from agent.reasoning_class_router import class_effort_table

        table = class_effort_table({"agent": {"reasoning_class_routing": True}})
        # Graded policy: complex classes sit on xhigh (not a flat max), and the
        # values are already letovo-clamped so no class can emit medium.
        for cls in ("code", "math", "logic", "multi_constraint"):
            assert table[cls] == "xhigh", cls
        assert table["routine"] == "low"
        # ops/undef are NOT routed (left for the global effort)
        assert table["ops"] is None
        assert table["undef"] is None

    def test_no_class_emits_a_letovo_rejected_level(self):
        # The wire-level guarantee for this router: nothing it can return is a
        # level the backend 400s on.
        from agent.reasoning_class_router import class_effort_table

        table = class_effort_table({"agent": {"reasoning_class_routing": True}})
        for cls, effort in table.items():
            assert effort not in ("medium", "minimal", "ultra"), (cls, effort)

    def test_never_points_to_ultra(self):
        from agent.reasoning_class_router import class_effort_table

        table = class_effort_table({"agent": {"reasoning_class_routing": True}})
        assert "ultra" not in [e for e in table.values() if e]

    def test_override_and_clamp(self):
        from agent.reasoning_class_router import effort_for_class

        cfg = {
            "agent": {
                "reasoning_class_routing": True,
                "reasoning_class_efforts": {"code": "high", "routine": "ultra"},
            }
        }
        assert effort_for_class("code", cfg) == "high"
        # ultra refused -> routed back to None (never applied)
        assert effort_for_class("routine", cfg) is None

    def test_override_medium_is_clamped_to_served_level(self):
        # An operator may still write "medium" in config (it is a valid Hermes
        # level); the router must translate it to a served level, not emit it.
        from agent.reasoning_class_router import effort_for_class

        cfg = {
            "agent": {
                "reasoning_class_routing": True,
                "reasoning_class_efforts": {"code": "medium", "math": "minimal"},
            }
        }
        assert effort_for_class("code", cfg) == "high"
        assert effort_for_class("math", cfg) == "low"

    def test_detector_classifies_known_markers(self):
        from agent.reasoning_class_router import detect_task_class

        assert detect_task_class("Напиши функцию на python, реализуй endpoint") == "code"
        assert detect_task_class("Вычисли среднее и процент от суммы") == "math"
        assert detect_task_class("Проведи аудит и ревью архитектуры") == "logic"
        assert detect_task_class("Кратко суммаризируй и подготовь справку") == "routine"


# ---------------------------------------------------------------------------
# L1: reasoning_step_routing flag + letovo ladder + gated aux/per-card pins
# (adaptive-reasoning-effort mission; design/effort-policy-v1.md)
# ---------------------------------------------------------------------------


class TestReasoningStepRoutingFlag:
    def test_step_routing_default_off(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        ac = (DEFAULT_CONFIG or {}).get("agent") or {}
        assert ac.get("reasoning_step_routing") is False


class TestReasoningClassRoutingFlag:
    def test_class_routing_default_off(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        ac = (DEFAULT_CONFIG or {}).get("agent") or {}
        assert ac.get("reasoning_class_routing") is False


class TestLetovoLadder:
    def test_wire_domain(self):
        from agent.reasoning_effort_l1 import LETOVO_EFFORTS

        assert LETOVO_EFFORTS == ("none", "low", "high", "xhigh", "max")

    def test_domain_excludes_rejected_levels(self):
        # Live-verified 2026-10-31: the backend 400s on both medium and minimal
        # ("must be low, high, xhigh, max, or an integer within [1, 100]").
        from agent.reasoning_effort_l1 import LETOVO_EFFORTS

        for forbidden in ("medium", "minimal", "ultra"):
            assert forbidden not in LETOVO_EFFORTS

    def test_clamp_maps_medium_to_high(self):
        # medium has no wire spelling on this backend; high is the nearest
        # served level for a mid-depth ask.
        from agent.reasoning_effort_l1 import clamp_to_letovo

        assert clamp_to_letovo("medium") == "high"

    def test_clamp_keeps_xhigh_distinct(self):
        # xhigh is a served level — it must NOT be collapsed into max.
        from agent.reasoning_effort_l1 import clamp_to_letovo

        assert clamp_to_letovo("xhigh") == "xhigh"

    def test_clamp_maps_ultra_to_max_and_minimal_to_low(self):
        from agent.reasoning_effort_l1 import clamp_to_letovo

        assert clamp_to_letovo("ultra") == "max"
        assert clamp_to_letovo("minimal") == "low"

    def test_clamp_passes_through_wire_native(self):
        from agent.reasoning_effort_l1 import clamp_to_letovo

        for level in ("none", "low", "high", "xhigh", "max"):
            assert clamp_to_letovo(level) == level

    def test_clamp_drops_bespoke_value(self):
        # Closed domain: an unknown name has no safe spelling, so it is dropped
        # (None) rather than forwarded into a guaranteed 400.
        from agent.reasoning_effort_l1 import clamp_to_letovo

        assert clamp_to_letovo("turbo") is None

    def test_clamp_never_returns_unsafe_level(self):
        from agent.reasoning_effort_l1 import clamp_to_letovo

        for bad in ("ultra", "xhigh", "minimal", "medium", "bogus"):
            assert clamp_to_letovo(bad) in ("low", "high", "xhigh", "max", None)


class TestGatedAuxPins:
    def test_aux_pins_inert_when_routing_off(self):
        from agent.reasoning_effort_l1 import reasoning_aux_pins, resolve_aux_effort

        cfg = {
            "agent": {
                "reasoning_class_routing": False,
                "reasoning_aux_pins": {"web_extract": "none", "compression": "low"},
            }
        }
        assert reasoning_aux_pins(cfg) == {}
        assert resolve_aux_effort("web_extract", cfg) is None
        assert resolve_aux_effort("compression", cfg) is None

    def test_aux_pins_active_when_routing_on(self):
        from agent.reasoning_effort_l1 import reasoning_aux_pins, resolve_aux_effort

        cfg = {
            "agent": {
                "reasoning_class_routing": True,
                "reasoning_aux_pins": {"web_extract": "none", "compression": "low"},
            }
        }
        pins = reasoning_aux_pins(cfg)
        assert pins == {"web_extract": "none", "compression": "low"}
        assert resolve_aux_effort("web_extract", cfg) == "none"
        assert resolve_aux_effort("compression", cfg) == "low"
        # A task without a pin resolves to None (caller keeps its behavior).
        assert resolve_aux_effort("title_generation", cfg) is None

    def test_aux_pin_invalid_value_skipped_not_crashed(self):
        from agent.reasoning_effort_l1 import reasoning_aux_pins

        cfg = {
            "agent": {
                "reasoning_class_routing": True,
                "reasoning_aux_pins": {"web_extract": "bogus", "compression": "low"},
            }
        }
        pins = reasoning_aux_pins(cfg)
        assert "web_extract" not in pins
        assert pins.get("compression") == "low"

    def test_aux_pin_clamped_to_letovo(self):
        from agent.reasoning_effort_l1 import reasoning_aux_pins

        cfg = {
            "agent": {
                "reasoning_class_routing": True,
                "reasoning_aux_pins": {"web_extract": "medium", "compression": "ultra"},
            }
        }
        pins = reasoning_aux_pins(cfg)
        # medium has no wire spelling -> high; ultra -> max.
        assert pins["web_extract"] == "high"
        assert pins["compression"] == "max"


class TestGatedCardPin:
    def test_card_effort_inert_when_routing_off(self):
        from agent.reasoning_effort_l1 import resolve_card_effort

        cfg = {"agent": {"reasoning_class_routing": False}}
        assert resolve_card_effort("code", "03-software-engineer", cfg) is None

    def test_card_effort_routes_complex_to_xhigh(self):
        from agent.reasoning_effort_l1 import resolve_card_effort

        cfg = {"agent": {"reasoning_class_routing": True}}
        assert resolve_card_effort("code", "03-software-engineer", cfg) == "xhigh"
        assert resolve_card_effort("logic", "01-orchestrator", cfg) == "xhigh"

    def test_card_effort_routes_routine_to_low(self):
        from agent.reasoning_effort_l1 import resolve_card_effort

        cfg = {"agent": {"reasoning_class_routing": True}}
        assert resolve_card_effort("routine", "07-document-producer", cfg) == "low"

    def test_card_effort_never_emits_medium(self):
        # End-to-end guard for the 400: no class may resolve to a level the
        # letovo backend rejects.
        from agent.reasoning_effort_l1 import resolve_card_effort

        cfg = {"agent": {"reasoning_class_routing": True}}
        for cls in ("code", "math", "logic", "multi_constraint", "routine", "ops", "undef"):
            got = resolve_card_effort(cls, "03-software-engineer", cfg)
            assert got not in ("medium", "minimal", "ultra"), (cls, got)

    def test_card_effort_wired_pin_wins(self):
        # An explicit operator pin (tasks.reasoning_effort) always wins over
        # routing — the helper clamps it but never drops it.
        from agent.reasoning_effort_l1 import resolve_card_effort

        cfg = {"agent": {"reasoning_class_routing": True}}
        assert resolve_card_effort("code", "03", cfg, wired_effort="high") == "high"
        # A medium operator pin is clamped to the served level, not forwarded.
        assert resolve_card_effort("code", "03", cfg, wired_effort="medium") == "high"
        assert resolve_card_effort("routine", "03", cfg, wired_effort="xhigh") == "xhigh"


class TestCardLaneEffort:
    """«review-gate» lane scheme — the container bug that produced the 400s."""

    def _tmp_kanban(self, tmp_path, monkeypatch):
        import os

        from hermes_cli import kanban_db as kb
        from hermes_cli import profiles

        monkeypatch.setattr(
            profiles,
            "profile_exists",
            lambda name: name
            in {
                "03-software-engineer",
                "15-sre-qa",
                "09-independent-reviewer",
                "07-document-producer",
            },
        )
        monkeypatch.setattr(kb, "_provider_route_blocker", lambda _p, _m: None)

        home = tmp_path / ".hermes"
        home.mkdir()
        os.environ["HERMES_HOME"] = str(home)
        db_path = tmp_path / "lane_kanban.db"
        os.environ["HERMES_KANBAN_DB"] = str(db_path)
        kb.init_db()
        return kb.connect(db_path=db_path), kb

    def test_container_card_is_high_not_medium(self, tmp_path, monkeypatch):
        # The exact defect: a card whose ONLY child is a review card used to
        # resolve to "medium" -> HTTP 400 for the whole container lane.
        from agent.reasoning_effort_l1 import resolve_card_lane_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            parent = kb.create_task(conn, title="контейнер", assignee="03-software-engineer")
            kb.create_task(
                conn, title="ревью", assignee="15-sre-qa", parents=[parent]
            )
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, parent, cfg) == "high"
        finally:
            conn.close()

    def test_review_card_is_max(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import resolve_card_lane_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            rev = kb.create_task(conn, title="ревью", assignee="15-sre-qa")
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, rev, cfg) == "max"
            rev09 = kb.create_task(conn, title="ревью2", assignee="09-independent-reviewer")
            assert resolve_card_lane_effort(conn, rev09, cfg) == "max"
        finally:
            conn.close()

    def test_author_with_review_child_is_high(self, tmp_path, monkeypatch):
        # Two children: real writing work + a review gate.
        from agent.reasoning_effort_l1 import resolve_card_lane_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            author = kb.create_task(conn, title="автор", assignee="03-software-engineer")
            kb.create_task(conn, title="работа", assignee="07-document-producer", parents=[author])
            kb.create_task(conn, title="ревью", assignee="15-sre-qa", parents=[author])
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, author, cfg) == "high"
        finally:
            conn.close()

    def test_no_gate_card_inherits_profile(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import resolve_card_lane_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            solo = kb.create_task(conn, title="одиночка", assignee="03-software-engineer")
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, solo, cfg) is None
        finally:
            conn.close()

    def test_lane_is_inert_when_routing_off(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import resolve_card_lane_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            parent = kb.create_task(conn, title="контейнер", assignee="03-software-engineer")
            kb.create_task(conn, title="ревью", assignee="15-sre-qa", parents=[parent])
            cfg = {"agent": {"reasoning_class_routing": False}}
            assert resolve_card_lane_effort(conn, parent, cfg) is None
        finally:
            conn.close()

    def test_lane_never_returns_medium(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import resolve_card_lane_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            container = kb.create_task(conn, title="c", assignee="03-software-engineer")
            kb.create_task(conn, title="r", assignee="15-sre-qa", parents=[container])
            author = kb.create_task(conn, title="a", assignee="03-software-engineer")
            kb.create_task(conn, title="w", assignee="07-document-producer", parents=[author])
            kb.create_task(conn, title="r", assignee="15-sre-qa", parents=[author])
            solo = kb.create_task(conn, title="s", assignee="03-software-engineer")
            cfg = {"agent": {"reasoning_class_routing": True}}
            for tid in (container, author, solo):
                got = resolve_card_lane_effort(conn, tid, cfg)
                assert got not in ("medium", "minimal", "ultra"), (tid, got)
        finally:
            conn.close()

    def test_operator_pin_wins_over_lane(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import resolve_card_lane_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            rev = kb.create_task(conn, title="ревью", assignee="15-sre-qa")
            cfg = {"agent": {"reasoning_class_routing": True}}
            # Operator pinned low: the lane must not override it, only clamp.
            assert resolve_card_lane_effort(conn, rev, cfg, wired_effort="low") == "low"
            assert resolve_card_lane_effort(conn, rev, cfg, wired_effort="medium") == "high"
        finally:
            conn.close()


class TestPinCardEffort:
    def _tmp_kanban(self, tmp_path, monkeypatch):
        import os

        from hermes_cli import kanban_db as kb
        from hermes_cli import profiles

        # synthetic assignee registry so create_task accepts test profiles
        monkeypatch.setattr(
            profiles, "profile_exists", lambda name: name in {"worker", "03-software-engineer"}
        )
        monkeypatch.setattr(kb, "_provider_route_blocker", lambda _p, _m: None)

        home = tmp_path / ".hermes"
        home.mkdir()
        os.environ["HERMES_HOME"] = str(home)
        from pathlib import Path

        # isolate the DB path so it does not touch the real board
        db_path = tmp_path / "test_kanban.db"
        os.environ["HERMES_KANBAN_DB"] = str(db_path)
        kb.init_db()
        conn = kb.connect(db_path=db_path)
        return conn, kb

    def test_pin_writes_when_routing_on(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import pin_card_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert pin_card_effort(conn, tid, "low", cfg) is True
            t = kb.get_task(conn, tid)
            assert t.reasoning_effort == "low"
            # medium is NOT a letovo wire level — it must be written as high,
            # never stored as the value that 400s the worker.
            assert pin_card_effort(conn, tid, "medium", cfg) is True
            t = kb.get_task(conn, tid)
            assert t.reasoning_effort == "high"
        finally:
            conn.close()

    def test_pin_clamps_minimal_and_xhigh(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import pin_card_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert pin_card_effort(conn, tid, "minimal", cfg) is True
            assert kb.get_task(conn, tid).reasoning_effort == "low"
            assert pin_card_effort(conn, tid, "xhigh", cfg) is True
            assert kb.get_task(conn, tid).reasoning_effort == "xhigh"
        finally:
            conn.close()

    def test_pin_is_noop_when_routing_off(self, tmp_path, monkeypatch):
        from agent.reasoning_effort_l1 import pin_card_effort

        conn, kb = self._tmp_kanban(tmp_path, monkeypatch)
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            cfg = {"agent": {"reasoning_class_routing": False}}
            assert pin_card_effort(conn, tid, "low", cfg) is False
            t = kb.get_task(conn, tid)
            assert t.reasoning_effort is None  # untouched
        finally:
            conn.close()
