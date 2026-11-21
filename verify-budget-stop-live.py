#!/usr/bin/env python3
"""Live probe: an iteration-budget stop keeps the card's work (t_af262aaf).

Drives the REAL production path against a REAL board DB and a REAL git
repository, with an artificially small budget (60 instead of the profile's 300):

  * 85% notice  -> agent.turn_iteration_prep._maybe_inject_iteration_budget_warning
  * budget stop -> agent.turn_finalizer.finalize_turn (checkpoint + board record)
  * restart     -> hermes_cli.kanban_db.build_worker_context for the next attempt

Acceptance (a) commit with content, (b) resume from the saved place,
(c) status text names commit/what is left, (d) no WIP commit when the card fits.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1] if (Path(__file__).resolve().parent.name == "agent") else None
if str(Path("/home/hermes/.hermes/kanban/workspaces/t_af262aaf")) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

OK = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  LEAK {label}  {detail}")


def read_text(path: Path) -> str:
    """File contents, or "" when the file is absent (a red baseline has no commit)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=False)


def main() -> int:
    # This probe itself runs inside a dispatcher worker, so the inherited envelope
    # would fence the temp board read-only. Build the board as a plain owner process
    # and set the worker envelope back only for the stage that must act as one.
    for _k in [k for k in os.environ if k.startswith("HERMES_KANBAN_")]:
        os.environ.pop(_k, None)
    root = Path(tempfile.mkdtemp(prefix="probe-budget-")).resolve()
    home = root / "home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    (home / "config.yaml").write_text("agent:\n  max_turns: 60\n", encoding="utf-8")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    db_path = home / "kanban.db"
    kb.init_db(db_path)
    os.environ["HERMES_KANBAN_DB"] = str(db_path)
    conn = kbc.connect(db_path)

    # --- a card on its own git worktree-shaped branch ------------------------
    ws = (root / "ws").resolve()
    ws.mkdir(parents=True)
    git(ws, "init", "-q", "-b", "wt/t_probe_budget")
    git(ws, "config", "user.email", "probe@example.invalid")
    git(ws, "config", "user.name", "probe")
    (ws / "README.md").write_text("base\n", encoding="utf-8")
    git(ws, "add", "README.md")
    git(ws, "commit", "-q", "-m", "base")

    task_id = kb.create_task(
        conn, title="[probe] budget stop keeps the work", assignee="03-software-engineer",
        body="## Приёмка\n- a\n- b\n- c\n- d\n- e\n- f\n- g\n- h\n- i\n",
        workspace_kind="dir", workspace_path=str(ws),
        created_by="probe",
    )
    claimed = kb.claim_task(conn, task_id, claimer="probe:1")
    check("card is running and owns an open run", claimed is not None and claimed.status == "running",
          f"claimed={claimed}")
    run_id = kb._current_run_id(conn, task_id)

    # The dispatcher worker envelope (what makes a process a card worker).
    os.environ.update({
        "HERMES_KANBAN_TASK": task_id,
        "HERMES_KANBAN_RUN_ID": str(run_id),
        "HERMES_KANBAN_WORKSPACE": str(ws),
        "HERMES_PROFILE": "03-software-engineer",
        "HERMES_SESSION_SOURCE": "kanban",
    })

    # Work in progress: the shape of t_f3d28e37 (verified code, tests, no commit).
    (ws / "results.py").write_text("def probe():\n    return 25\n" * 120, encoding="utf-8")
    (ws / "test_results.py").write_text("assert True\n" * 80, encoding="utf-8")
    (ws / "PROGRESS.md").write_text(
        "# work\n\n- [x] hermes_acl_results.py\n- [ ] wire the loader\n- [ ] rerun the probe\n",
        encoding="utf-8",
    )
    head_before = git(ws, "rev-parse", "HEAD").stdout.strip()

    print("\n== 1. 85% notice parks the tree WITHOUT a branch commit ==")
    from agent.turn_iteration_prep import _maybe_inject_iteration_budget_warning

    messages = [{"role": "tool", "content": "tool output"}]
    agent85 = SimpleNamespace(
        _interrupt_requested=False,
        budget_warning_ratio=0.85,
        iteration_budget=SimpleNamespace(used=51, max_total=60, remaining=9),
        _iteration_budget_warning_injected=False,
        valid_tool_names=["kanban_complete", "kanban_block"],
        quiet_mode=True,
    )
    injected = _maybe_inject_iteration_budget_warning(agent85, messages)
    notice = messages[-1]["content"] if injected else ""
    ref = f"refs/hermes-wip/{task_id}"
    check("85% notice was injected", injected)
    check("notice names the parked ref", ref in notice, notice[-200:])
    check("the ref really exists", git(ws, "rev-parse", "--verify", ref).returncode == 0)
    check("85% did NOT create a branch commit",
          git(ws, "rev-parse", "HEAD").stdout.strip() == head_before)
    check("working tree still dirty (model keeps working)",
          git(ws, "status", "--porcelain").stdout.strip() != "")
    parked = git(ws, "show", f"{ref}:results.py").stdout
    check("parked ref carries the file bytes", "def probe():" in parked)

    print("\n== 2. budget stop (60/60) commits the work and records it ==")
    from agent.turn_finalizer import finalize_turn

    class _LimitAgent:
        max_iterations = 60
        quiet_mode = True
        model = "probe-model"
        provider = "probe-provider"
        base_url = ""
        session_id = "sess-probe"
        persisted_messages = None
        _response_was_previewed = False
        _handle_max_iterations_called = False
        _completion_explainer = False
        _tool_guardrail_halt_decision = None
        _interrupt_message = None
        _skill_nudge_interval = 0
        _iters_since_skill = 0
        valid_tool_names = []
        context_compressor = SimpleNamespace(last_prompt_tokens=0)

        def __init__(self):
            self.iteration_budget = SimpleNamespace(remaining=0, used=60, max_total=60)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "reasoning_tokens", "prompt_tokens",
                      "completion_tokens", "total_tokens"):
                setattr(self, f"session_{k}", 0)
            self.session_estimated_cost_usd = 0
            self.session_cost_status = "unknown"
            self.session_cost_source = "probe"

        def _handle_max_iterations(self, messages, api_call_count):
            self._handle_max_iterations_called = True
            return "summary from extra call"

        def _emit_status(self, *a, **k): pass
        def _emit_diagnostic_status(self, *a, **k): pass
        def _safe_print(self, *a, **k): pass
        def _save_trajectory(self, *a, **k): pass
        def _cleanup_task_resources(self, *a, **k): pass
        def _drop_trailing_empty_response_scaffolding(self, m): pass

        def _persist_session(self, messages, conversation_history):
            self.persisted_messages = list(messages)

        def _file_mutation_verifier_enabled(self): return False
        def _turn_completion_explainer_enabled(self): return False
        def _format_turn_completion_explanation(self, _r): return ""
        def _drain_pending_steer(self): return None
        def clear_interrupt(self): pass
        def _sync_external_memory_for_turn(self, **k): pass

    agent = _LimitAgent()
    with patch("hermes_cli.plugins.invoke_hook", lambda *a, **k: []):
        result = finalize_turn(
            agent, final_response=None, api_call_count=60, interrupted=False, failed=False,
            messages=[{"role": "user", "content": "task"}], conversation_history=[],
            effective_task_id="task", turn_id="turn", user_message="task",
            original_user_message="task", _should_review_memory=False,
            _turn_exit_reason="budget_exhausted",
        )

    log_subject = git(ws, "log", "-1", "--pretty=%s").stdout.strip()
    log_body = git(ws, "show", "--name-only", "--pretty=", "HEAD").stdout.split()
    wip_sha = git(ws, "rev-parse", "--short", "HEAD").stdout.strip()
    check("turn exit reason is the budget stop",
          result["turn_exit_reason"] == "max_iterations_reached(60/60)", str(result.get("turn_exit_reason")))
    check("a WIP commit landed on the card's branch", "WIP бюджет: 60/60" in log_subject, log_subject)
    check("the commit carries the work files",
          sorted(log_body) == ["PROGRESS.md", "results.py", "test_results.py"], str(log_body))
    check("working tree is clean after the stop",
          git(ws, "status", "--porcelain").stdout.strip() == "")
    committed_code = git(ws, "show", "HEAD:results.py").stdout
    check("committed bytes are the real content", committed_code.count("def probe():") == 120)

    task_row = conn.execute(
        "SELECT status, consecutive_failures, last_failure_error FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    err = task_row["last_failure_error"] or ""
    run_row = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'timed_out' "
        "ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    payload = json.loads(event["payload"]) if event else {}

    print(f"\n  board status={task_row['status']!r}  run={run_row['outcome']!r}")
    print(f"  last_failure_error: {err}")
    check("card did not stay 'running'", task_row["status"] != "running", task_row["status"])
    check("run closed as timed_out", run_row["outcome"] == "timed_out", run_row["outcome"])
    check("(c) text names the budget counters", "60/60" in err, err)
    check("(c) text names the commit", wip_sha in err, err)
    check("(c) text names what is left", "wire the loader" in err and "rerun the probe" in err, err)
    check("(c) text is not the bare give-up phrasing",
          "could not complete" not in err, err)
    check("event payload carries the stop_note",
          wip_sha in (payload.get("stop_note") or ""), str(payload))
    check("event payload keeps the counters",
          payload.get("budget_used") == 60 and payload.get("budget_max") == 60, str(payload))

    print("\n== 3. the next attempt resumes from the saved place ==")
    ctx = kb.build_worker_context(conn, task_id)
    check("(b) worker context names the stop and the commit",
          "остановлено по бюджету 60/60" in ctx and wip_sha in ctx)
    check("(b) worker context keeps the open items",
          "wire the loader" in ctx)
    check("(b) worker context names the previous attempt as timed_out",
          "timed_out" in ctx or "budget" in ctx)
    # A successor that starts from the branch tip sees the work, not an empty tree:
    # a fresh clone of the branch has the 1459-line-era files at their committed size.
    successor = (root / "successor").resolve()
    git(Path(successor.parent), "clone", "-q", "-b", "wt/t_probe_budget", str(ws), str(successor))
    check("(b) a successor checkout carries the work, not an empty tree",
          read_text(successor / "results.py").count("def probe():") == 120
          and (successor / "test_results.py").exists())
    check("(b) the successor's PROGRESS.md is the saved one",
          "wire the loader" in read_text(successor / "PROGRESS.md"))
    reclaim = kb.claim_task(conn, task_id, claimer="probe:2")
    check("(b) the card is retryable (claimed again from ready)",
          reclaim is not None and reclaim.status == "running", str(reclaim and reclaim.status))

    print("\n== 4. regression: a card that fits gets no WIP commit ==")
    ws2 = (root / "ws2").resolve()
    ws2.mkdir(parents=True)
    git(ws2, "init", "-q", "-b", "wt/t_fits")
    git(ws2, "config", "user.email", "probe@example.invalid")
    git(ws2, "config", "user.name", "probe")
    (ws2 / "base.py").write_text("x = 1\n", encoding="utf-8")
    git(ws2, "add", "base.py")
    git(ws2, "commit", "-q", "-m", "base")
    from agent.kanban_budget_checkpoint import checkpoint_wip, preserve_wip_ref

    clean = checkpoint_wip(reason="предел", used=60, max_total=60, workspace=ws2, task_id="t_fits")
    check("clean tree -> no checkpoint", (not clean.ok) and clean.files == [])
    check("clean tree -> no parked ref", preserve_wip_ref(workspace=ws2, task_id="t_fits") is None)
    subjects = git(ws2, "log", "--pretty=%s").stdout
    check("no WIP subject anywhere in history", "WIP бюджет" not in subjects, subjects)
    check("history still has exactly the one base commit",
          len(git(ws2, "rev-list", "HEAD").stdout.split()) == 1)

    # The normal completion path: work committed by the worker itself, then done.
    (ws2 / "done.py").write_text("done = True\n", encoding="utf-8")
    git(ws2, "add", "done.py")
    git(ws2, "commit", "-q", "-m", "feat: the actual work")
    after = checkpoint_wip(reason="предел", used=60, max_total=60, workspace=ws2, task_id="t_fits")
    check("card that committed its own work gets no extra commit", not after.ok)
    check("its history carries only its own commits",
          git(ws2, "log", "--pretty=%s").stdout.strip().splitlines()
          == ["feat: the actual work", "base"])

    print(f"\n=== ИТОГ {OK}/{OK + FAIL} OK, {FAIL} LEAK ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
