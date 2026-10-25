"""Regression: session reasoning-override is cleared after run_agent turns.

The RGZ trigger pins the *session* to ``reasoning=max`` via
``apply_rgz_session_override`` for the trigger turn. On a long-lived session
(e.g. PWA/yadisk_bridge) the override would otherwise persist and every later
ordinary request kept resolving to ``max`` — the «залипание» defect (wg
t_9d4720da).

The guarantee is: ``AIAgent.run_conversation`` releases the session override
in its OUTER ``finally`` (next to ``release_agent_max``), so it runs no matter
how the turn exits. These tests drive a real ``AIAgent`` through all three
exit classes and assert the session override is gone after each call:

1. **success** — the conversation loop returns a normal result dict;
2. **exception** — the conversation loop raises; the exception propagates but
   ``reasoning_override`` must not survive;
3. **early return** — ``run_conversation`` returns BEFORE the conversation
   loop runs (the ``_tool_resume`` cached-final short-circuit at
   ``run_agent.run_conversation``), which is the earliest non-exception exit.

The override lives on the owning ``GatewayRunner``'s session state
(``_session_state(session_key).conversation.reasoning_override``), exactly the
field ``release_rgz_session_override`` clears via
``runner._set_session_reasoning_override(session_key, None)``. We reach it
through the module-level ``agent.rgz_reasoning._gateway_runner_ref`` seam,
which in a standalone pytest process has no live gateway — so we patch the
seam with a fake runner mirroring the real ``SessionState``/``ConversationState``
shape.

The test must FAIL on a build where the finally-release is absent (the
override survives the turn) and PASS with the cleanup wired in.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.rgz_reasoning import MAX_REASONING_CONFIG, apply_rgz_session_override
from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Fake runner + agent scaffold (mirror the real gateway run.py shapes)
# ---------------------------------------------------------------------------


class _FakeConversationState:
    """Minimal stand-in for gateway.session_state.ConversationState.

    Only the field the cleanup touches is needed; the acceptance asserts the
    SAME attribute path the production ``_set_session_reasoning_override``
    writes (``state.conversation.reasoning_override``).
    """

    def __init__(self) -> None:
        self.reasoning_override = None


class _FakeSessionState:
    def __init__(self) -> None:
        self.conversation = _FakeConversationState()


class _FakeRunner:
    """GatewayRunner stub with the real ``_set_session_reasoning_override``
    write path against per-session conversation state."""

    def __init__(self, session_key: str) -> None:
        self._session_key = session_key
        self._sessions = {session_key: _FakeSessionState()}
        self.set_calls = []

    def _sessions_map(self):
        return self._sessions

    def _session_state(self, session_key: str):
        return self._sessions_map().get(session_key) or _FakeSessionState()

    def _set_session_reasoning_override(self, session_key: str, cfg):
        self.set_calls.append((session_key, dict(cfg) if cfg is not None else None))
        state = self._session_state(session_key)
        state.conversation.reasoning_override = (
            None if cfg is None else dict(cfg)
        )

    @property
    def reasoning_override(self):
        return self._session_state(self._session_key).conversation.reasoning_override


class _LeaseDB:
    """Durable-session shim for the early-return path (pattern borrowed from
    tests/run_agent/test_cross_process_turn_lease.py)."""

    def __init__(self, history):
        self._history = list(history)

    def get_session(self, session_id):
        return {"id": session_id}

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def get_messages_as_conversation(self, session_id, **kwargs):
        return list(self._history)

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        pass


def _make_agent(session_key: str, *, session_db=None):
    """A concrete AIAgent built with __new__ (no network/init side effects),
    exactly like the existing run_agent lease tests."""
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = f"sid-{session_key}"
    agent.platform = "cli"
    agent.model = "test-model"
    agent._session_db = session_db
    agent._session_db_created = True
    agent._persist_disabled = False
    agent._parent_session_id = None
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent.log_prefix = ""
    agent._vprint = lambda *a, **k: None
    agent.status_callback = None
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._interrupt_hard_requested = None
    agent._pending_redirect = None
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._session_turn_lease_refresh_interval = 600.0
    agent._gateway_session_key = session_key
    return agent


# ---------------------------------------------------------------------------
# Success / exception scenarios — conversation loop outcome is patched
# ---------------------------------------------------------------------------


def test_session_override_cleared_after_successful_turn():
    """A normal turn (loop returns a result) releases the pinned override."""
    session_key = "agent:main:yadisk_bridge:dm:regr-ok"
    runner = _FakeRunner(session_key)
    agent = _make_agent(session_key)

    # Pin the session as the RGZ trigger turn does.
    assert apply_rgz_session_override(runner, session_key, "дай гигантский план") is True
    assert runner.reasoning_override == MAX_REASONING_CONFIG

    def fake_run(*_a, **_k):
        return {
            "final_response": "done",
            "messages": [{"role": "assistant", "content": "done"}],
            "failed": False,
        }

    with patch("agent.conversation_loop.run_conversation", fake_run), patch(
        "agent.rgz_reasoning._gateway_runner_ref", return_value=lambda: runner,
    ):
        result = agent.run_conversation("обычный запрос без триггера", conversation_history=[], task_id="t")

    assert result["final_response"] == "done"
    # The override must be released after the turn.
    assert runner.reasoning_override is None
    assert runner.set_calls[-1] == (session_key, None)


def test_session_override_cleared_after_exception():
    """Even when the loop raises, the outer finally clears the override."""
    session_key = "agent:main:yadisk_bridge:dm:regr-exc"
    runner = _FakeRunner(session_key)
    agent = _make_agent(session_key)

    assert apply_rgz_session_override(runner, session_key, "гигантская задача") is True
    assert runner.reasoning_override == MAX_REASONING_CONFIG

    def boom(*_a, **_k):
        raise RuntimeError("turn crashed")

    with patch("agent.conversation_loop.run_conversation", boom), patch(
        "agent.rgz_reasoning._gateway_runner_ref", return_value=lambda: runner,
    ):
        with pytest.raises(RuntimeError, match="turn crashed"):
            agent.run_conversation("проверка", conversation_history=[], task_id="t")

    # The exception propagated, but the override must still be released.
    assert runner.reasoning_override is None
    assert runner.set_calls[-1] == (session_key, None)


def test_session_override_cleared_after_early_return():
    """`run_conversation` can return before the loop runs (the _tool_resume
    cached-final short-circuit). The finally release must still fire."""
    from agent.suspension import ValidatedToolResume

    session_key = "agent:main:yadisk_bridge:dm:regr-early"
    # Seed a completed resume: the tail is exactly what the resume replayed.
    resume_result = '{"answers":[{"answer":"y","question_id":"q1"}]}'
    history = [
        {"role": "user", "content": "question", "_row_id": 1},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "clarify-1",
                "type": "function",
                "function": {"name": "clarify", "arguments": "{}"},
            }],
            "_row_id": 2,
        },
        {
            "role": "tool",
            "tool_call_id": "clarify-1",
            "tool_name": "clarify",
            "content": resume_result,
            "_row_id": 3,
        },
        {"role": "assistant", "content": "continued", "_row_id": 4},
    ]
    db = _LeaseDB(history)
    agent = _make_agent(session_key, session_db=db)
    runner = _FakeRunner(session_key)

    resume = ValidatedToolResume(
        session_id=f"sid-{session_key}",
        kind="question_set",
        tool_name="clarify",
        tool_call_id="clarify-1",
        result={"answers": [{"answer": "y", "question_id": "q1"}]},
        token_fingerprint="sha256:test",
    )

    assert apply_rgz_session_override(runner, session_key, "гигантская миссия") is True
    assert runner.reasoning_override == MAX_REASONING_CONFIG

    # Spy: if the early cached-final return does NOT short-circuit before the
    # loop, this fires — the test then no longer proves the early-exit class.
    loop_called = []

    def must_not_run(*_a, **_k):
        loop_called.append(True)
        return {"final_response": "loop-ran", "messages": [], "failed": False}

    with patch("agent.conversation_loop.run_conversation", must_not_run), patch(
        "agent.rgz_reasoning._gateway_runner_ref", return_value=lambda: runner,
    ):
        result = agent.run_conversation(
            None,
            conversation_history=[],
            task_id="t",
            _tool_resume=resume,
        )

    # The early return must be a genuine one, not a fall-through to the loop.
    assert loop_called == []
    assert result["final_response"] == "continued"
    assert runner.reasoning_override is None
    assert runner.set_calls[-1] == (session_key, None)
