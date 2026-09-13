"""Opt-in tool suspension stops a turn without forging a tool result."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.suspension import ToolSuspension
from agent.tool_executor import execute_tool_calls_segmented, execute_tool_calls_sequential
from run_agent import AIAgent
from tools.clarify_tool import clarify_tool


def _make_agent(tmp_path: Path) -> AIAgent:
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=[
                {
                    "type": "function",
                    "function": {
                        "name": "clarify",
                        "description": "test tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", tmp_path),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    return agent


def _call(name: str, call_id: str):
    arguments = '{"question": "Pick?", "choices": ["A", "B"]}' if name == "clarify" else "{}"
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_clarify_callback_can_opt_in_to_generic_suspension():
    suspension = ToolSuspension(
        kind="question_set",
        payload={"question_set_id": "qs-1", "revision": 1},
    )

    result = clarify_tool(
        question="Pick?",
        choices=["A", "B"],
        callback=lambda *_args, **_kwargs: suspension,
    )

    assert result is suspension


def test_ordinary_clarify_callback_keeps_existing_json_result():
    result = clarify_tool(
        question="Pick?",
        choices=["A", "B"],
        callback=lambda *_args, **_kwargs: "A (Recommended)",
    )

    assert result == (
        '{"question": "Pick?", "choices_offered": ["A", "B"], '
        '"user_response": "A"}'
    )


def test_sequential_executor_stops_at_suspension_without_tool_result(tmp_path):
    agent = _make_agent(tmp_path)
    suspension = ToolSuspension(
        kind="question_set",
        payload={"question_set_id": "qs-1", "revision": 1},
    )
    agent.clarify_callback = lambda *_args, **_kwargs: suspension
    durable_assistant_row = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "clarify-1", "type": "function"}],
    }
    messages = [durable_assistant_row]

    with patch("run_agent.handle_function_call") as later_dispatch:
        execute_tool_calls_sequential(
            agent,
            SimpleNamespace(tool_calls=[_call("clarify", "clarify-1"), _call("later", "later-1")]),
            messages,
            "task",
        )

    # The executor neither forges nor flushes a role=tool row. The assistant
    # tool-call row, persisted by the conversation loop before execution,
    # remains the only recovery boundary.
    assert messages == [durable_assistant_row]
    agent._flush_messages_to_session_db.assert_not_called()
    assert agent._tool_suspension == {
        "kind": "question_set",
        "payload": {"question_set_id": "qs-1", "revision": 1},
        "tool_name": "clarify",
        "tool_call_id": "clarify-1",
    }
    later_dispatch.assert_not_called()


def test_segmented_dispatch_stops_before_later_parallel_segment(tmp_path):
    agent = _make_agent(tmp_path)
    agent.clarify_callback = lambda *_args, **_kwargs: ToolSuspension(
        kind="question_set",
        payload={"question_set_id": "qs-2"},
    )
    clarify_call = _call("clarify", "clarify-2")
    later_call = _call("terminal", "terminal-1")
    assistant = SimpleNamespace(tool_calls=[clarify_call, later_call])

    with patch("run_agent.handle_function_call") as later_dispatch:
        execute_tool_calls_segmented(
            agent,
            assistant,
            [],
            "task",
            segments=[
                ("sequential", [clarify_call]),
                ("parallel", [later_call]),
            ],
        )

    assert agent._tool_suspension["tool_call_id"] == "clarify-2"
    later_dispatch.assert_not_called()


def test_suspension_snapshot_ignores_post_construction_mutation():
    # The frozen dataclass must not hold a mutable alias of the caller's
    # payload: mutating the source dict after construction must not change
    # the projection (the validation snapshot must be deep-copied).
    payload = {"question_set_id": "qs-1", "revision": 1}
    suspension = ToolSuspension(kind="question_set", payload=payload)
    payload["revision"] = 999  # mutate the caller's dict
    payload["session_key"] = "sekret"  # add a secret after the fact
    result = suspension.to_result(tool_name="clarify", tool_call_id="call-1")
    assert result["payload"] == {"question_set_id": "qs-1", "revision": 1}
    assert "session_key" not in result["payload"]


def test_privately_routed_suspension_keeps_secret_in_internal_boundary():
    # The internal boundary (agent._tool_suspension) may carry the private
    # routing envelope, but the observer-facing projection must not leak it.
    from agent.suspension import public_suspension_projection

    suspension = ToolSuspension(
        kind="question_set",
        payload={"question_set_id": "qs-1", "session_key": "sekret-routing-token"},
    )
    internal = suspension.to_result(tool_name="clarify", tool_call_id="call-1")
    assert internal["payload"]["session_key"] == "sekret-routing-token"
    public = public_suspension_projection(internal)
    assert "session_key" not in public["payload"]
    assert "tool_call_id" not in public


def test_observer_result_does_not_leak_session_key(tmp_path):
    # The terminal post_tool_call observer must receive the bounded public
    # shape, never the private routing envelope.
    import model_tools
    from agent.tool_executor import execute_tool_calls_sequential

    captured = {}

    def _capture_hook(**kwargs):
        captured["result"] = kwargs.get("result")

    agent = _make_agent(tmp_path)
    agent.clarify_callback = lambda *_a, **_k: ToolSuspension(
        kind="question_set",
        payload={"question_set_id": "qs-1", "session_key": "sekret-routing-token"},
    )
    durable_row = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "clarify-1", "type": "function"}],
    }
    with (
        patch.object(model_tools, "_emit_post_tool_call_hook", side_effect=_capture_hook),
        patch("run_agent.handle_function_call"),
    ):
        execute_tool_calls_sequential(
            agent,
            SimpleNamespace(tool_calls=[_call("clarify", "clarify-1")]),
            [durable_row],
            "task",
        )

    assert captured["result"]["payload"].get("session_key") is None
    assert "tool_call_id" not in captured["result"]
