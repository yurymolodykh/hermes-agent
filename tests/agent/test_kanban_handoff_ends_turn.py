"""OPS-16: the tool round itself ends the turn once the worker handed the card off.

The end-to-end shape of the bug: the worker calls ``kanban_complete``, the tool round
returns normally, the model keeps issuing tool calls, the iteration budget drains and the
run is finally booked ``timed_out`` on an already-``done`` card. This drives the real
``run_tool_round`` to prove the round now breaks instead of continuing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import _assistant_tool_call_dict
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from run_agent import AIAgent


def _tool_call(raw_id, n, name="read_file"):
    return SimpleNamespace(
        id=raw_id, type="function",
        function=SimpleNamespace(name=name, arguments=f'{{"path": "{n}"}}'),
    )


class _Compressor:
    """Minimal stand-in for the per-agent compressor: never compresses, no usage yet."""
    awaiting_real_usage_after_compression = False
    last_prompt_tokens = 0

    def should_compress(self, tokens):
        return False


def _agent():
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id, agent.tools = "nous", "m", "s1", []
    agent.api_mode = "chat_completions"
    agent._base_url = "https://example.invalid/v1"
    agent.valid_tool_names = {"read_file"}
    agent.log_prefix = ""
    agent.quiet_mode, agent.verbose_logging = True, False
    agent._invalid_tool_retries = agent._invalid_json_retries = 0
    agent._stream_needs_break = False
    agent.stream_delta_callback = None
    agent._tool_guardrail_halt_decision = None
    agent._incremental_persistence_failed = False
    agent.context_compressor = _Compressor()
    agent.compression_enabled = False
    agent._safe_print = lambda *a, **k: None
    agent._execute_tool_calls = lambda *a, **k: None
    agent._flush_messages_to_session_db = lambda *a, **k: True
    return agent


@pytest.fixture(autouse=True)
def _no_metrics(monkeypatch):
    import hermes_cli.observability.shared_metrics_model as metrics
    monkeypatch.setattr(metrics, "record_tool_call_quality", lambda *a, **k: None)


def _board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return kbc.connect()


def _round(agent, messages):
    import agent.turn_tool_round as round_module
    return round_module.run_tool_round(
        agent, assistant_message=SimpleNamespace(content="", tool_calls=[_tool_call("call_0", "a")]),
        finish_reason="tool_calls", messages=messages, conversation_history=[], api_call_count=1,
        effective_task_id="t", user_message="read", system_message="", active_system_prompt="",
        compression_attempts=0, max_compression_attempts=1, final_response=None, failed=False,
        _turn_exit_reason=None, truncated_tool_call_retries=0, current_turn_user_idx=0,
    )


def test_tool_round_breaks_after_the_card_was_completed(tmp_path, monkeypatch):
    with _board(tmp_path, monkeypatch) as conn:
        tid = kb.create_task(conn, title="closed", assignee="coder")
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        run_id = kb._current_run_id(conn, tid)
        assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id) is True

        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

        messages = []
        verdict = _round(_agent(), messages)

    assert verdict.action == "break"
    assert verdict._turn_exit_reason == "kanban_handoff_complete"
    # The turn closes with a visible assistant row, not a dangling tool tail.
    assert messages and messages[-1]["role"] == "assistant"
    assert messages[-1]["content"].strip()


def test_tool_round_continues_during_an_open_run(tmp_path, monkeypatch):
    with _board(tmp_path, monkeypatch) as conn:
        tid = kb.create_task(conn, title="live", assignee="coder")
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        run_id = kb._current_run_id(conn, tid)

        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

        verdict = _round(_agent(), [])

    # A card that is genuinely in progress keeps working: the fix must not end live runs.
    assert verdict.action == "continue"


def test_tool_round_is_untouched_outside_a_worker(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    verdict = _round(_agent(), [])
    assert verdict.action == "continue"
