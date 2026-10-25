"""Unit tests for the RGZ reasoning mechanical guarantee (agent/rgz_reasoning).

Locks in the owner decision 2026-10-20 contract:

1. Trigger detection: an inbound message containing «гигантск…» + one of
   (план|задача|миссия|проект) is an RGZ plan trigger; the session must be
   pinned to reasoning=max.
2. apply_rgz_session_override sets the session override (same field as
   /reasoning --session) and returns True only when a trigger fired.
3. ensure_max_for_agent (the kanban_create hook) raises a live agent's
   reasoning_config to max, snapshots the pre-raise config, and refuses to
   override an explicit "thinking off" ({enabled: False}).
4. release_agent_max restores the pre-raise config and clears the snapshot —
   the raise never leaks into a later turn of a reused cached agent, and an
   agent that was never raised is a harmless no-op.
5. Wiring: the tool-executor chokepoint fires the raise on kanban_create but
   leaves other tools untouched ("не ниже high" is preserved, 11-fast-answers
   and the L1 execution pinner are untouched by design).

The hook and release are pure/mechanical — no LLM involved.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.rgz_reasoning import (
    MAX_REASONING_CONFIG,
    apply_rgz_session_override,
    ensure_max_for_agent,
    release_agent_max,
    rgz_trigger_detected,
)


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

def test_trigger_detected_giant_plan():
    assert rgz_trigger_detected("Составь гигантский план внедрения") is True


def test_trigger_detected_giant_task():
    assert rgz_trigger_detected("Это гигантская задача, нужен декомпоз") is True


def test_trigger_detected_giant_mission_project():
    assert rgz_trigger_detected("новая гигантская миссия начинается") is True
    assert rgz_trigger_detected("работаем над гигантским проектом") is True


def test_trigger_detected_plain_message_not_trigger():
    assert rgz_trigger_detected("Обнови отчёт по прибыли") is False


def test_trigger_detected_empty_and_none():
    assert rgz_trigger_detected("") is False
    assert rgz_trigger_detected(None) is False


def test_trigger_detected_case_and_flexion_insensitive():
    # «ГИГАНТСКИЙ ПЛАН» matches case-insensitively; the adjective flexes
    # (гигантской) but the NOUN must be the exact base form per the fast-lane
    # marker contract (план|задача|миссия|проект) — so the genitive
    # «гигантской задачи» intentionally does NOT match.
    assert rgz_trigger_detected("ГИГАНТСКИЙ ПЛАН подать сегодня") is True
    assert rgz_trigger_detected("решаем за гигантской задачей") is False


def test_trigger_detected_multimodal_list_with_attachment():
    """RGZ trigger must fire on a message that also carries an attachment.

    api_server normalizes a message with an attached file/image into a *list*
    of parts ``[{type:text,...}, {type:image_url/file,...}]``. The RGZ phrase
    lives in the text part and must still pin the session to max — this is the
    attachment regression the owner reported.
    """
    multimodal = [
        {"type": "text", "text": "Составь гигантский план внедрения"},
        {"type": "file", "text": "[документ приложен]", "file": {"id": "f1"}},
    ]
    assert rgz_trigger_detected(multimodal) is True

    image_msg = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "это гигантская задача, нужен декомпоз"},
    ]
    assert rgz_trigger_detected(image_msg) is True


def test_trigger_detected_multimodal_list_no_phrase():
    multimodal = [
        {"type": "text", "text": "Обнови отчёт по прибыли"},
        {"type": "image_url", "image_url": {"url": "https://x/i.png"}},
    ]
    assert rgz_trigger_detected(multimodal) is False


# ---------------------------------------------------------------------------
# apply_rgz_session_override
# ---------------------------------------------------------------------------

def _fake_runner():
    return SimpleNamespace(
        _session_reasoning_overrides={},
        _set_session_reasoning_override_calls=[],
    )


class _FakeRunner:
    """Runner stub recording ``_set_session_reasoning_override`` calls."""

    def __init__(self):
        self.overrides = {}
        self.calls = []

    def _set_session_reasoning_override(self, key, cfg):
        self.calls.append((key, dict(cfg) if cfg is not None else None))
        if cfg is None:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = dict(cfg)


def test_apply_override_pins_session_on_trigger():
    runner = _FakeRunner()
    assert apply_rgz_session_override(runner, "sess-1", "подготовь гигантский план") is True
    assert runner.overrides == {"sess-1": MAX_REASONING_CONFIG}


def test_apply_override_noop_without_trigger():
    runner = _FakeRunner()
    assert apply_rgz_session_override(runner, "sess-1", "просто привет") is False
    assert runner.calls == []


def test_apply_override_noop_without_session_key():
    runner = _FakeRunner()
    assert apply_rgz_session_override(runner, None, "гигантский план") is False
    assert apply_rgz_session_override(runner, "", "гигантский план") is False
    assert runner.calls == []


def test_apply_override_graceful_when_setter_missing():
    # Runner without the setter must degrade to False, never raise.
    bare = SimpleNamespace()
    assert apply_rgz_session_override(bare, "sess-9", "гигантская задача") is False


# ---------------------------------------------------------------------------
# ensure_max_for_agent (kanban_create hook — raise)
# ---------------------------------------------------------------------------

def _make_agent(reasoning_config=None, session_id="sess-rgz"):
    # Deliberately WITHOUT a pre-set _rgz_saved_reasoning_config attribute —
    # a real agent has no raise state until ensure_max_for_agent snapshots it.
    return SimpleNamespace(
        reasonings={"saved": None},
        reasoning_config=reasoning_config,
        session_id=session_id,
    )


def _assert_raised_state(agent, snapshot):
    # The snapshot attr exists with the expected value while the raise is active.
    assert getattr(agent, "_rgz_saved_reasoning_config", object()) == snapshot


def test_raise_sets_max_and_snapshots_prior():
    agent = _make_agent(reasoning_config={"enabled": True, "effort": "high"})
    assert ensure_max_for_agent(agent) is True
    assert agent.reasoning_config == MAX_REASONING_CONFIG
    _assert_raised_state(agent, {"enabled": True, "effort": "high"})


def test_raise_idempotent_when_agent_none():
    assert ensure_max_for_agent(None) is False


def test_raise_noop_when_already_max():
    agent = _make_agent(reasoning_config=dict(MAX_REASONING_CONFIG))
    assert ensure_max_for_agent(agent) is True
    assert agent.reasoning_config == MAX_REASONING_CONFIG
    # No snapshot needed for an already-max agent — attr stays absent.
    assert not hasattr(agent, "_rgz_saved_reasoning_config")


def test_raise_never_overrides_thinking_off():
    agent = _make_agent(reasoning_config={"enabled": False})
    assert ensure_max_for_agent(agent) is False
    assert agent.reasoning_config == {"enabled": False}
    assert not hasattr(agent, "_rgz_saved_reasoning_config")


def test_raise_snapshot_only_first_time():
    # Two kanban_create calls in one run: the second does not clobber the
    # original snapshot, so release restores the ORIGINAL config.
    agent = _make_agent(reasoning_config={"enabled": True, "effort": "medium"})
    ensure_max_for_agent(agent)
    agent.reasoning_config = {"enabled": True, "effort": "xhigh"}  # mid-run drift
    ensure_max_for_agent(agent)
    _assert_raised_state(agent, {"enabled": True, "effort": "medium"})
    assert agent.reasoning_config == MAX_REASONING_CONFIG


# ---------------------------------------------------------------------------
# release_agent_max (end-of-run restore)
# ---------------------------------------------------------------------------

def test_release_restores_prior_config():
    agent = _make_agent(reasoning_config={"enabled": True, "effort": "high"})
    ensure_max_for_agent(agent)
    assert release_agent_max(agent) is True
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert not hasattr(agent, "_rgz_saved_reasoning_config")


def test_release_noop_when_never_raised():
    agent = _make_agent(reasoning_config={"enabled": True, "effort": "high"})
    assert release_agent_max(agent) is False
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_release_noop_when_agent_none():
    assert release_agent_max(None) is False


# ---------------------------------------------------------------------------
# Wiring: the tool-executor chokepoint fires the raise on kanban_create
# ---------------------------------------------------------------------------

class _FakeWiringAgent:
    """Minimal agent that satisfies _run_agent_tool_execution_middleware.

    ``quiet_mode=True`` skips the user-visible display branch in
    ``_begin_tool_execution`` so the wiring test needs no display attrs.
    Deliberately does NOT pre-set ``_rgz_saved_reasoning_config`` — a real
    agent has no raise state until ensure_max_for_agent snapshots it, and the
    sentinel-based active-check depends on that absence.
    """

    def __init__(self, reasoning_config=None):
        self.session_id = "sess-wiring"
        self.quiet_mode = True
        self.reasoning_config = reasoning_config
        self._tool_worker_threads = set()
        self._tool_worker_threads_lock = __import__("threading").Lock()
        self._interrupt_requested = False
        self._tool_guardrails = _NoGuardrails()
        self._turns_since_memory = 1
        self._iters_since_skill = 1
        self._touch_activity = lambda msg: None
        self._current_tool = None
        self.tool_progress_callback = None
        self.tool_start_callback = None
        self.tool_complete_callback = None


class _NoGuardrails:
    def before_call(self, function_name, final_args):
        return _Allow()


class _Allow:
    allows_execution = True


def test_wiring_kanban_create_raises_to_max(monkeypatch):
    from agent.tool_executor import _run_agent_tool_execution_middleware

    agent = _FakeWiringAgent(reasoning_config={"enabled": True, "effort": "high"})
    executed = []

    def _execute(next_args):
        executed.append(next_args)
        return '{"ok": true}'

    result = _run_agent_tool_execution_middleware(
        agent,
        function_name="kanban_create",
        function_args={"title": "t", "assignee": "a"},
        effective_task_id="task-1",
        tool_call_id="tc-1",
        execute=_execute,
        scope_block=None,
        display_index=1,
        middleware_trace=[],
    )
    assert executed == [{"title": "t", "assignee": "a"}]
    assert agent.reasoning_config == MAX_REASONING_CONFIG
    assert result.result == '{"ok": true}'


def test_wiring_other_tool_untouched(monkeypatch):
    from agent.tool_executor import _run_agent_tool_execution_middleware

    agent = _FakeWiringAgent(reasoning_config={"enabled": True, "effort": "high"})
    executed = []

    def _execute(next_args):
        executed.append(next_args)
        return "result"

    _run_agent_tool_execution_middleware(
        agent,
        function_name="web_search",
        function_args={"query": "q"},
        effective_task_id="task-1",
        tool_call_id="tc-1",
        execute=_execute,
        scope_block=None,
        display_index=1,
        middleware_trace=[],
    )
    assert executed == [{"query": "q"}]
    # Non-kanban_create tools never mutate the reasoning config.
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert not hasattr(agent, "_rgz_saved_reasoning_config")


def test_wiring_raise_survives_hook_failure(monkeypatch):
    """A failing hook must never break the tool call itself."""
    from agent.tool_executor import _run_agent_tool_execution_middleware

    agent = _FakeWiringAgent(reasoning_config={"enabled": True, "effort": "high"})
    executed = []

    def _boom(*args, **kwargs):
        raise RuntimeError("hook blew up")

    monkeypatch.setattr("agent.rgz_reasoning.ensure_max_for_agent", _boom)
    monkeypatch.setattr("agent.tool_executor.logger.debug", lambda *a, **k: None)

    def _execute(next_args):
        executed.append(next_args)
        return "tool-ran"

    result = _run_agent_tool_execution_middleware(
        agent,
        function_name="kanban_create",
        function_args={"title": "t"},
        effective_task_id="task-1",
        tool_call_id="tc-1",
        execute=_execute,
        scope_block=None,
        display_index=1,
        middleware_trace=[],
    )
    assert result.result == "tool-ran"
    assert executed == [{"title": "t"}]


def test_wiring_release_restores_after_run(monkeypatch):
    """End-to-end: raise on kanban_create, then release restores the config."""
    from agent.rgz_reasoning import release_agent_max

    agent = _FakeWiringAgent(reasoning_config={"enabled": True, "effort": "high"})
    ensure_max_for_agent(agent)
    assert agent.reasoning_config == MAX_REASONING_CONFIG
    assert release_agent_max(agent) is True
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
