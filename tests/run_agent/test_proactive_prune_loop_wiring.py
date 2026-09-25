"""Behavioral tests for the post-tool proactive tool-result prune wiring.

The conversation loop's post-tool gate now has a prune arm inside the
``elif agent.compression_enabled`` branch: when full compression does NOT
fire (the usual case on a large-window model), the deterministic no-LLM
prune gets one shot per tool iteration, committing only when the engine
returns a NEW list object with a non-zero prune count.

These tests drive ``run_conversation()`` through real tool iterations and pin:
- the prune is consulted when compression stands down;
- a committed prune replaces ``messages`` for subsequent iterations;
- a no-op (input object returned) commits nothing;
- a compressor WITHOUT the method (plugin engine predating the hook /
  SimpleNamespace test double) does not raise — getattr-guarded;
- a raising prune is swallowed (debug log), never fails the turn.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _tool_call(i: int):
    return SimpleNamespace(
        id=f"call_{i}",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _tool_response(i: int):
    msg = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call(i)],
    )
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _stop_response():
    msg = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _quiet_compressor() -> MagicMock:
    """A compressor that never demands full compression.

    ``should_compress`` False routes the post-tool gate into the ``elif``
    branch where the proactive prune arm lives.  ``should_compress_info``
    reports unblocked (no block reason) so the overflow warning stays quiet.
    """
    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 500_000
    compressor.context_length = 1_000_000
    compressor.last_prompt_tokens = 120_000
    compressor.should_compress.return_value = False
    compressor.should_compress_info.return_value = (False, None)
    compressor.should_defer_preflight_to_real_usage.return_value = True
    compressor.get_active_compression_failure_cooldown.return_value = None
    return compressor


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=10,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a._disable_streaming = True
    a.tool_delay = 0
    a.save_trajectories = False
    a.compression_enabled = True
    a.context_compressor = _quiet_compressor()
    return a


def _run_tool_loop(agent, n_tool_iterations: int):
    responses = [_tool_response(i) for i in range(n_tool_iterations)]
    responses.append(_stop_response())
    agent.client.chat.completions.create.side_effect = responses

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        result = agent.run_conversation("do a lot of tool work")

    return result


@pytest.mark.parametrize("outcome", ["commit", "disabled", "reclaim_rejected", "db_failure"])
def test_real_prune_skill_reload_and_task_isolation(agent, tmp_path, monkeypatch, outcome):
    """Full skill view -> actual loop/SQLite prune -> full reload, not a stub."""
    from agent.context_compressor import ContextCompressor
    from hermes_state import SessionDB
    from tools.skills_tool import _skill_view_with_bump, reset_skill_view_dedup

    skill_dir = tmp_path / "home" / "skills" / "prune-reload-demo"
    skill_dir.mkdir(parents=True)
    body = "# Procedure\n" + "Preserve this detailed operating instruction.\n" * 700
    (skill_dir / "SKILL.md").write_text(
        "---\nname: prune-reload-demo\ndescription: Prune reload regression.\n---\n" + body,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    task = "pruned-task"
    other_task = "unrelated-task"

    def view(task_id):
        return json.loads(_skill_view_with_bump({"name": "prune-reload-demo"}, task_id=task_id))

    reset_skill_view_dedup(task)
    reset_skill_view_dedup(other_task)
    db = SessionDB(db_path=tmp_path / "prune.db")
    try:
        first = view(task)
        assert body in first["content"]
        assert body in view(other_task)["content"]
        assert view(task).get("dedup") is True
        history = [{"role": "user", "content": "Follow the procedure."}]
        for index in range(8):
            name = "skill_view" if index == 0 else "web_search"
            history.append({
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": f"old_{index}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps({"name": "prune-reload-demo"})
                        if index == 0 else '{"query":"prior work"}',
                    },
                }],
            })
            history.append({
                "role": "tool", "tool_call_id": f"old_{index}",
                "content": json.dumps(first) if index == 0 else f"result {index}",
            })
        db.create_session(agent.session_id, source="test")
        db.append_messages_batch(agent.session_id, history)
        with patch("agent.context_compressor.get_model_context_length", return_value=1_000_000):
            compressor = ContextCompressor(
                model="test", quiet_mode=True, threshold_percent=0.50,
                protect_first_n=2, protect_last_n=4,
                proactive_prune_tokens=1, proactive_prune_min_result_chars=8_000,
                proactive_prune_min_reclaim_tokens=4_096,
            )
        compressor._session_db = db
        compressor._session_id = agent.session_id
        compressor.last_prompt_tokens = 120_000
        agent.context_compressor = compressor
        if outcome == "disabled":
            compressor.proactive_prune_tokens = 0
        elif outcome == "reclaim_rejected":
            compressor.proactive_prune_min_reclaim_tokens = 1_000_000
        elif outcome == "db_failure":
            monkeypatch.setattr(db, "archive_and_compact", MagicMock(side_effect=RuntimeError("disk full")))

        agent.client.chat.completions.create.side_effect = [_tool_response(0), _stop_response()]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.handle_function_call", return_value='{"ok":true}'),
        ):
            result = agent.run_conversation("continue the work", conversation_history=history, task_id=task)
        assert result["completed"] is True
        row = next(m for m in result["messages"] if m.get("tool_call_id") == "old_0")
        durable = next(m for m in db.get_messages_as_conversation(agent.session_id)
                       if m.get("tool_call_id") == "old_0")
        reloaded = view(task)
        if outcome == "commit":
            assert "[SKILL_PRUNED:" in row["content"]
            assert durable["content"] == row["content"]
            assert body in reloaded.get("content", ""), reloaded
        else:
            assert row["content"] == json.dumps(first)
            assert durable["content"] == json.dumps(first)
            assert reloaded.get("dedup") is True
            assert compressor._proactive_prune_rearm_tokens == 0
        assert view(other_task).get("dedup") is True
    finally:
        reset_skill_view_dedup(task)
        reset_skill_view_dedup(other_task)
        db.close()


@pytest.mark.parametrize("explicit_task", [True, False])
def test_full_compaction_reload_uses_known_task_scope(agent, tmp_path, monkeypatch, explicit_task):
    from agent.context_compressor import ContextCompressor
    from tools.skills_tool import _skill_view_with_bump, reset_skill_view_dedup

    skill = tmp_path / "home" / "skills" / "full-reload-demo"
    skill.mkdir(parents=True)
    body = "# Procedure\n" + "Detailed content to reload.\n" * 500
    (skill / "SKILL.md").write_text(
        "---\nname: full-reload-demo\ndescription: Full compaction test.\n---\n" + body,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    task, other = "full-active", "full-other"

    def view(t):
        return json.loads(_skill_view_with_bump({"name": "full-reload-demo"}, task_id=t))

    try:
        assert body in view(task)["content"]
        assert body in view(other)["content"]
        with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
            compressor = ContextCompressor(
                model="test", quiet_mode=True, protect_first_n=2, protect_last_n=4,
            )
        agent.context_compressor = compressor
        agent._current_task_id = task
        messages = [{"role": "system", "content": "system"},
                    {"role": "user", "content": "start"}]
        for index in range(12):
            messages.append({"role": "user" if index % 2 == 0 else "assistant",
                             "content": body if index == 2 else f"work {index}"})
        # Replace only the unavailable LLM summary, not compressor or lifecycle.
        with patch.object(compressor, "_generate_summary", return_value="Earlier work summary."):
            returned, _ = agent._compress_context(
                messages, "system", approx_tokens=50_000,
                task_id=task if explicit_task else None,
            )
        assert returned is not messages
        assert len(returned) < len(messages)
        assert not any(body in str(m.get("content", "")) for m in returned)
        assert body in view(task).get("content", "")
        assert view(other).get("dedup") is True
    finally:
        reset_skill_view_dedup(task)
        reset_skill_view_dedup(other)


class TestProactivePruneLoopWiring:
    def test_full_compression_preempts_proactive_prune(self, agent):
        agent.context_compressor.should_compress.return_value = True

        def _compress(messages, system_message, **_kwargs):
            return [dict(m) for m in messages], system_message

        with (
            patch.object(agent, "_compress_context", side_effect=_compress) as compress,
            patch(
                "agent.conversation_loop.conversation_history_after_compression",
                return_value=[],
            ),
        ):
            result = _run_tool_loop(agent, n_tool_iterations=1)

        assert result["completed"] is True
        compress.assert_called_once()
        agent.context_compressor.prune_tool_results_only.assert_not_called()

    def test_prune_consulted_when_compression_stands_down(self, agent):
        calls = []

        def _prune(messages, current_tokens=None):
            calls.append(current_tokens)
            return messages, 0  # no-op contract: input object back

        agent.context_compressor.prune_tool_results_only = _prune
        result = _run_tool_loop(agent, n_tool_iterations=3)
        assert result["completed"] is True
        assert len(calls) == 3  # one shot per tool iteration
        assert all(t == 120_000 for t in calls)  # fed the real usage reading

    def test_committed_prune_replaces_messages(self, agent):
        marker = "[old tool output pruned]"

        def _prune(messages, current_tokens=None):
            pruned = [dict(m) for m in messages]
            changed = 0
            for m in pruned:
                if m.get("role") == "tool" and m.get("content") != marker:
                    m["content"] = marker
                    changed += 1
            if not changed:
                return messages, 0
            return pruned, changed

        agent.context_compressor.prune_tool_results_only = _prune
        result = _run_tool_loop(agent, n_tool_iterations=2)
        assert result["completed"] is True
        tool_rows = [m for m in result["messages"] if m.get("role") == "tool"]
        assert tool_rows, "expected tool rows in the final transcript"
        assert all(m["content"] == marker for m in tool_rows)

    def test_noop_input_object_commits_nothing(self, agent):
        """Engine returns the INPUT object with a (bogus) non-zero count —
        the caller's ``result is not input`` gate must refuse the commit."""
        def _prune(messages, current_tokens=None):
            return messages, 5  # lies about count but returns input object

        agent.context_compressor.prune_tool_results_only = _prune
        result = _run_tool_loop(agent, n_tool_iterations=2)
        assert result["completed"] is True
        tool_rows = [m for m in result["messages"] if m.get("role") == "tool"]
        # tool output may be wrapped in an untrusted_tool_result envelope —
        # assert the original payload survived un-pruned.
        assert all('"ok": true' in m["content"] for m in tool_rows)

    def test_engine_without_method_does_not_raise(self, agent):
        """Plugin engines predating the hook / minimal doubles lack the
        method entirely — the getattr guard treats absence as a no-op."""
        compressor = SimpleNamespace(
            protect_first_n=3,
            protect_last_n=20,
            threshold_tokens=500_000,
            context_length=1_000_000,
            last_prompt_tokens=120_000,
            should_compress=lambda _t: False,
            should_defer_preflight_to_real_usage=lambda _t: True,
            get_active_compression_failure_cooldown=lambda: None,
        )
        agent.context_compressor = compressor
        result = _run_tool_loop(agent, n_tool_iterations=2)
        assert result["completed"] is True

    def test_raising_prune_is_swallowed(self, agent):
        def _prune(messages, current_tokens=None):
            raise RuntimeError("boom")

        agent.context_compressor.prune_tool_results_only = _prune
        result = _run_tool_loop(agent, n_tool_iterations=2)
        assert result["completed"] is True
        tool_rows = [m for m in result["messages"] if m.get("role") == "tool"]
        # tool output may be wrapped in an untrusted_tool_result envelope —
        # assert the original payload survived un-pruned.
        assert all('"ok": true' in m["content"] for m in tool_rows)
