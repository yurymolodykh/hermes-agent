"""Proof artifact for t_ed744fd2 (П2 + acceptance #1 live evidence).

Run with the repo venv from the hermes-agent root:

    ./venv/Scripts/python.exe tests/manual/soft_finish_proof.py

It does three things against a TEMP board (never the live one):

A. P2 — measures WHY the pre-existing stop guards could not save a card:
   drives the real ``AIAgent.run_conversation`` with a worker that never calls
   a terminal kanban tool and has a small iteration budget, and records that
   the turn-end nudge builder is never reached (the run dies inside the loop).

B. Acceptance #1 — runs the same real loop with a large budget and captures
   the exact ``kanban soft finish: directive injected ...`` log line, the step
   at which the directive reached the model, and the forced close that turned
   what used to be ``timed_out`` into ``done`` with the worker's progress.

C. Acceptance #3 — a short routine card (30 steps) never sees the directive.

Output is printed to stdout; redirect it to keep a durable copy.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

# Isolate completely from the live board/config before importing anything.
_TMP_HOME = tempfile.mkdtemp(prefix="soft_finish_proof_")
for _var in (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_CLAIM_LOCK",
):
    os.environ.pop(_var, None)
os.environ["HERMES_HOME"] = _TMP_HOME

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

TOOL = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "run a shell command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    },
}


def _response(step):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id=f"c{step}",
                            type="function",
                            function=SimpleNamespace(name="terminal", arguments="{}"),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        model="proof/model",
        usage=None,
    )


def _setup_board():
    from hermes_cli import kanban_db as kb

    kb.init_db()
    conn = kb.connect()
    kb._validate_assignee_profile = lambda a: a  # temp home has no profiles
    return kb, conn


def _make_worker(kb, conn, max_iterations: int, session_id: str):
    import run_agent

    tid = kb.create_task(
        conn, title="proof card", assignee="worker", max_turns=max_iterations
    )
    task = kb.claim_task(conn, tid)
    os.environ.update(
        {
            "HERMES_KANBAN_TASK": tid,
            "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
            "HERMES_KANBAN_DB": str(kb.kanban_db_path()),
            "HERMES_KANBAN_WORKSPACE": _TMP_HOME,
            "HERMES_KANBAN_BOARD": "default",
            "HERMES_PROFILE": "worker",
            "HERMES_SESSION_SOURCE": "kanban",
        }
    )
    import agent.delegation_context as dc

    dc.is_dispatcher_owned_worker_context = lambda: True

    with (
        patch("run_agent.get_tool_definitions", return_value=[TOOL]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = run_agent.AIAgent(
            session_id=session_id,
            api_key="proof",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="proof/model",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    assert kb.bind_worker_session(conn, tid, task.current_run_id, "worker", agent.session_id)
    agent._cached_system_prompt = "proof prompt"
    agent._session_db = None
    agent._session_json_enabled = False
    agent.save_trajectories = False
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *a, **kw: None
    agent._save_trajectory = lambda *a, **kw: None

    def _exec(assistant_message, messages, *a, **kw):
        for tc in assistant_message.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "name": tc.function.name,
                    "tool_call_id": tc.id,
                    "content": "exit 0",
                }
            )

    agent._execute_tool_calls = _exec
    agent._execute_tool_calls_sequential = _exec
    agent._handle_max_iterations = lambda *a, **kw: "summary"
    return agent, tid, task.current_run_id


def part_a():
    print("\n" + "=" * 78)
    print("A. P2 — why the pre-existing stop guards could not save these cards")
    print("=" * 78)
    kb, conn = _setup_board()
    agent, tid, run_id = _make_worker(kb, conn, max_iterations=20, session_id="proof-a")

    import agent.kanban_soft_finish as soft
    from agent import kanban_stop as ks

    calls = {"turn_end_nudge_builder": 0}
    real_builder = ks.build_kanban_stop_nudge
    traced = lambda *a, **kw: (
        calls.__setitem__("turn_end_nudge_builder", calls["turn_end_nudge_builder"] + 1)
        or real_builder(*a, **kw)
    )
    agent_steps = {"n": 0}
    agent._interruptible_api_call = lambda kw: (
        agent_steps.__setitem__("n", agent_steps["n"] + 1) or _response(agent_steps["n"])
    )

    with patch.object(ks, "build_kanban_stop_nudge", traced):
        result = agent.run_conversation("work the card")

    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    run = conn.execute(
        "SELECT outcome, summary FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    print(f"   max_iterations          : 20")
    print(f"   api calls made          : {agent_steps['n']}")
    print(f"   turn_exit_reason        : {result['turn_exit_reason']}")
    print(f"   turn-end nudge builder  : {calls['turn_end_nudge_builder']} invocation(s)")
    print(f"   card status             : {row['status']}")
    print(f"   run outcome             : {run['outcome']}")
    print(f"   run summary             : {run['summary']}")
    print()
    print("   => the worker burned the whole budget INSIDE the loop; the turn-end")
    print("      stop guard (kanban_stop / verification_stop / pre_verify) is only")
    print("      reachable when a turn ENDS, so it could not fire. That is the")
    print("      defect this card fixes: the missing signal is mid-run, not turn-end.")
    conn.close()
    return calls["turn_end_nudge_builder"]


def part_b():
    print("\n" + "=" * 78)
    print("B. Acceptance #1 — the directive fires inside a REAL loop iteration")
    print("=" * 78)
    kb, conn = _setup_board()
    agent, tid, run_id = _make_worker(kb, conn, max_iterations=100, session_id="proof-b")

    steps = {"n": 0}
    seen = {"step": None, "excerpt": None}

    def model_call(kw):
        steps["n"] += 1
        blob = json.dumps(kw.get("messages") or [], default=str)
        if "kanban budget" in blob and seen["step"] is None:
            seen["step"] = steps["n"]
            i = blob.find("SYSTEM DIRECTIVE")
            seen["excerpt"] = blob[i : i + 220]
        return _response(steps["n"])

    agent._interruptible_api_call = model_call
    result = agent.run_conversation("work the card")

    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (tid,)).fetchone()
    run = conn.execute(
        "SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    meta = json.loads(run["metadata"] or "{}")
    events = [
        (r["kind"], json.loads(r["payload"] or "{}"))
        for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        )
    ]
    print(f"   budget                  : {agent.max_iterations} steps")
    print(f"   directive reached model : at API call #{seen['step']} "
          f"({seen['step'] / agent.max_iterations:.0%} of budget)")
    print(f"   directive excerpt       : {(seen['excerpt'] or '')[:140]}...")
    print(f"   api calls used          : {steps['n']}")
    print(f"   turn_exit_reason        : {result['turn_exit_reason']}")
    print(f"   card status             : {row['status']}   (was 'timed_out' before this card)")
    print(f"   run outcome             : {run['outcome']}")
    print(f"   forced-close metadata   : {json.dumps(meta, ensure_ascii=False)[:200]}")
    print(f"   run summary (progress)  : {(run['summary'] or '')[:160]}")
    print(f"   board events            : {[k for k, _ in events]}")
    assert row["status"] == "done", row["status"]
    assert run["outcome"] == "completed", run["outcome"]
    assert meta.get("soft_finish_forced") is True, meta
    conn.close()
    return seen["step"], steps["n"]


def part_c():
    print("\n" + "=" * 78)
    print("C. Acceptance #3 — a short routine card never sees the directive")
    print("=" * 78)
    kb, conn = _setup_board()
    agent, tid, run_id = _make_worker(kb, conn, max_iterations=30, session_id="proof-c")
    steps = {"n": 0}
    hits = {"n": 0}

    def model_call(kw):
        steps["n"] += 1
        if "kanban budget" in json.dumps(kw.get("messages") or [], default=str):
            hits["n"] += 1
        return _response(steps["n"])

    agent._interruptible_api_call = model_call
    result = agent.run_conversation("routine card")
    print(f"   budget                  : {agent.max_iterations} steps")
    print(f"   api calls used          : {steps['n']}")
    print(f"   directive sightings     : {hits['n']}")
    print(f"   latch set               : {agent._kanban_soft_finish_injected}")
    print(f"   turn_exit_reason        : {result['turn_exit_reason']}")
    assert hits["n"] == 0, hits["n"]
    assert agent._kanban_soft_finish_injected is False
    conn.close()


if __name__ == "__main__":
    print(f"temp HERMES_HOME: {_TMP_HOME}")
    nudge_calls = part_a()
    fired_step, used = part_b()
    part_c()
    print("\n" + "=" * 78)
    print("RESULT")
    print("=" * 78)
    print(
        f"   P2: turn-end nudge builder invocations on a budget-exhausted worker = {nudge_calls}"
    )
    print(f"   directive fired at step {fired_step} of {100} (80%), card closed done at step {used}")
    print("   30-step card: no directive (regression contract holds)")
    assert nudge_calls == 0, nudge_calls
    print("\nALL PROOF ASSERTIONS PASSED")
