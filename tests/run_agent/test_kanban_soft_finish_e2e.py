"""End-to-end: the soft-finish directive reaches a REAL ``run_conversation``.

The unit tests in ``tests/agent/test_kanban_soft_finish.py`` exercise the
policy module directly. This file drives the production loop
(``AIAgent.run_conversation``) with a stubbed transport and a real board row,
which is what the card's acceptance criterion #1 asks for: the threshold must
fire inside an actual worker run, not only in a unit test.

It also pins the regression contract (acceptance #3) end-to-end: a short
routine card never sees the directive.
"""

from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from run_agent import AIAgent

# A stable fragment of agent.kanban_soft_finish.SOFT_FINISH_DIRECTIVE. The
# directive is the only mid-run text that carries this marker, so its presence
# in an API payload proves the threshold armed inside the real loop.
_DIRECTIVE_MARKER = "kanban budget"


def _response(content="", tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model",
        usage=None,
    )


def _tool_call(name="terminal", arguments="{}", call_id="c1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real board DB with one running, bound card for the worker.

    Every inherited kanban env var is cleared FIRST: this test file is itself
    frequently executed inside a dispatcher worker (``HERMES_KANBAN_DB`` etc.
    already set), and leaking that pin would make the fixture claim cards on
    the LIVE board instead of its temp one.
    """
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_STOP_NUDGE",
    ):
        monkeypatch.delenv(var, raising=False)

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import kanban_db as kb

    kb.init_db()
    conn = kb.connect()
    monkeypatch.setattr(kb, "_validate_assignee_profile", lambda a: a)
    tid = kb.create_task(conn, title="live soft finish", assignee="worker")
    task = kb.claim_task(conn, tid)
    assert task is not None
    run_id = task.current_run_id

    db_path = str(kb.kanban_db_path())
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", db_path)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")

    # The arming fences: dispatcher envelope is env-based (set above), the
    # owner context is "not a delegate child / cron", and the live-card probe
    # is a real SQL read against the row we just created.
    import agent.kanban_stop as ks
    import agent.delegation_context as dc

    ks._LIVE_RUNNING_CACHE.clear()
    monkeypatch.setattr(dc, "is_dispatcher_owned_worker_context", lambda: True)

    yield SimpleNamespace(conn=conn, tid=tid, run_id=run_id, kb=kb, db_path=db_path)
    conn.close()


_TERMINAL_TOOL = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "run a shell command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}


def _make_agent(monkeypatch, max_iterations: int, session_id: str) -> AIAgent:
    # ``init_agent`` authenticates a dispatcher worker to its claimed run at
    # CONSTRUCTION time. In production the dispatcher has already created the
    # run, so the bind succeeds; here the agent session id does not exist yet,
    # so construct with the binding vars unset and bind explicitly afterwards
    # (see ``_bind``), exactly like the dispatcher → worker handoff.
    _binding_vars = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID")
    _saved = {k: os.environ.pop(k) for k in _binding_vars if k in os.environ}
    try:
        with (
            patch("run_agent.get_tool_definitions", return_value=[_TERMINAL_TOOL]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            agent = AIAgent(
                session_id=session_id,
                api_key="test-key",
                base_url="https://example.invalid/v1",
                provider="openai-compat",
                model="test/model",
                max_iterations=max_iterations,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
    finally:
        for k, v in _saved.items():
            os.environ[k] = v
    agent._cached_system_prompt = "stable test prompt"
    agent._session_db = None
    agent._session_json_enabled = False
    agent.save_trajectories = False
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *_a, **_kw: None
    agent._save_trajectory = lambda *_a, **_kw: None
    return agent


def _bind(board, agent):
    assert board.kb.bind_worker_session(
        board.conn, board.tid, board.run_id, "worker", agent.session_id
    )


def _tool_env(agent, monkeypatch):
    """Tool execution is irrelevant here — record the calls, return a result."""
    calls = []

    def _exec(_assistant_message, messages, *a, **kw):
        calls.append(1)
        for tc in _assistant_message.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "name": tc.function.name,
                    "tool_call_id": tc.id,
                    "content": "exit 0",
                }
            )
        return None

    monkeypatch.setattr(agent, "_execute_tool_calls", _exec, raising=False)
    monkeypatch.setattr(agent, "_execute_tool_calls_sequential", _exec, raising=False)
    return calls


def test_directive_fires_inside_a_real_conversation(board, monkeypatch):
    """Acceptance #1: instrumented log line from an actual loop iteration."""
    agent = _make_agent(monkeypatch, max_iterations=100, session_id="soft-live-1")
    _bind(board, agent)
    _tool_env(agent, monkeypatch)

    seen_directive = {"step": None, "text": None}
    steps = {"n": 0}

    def model_call(api_kwargs):
        steps["n"] += 1
        # The directive is appended to the newest tool result, so by the time
        # the step-80 request is built it must be inside the request payload.
        blob = json.dumps(api_kwargs.get("messages") or [], default=str)
        if _DIRECTIVE_MARKER in blob and seen_directive["step"] is None:
            seen_directive["step"] = steps["n"]
            seen_directive["text"] = blob
        return _response(tool_calls=[_tool_call(call_id=f"c{steps['n']}")],
                         finish_reason="tool_calls")

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = lambda *a, **kw: "summary"

    result = agent.run_conversation("work the card")

    # 80% of 100 = step 80; the directive is injected before that request.
    assert seen_directive["step"] is not None, "directive never reached the model"
    assert 78 <= seen_directive["step"] <= 82, seen_directive["step"]
    assert "kanban_complete" in seen_directive["text"]
    # One-shot: exactly one occurrence in the whole transcript.
    hits = [
        m for m in result["messages"]
        if isinstance(m.get("content"), str) and "SOFT FINISH" in m["content"].upper()
    ]
    assert len(hits) == 1


def test_short_card_never_gets_the_directive(board, monkeypatch):
    """Acceptance #3 end-to-end: a 30-step routine card is untouched."""
    agent = _make_agent(monkeypatch, max_iterations=30, session_id="soft-live-2")
    _bind(board, agent)
    _tool_env(agent, monkeypatch)

    steps = {"n": 0}

    def model_call(_api_kwargs):
        steps["n"] += 1
        return _response(tool_calls=[_tool_call(call_id=f"c{steps['n']}")],
                         finish_reason="tool_calls")

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = lambda *a, **kw: "summary"

    result = agent.run_conversation("routine card")

    assert not any(
        isinstance(m.get("content"), str) and "SOFT FINISH" in m["content"].upper()
        for m in result["messages"]
    )
    assert agent._kanban_soft_finish_injected is False


def test_grace_expiry_closes_the_card_done_with_its_progress(board, monkeypatch):
    """Acceptance #1, second half: no kanban_complete in time -> closed done."""
    from agent import kanban_soft_finish as soft

    agent = _make_agent(monkeypatch, max_iterations=100, session_id="soft-live-3")
    _bind(board, agent)
    _tool_env(agent, monkeypatch)
    monkeypatch.setattr(soft, "soft_finish_grace_steps", lambda *_a, **_kw: 2)

    steps = {"n": 0}

    def model_call(_api_kwargs):
        steps["n"] += 1
        return _response(tool_calls=[_tool_call(call_id=f"c{steps['n']}")],
                         finish_reason="tool_calls")

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = lambda *a, **kw: "summary"

    result = agent.run_conversation("work the card")

    row = board.conn.execute(
        "SELECT status, result FROM tasks WHERE id = ?", (board.tid,)
    ).fetchone()
    assert row["status"] == "done", f"card stayed {row['status']}"
    run = board.conn.execute(
        "SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (board.run_id,)
    ).fetchone()
    assert run["outcome"] == "completed"
    assert run["summary"], "the forced close must carry the worker's progress"
    meta = json.loads(run["metadata"] or "{}")
    assert meta.get("soft_finish_forced") is True, meta
    assert meta.get("forced_close_reason") == "soft_finish_grace_expired"
    assert meta.get("budget_max") == 100
    assert agent._kanban_soft_finish_forced is True
    assert result["turn_exit_reason"] == "kanban_soft_finish_forced"

    events = [
        json.loads(r["payload"] or "{}")
        for r in board.conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (board.tid,),
        )
    ]
    assert events, "no completed event recorded"
