"""Tests for the automatic budget split — the no-human fan-out that fires
when a worker exhausts its operation budget.

Covers three layers:

1. DB: kb.split_task_on_budget — children created + linked, root gated on
   the graph, run closed with outcome='budget_split', depth cap enforced.
2. Orchestrator: kanban_budget_split.run_budget_split with the LLM
   unavailable → deterministic 2-card fallback split still happens.
3. Goal loop: goals.run_kanban_goal_loop consults budget_split_fn before
   blocking and returns outcome='budget_split' on success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_budget_split as bsplit
from hermes_cli import goals


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Isolated HOME has no profiles; the assignee validator must not block.
    from hermes_cli import profiles as _profiles

    monkeypatch.setattr(_profiles, "profile_exists", lambda _n: True)
    kb.init_db()
    return home


def _create_ready(conn, title="big work", body="do the whole thing", assignee=None):
    return kb.create_task(conn, title=title, body=body, assignee=assignee)


def _claim(conn, task_id):
    task = kb.claim_task(conn, task_id)
    assert task is not None, "claim failed — task not ready?"
    return task


# ---------------------------------------------------------------------------
# 1. DB layer
# ---------------------------------------------------------------------------

def test_split_creates_children_and_gates_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_ready(conn)
        _claim(conn, tid)

        children = [
            {"title": "part A", "body": "first half", "parents": []},
            {"title": "part B", "body": "second half", "parents": [0]},
        ]
        child_ids = kb.split_task_on_budget(
            conn, tid, children=children, author="t", source="iteration_budget",
        )
    assert child_ids and len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root: waiting on the graph, claim released, failures untouched.
    assert root.status == "todo"
    assert root.claim_lock is None
    assert root.current_run_id is None
    # Children inherit assignee, workspace, tenant; parent-free one ready.
    assert c0.assignee == "01-orchestrator"  # inherited from the root card
    assert c0.status == "ready"
    assert c1.status == "todo"  # gated on c0
    # Root linked as child of every split child (waits for the graph).
    links = kb.connect().execute(
        "SELECT parent_id, child_id FROM task_links WHERE parent_id = ? OR child_id = ?",
        (tid, tid),
    ).fetchall()
    link_pairs = {(r["parent_id"], r["child_id"]) for r in links}
    assert (child_ids[0], tid) in link_pairs
    assert (child_ids[1], tid) in link_pairs


def test_split_closes_run_with_budget_split_outcome(kanban_home):
    with kb.connect() as conn:
        tid = _create_ready(conn)
        _claim(conn, tid)
        with kb.connect() as conn2:
            row = conn2.execute(
                "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
            ).fetchone()
        run_id = row["current_run_id"]
        assert run_id is not None

        child_ids = kb.split_task_on_budget(
            conn, tid,
            children=[{"title": "a", "body": "", "parents": []},
                      {"title": "b", "body": "", "parents": []}],
            author="t", source="goal_loop",
        )
        assert child_ids
        run = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert run["outcome"] == "budget_split"
        assert run["ended_at"] is not None


def test_split_depth_cap_refuses(kanban_home):
    with kb.connect() as conn:
        tid = _create_ready(conn)
        _claim(conn, tid)
        # First split at depth 0 → children at depth 1.
        first = kb.split_task_on_budget(
            conn, tid,
            children=[{"title": "a", "body": "", "parents": []},
                      {"title": "b", "body": "", "parents": []}],
            author="t", source="iteration_budget",
        )
        assert first
    with kb.connect() as conn:
        # A child (depth 1) is ready; claim and exhaust it with max_depth=1
        # → refused (already at cap).
        child = kb.connect().execute(
            "SELECT id FROM tasks WHERE title = 'a'",
        ).fetchone()
        cid = child["id"]
        got = kb.claim_task(conn, cid)
        assert got is not None
        res = kb.split_task_on_budget(
            conn, cid,
            children=[{"title": "x", "body": "", "parents": []},
                      {"title": "y", "body": "", "parents": []}],
            author="t", source="iteration_budget", max_depth=1,
        )
        assert res is None


def test_split_refuses_when_not_running(kanban_home):
    with kb.connect() as conn:
        tid = _create_ready(conn)  # stays ready, never claimed
        res = kb.split_task_on_budget(
            conn, tid,
            children=[{"title": "a", "body": "", "parents": []},
                      {"title": "b", "body": "", "parents": []}],
            author="t", source="iteration_budget",
        )
        assert res is None


def test_split_refuses_wrong_run_id(kanban_home):
    with kb.connect() as conn:
        tid = _create_ready(conn)
        _claim(conn, tid)
        res = kb.split_task_on_budget(
            conn, tid,
            children=[{"title": "a", "body": "", "parents": []},
                      {"title": "b", "body": "", "parents": []}],
            author="t", source="iteration_budget", expected_run_id=999999,
        )
        assert res is None


# ---------------------------------------------------------------------------
# 2. Orchestrator (fallback path, no LLM)
# ---------------------------------------------------------------------------

def test_run_budget_split_fallback_without_llm(kanban_home, monkeypatch):
    # Kill the aux LLM entirely → deterministic fallback must still split.
    import sys

    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)
    monkeypatch.setattr(bsplit, "_load_config", lambda: {})

    with kb.connect() as conn:
        tid = _create_ready(conn, body="acceptance: it works")
        _claim(conn, tid)

    res = bsplit.run_budget_split(tid, source="iteration_budget", cfg={})
    assert res.ok is True
    assert res.via == "fallback"
    assert res.child_ids and len(res.child_ids) == 2

    with kb.connect() as conn:
        c0 = kb.get_task(conn, res.child_ids[0])
        c1 = kb.get_task(conn, res.child_ids[1])
    assert c0.status == "ready"
    assert c1.status == "todo"  # serial fallback: 2 waits on 1
    assert "split 1/2" in c0.title
    assert "acceptance: it works" in c0.body


def test_run_budget_split_disabled_in_config(kanban_home, monkeypatch):
    monkeypatch.setattr(bsplit, "_load_config", lambda: {})
    with kb.connect() as conn:
        tid = _create_ready(conn)
        _claim(conn, tid)
    res = bsplit.run_budget_split(
        tid, source="iteration_budget", cfg={"kanban": {"budget_split": False}},
    )
    assert res.ok is False
    assert "disabled" in res.reason


# ---------------------------------------------------------------------------
# 3. Goal loop integration
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "continue"
        return v, f"scripted:{v}", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_goal_loop_splits_instead_of_blocking(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])

    class _Split:
        ok = True
        via = "fallback"
        reason = "split into 2 children via fallback"
        child_ids = ["c1", "c2"]

    calls = []

    def _split_fn(task_id, *, source, progress_note=""):
        calls.append((task_id, source, progress_note))
        return _Split()

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: "working...",
        task_status_fn=lambda: "running",
        block_fn=lambda r: pytest.fail("must not block when split succeeds"),
        max_turns=1,
        first_response="partial work done",
        budget_split_fn=_split_fn,
    )
    assert res["outcome"] == "budget_split"
    assert res["child_ids"] == ["c1", "c2"]
    assert calls and calls[0][1] == "goal_loop"
    assert "partial work done" in calls[0][2]


def test_goal_loop_blocks_when_split_refused(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])

    class _Refused:
        ok = False
        reason = "max depth"
        child_ids = None
        via = ""

    blocked = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: "working...",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.append(r),
        max_turns=1,
        first_response="partial",
        budget_split_fn=lambda *a, **kw: _Refused(),
    )
    assert res["outcome"] == "blocked_budget"
    assert blocked, "legacy block must still fire when split is refused"


def test_goal_loop_blocks_without_split_fn(monkeypatch):
    # Legacy call sites (no budget_split_fn) keep the exact old behaviour.
    _patch_judge(monkeypatch, ["continue"])
    blocked = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: "working...",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.append(r),
        max_turns=1,
        first_response="partial",
    )
    assert res["outcome"] == "blocked_budget"
    assert blocked
