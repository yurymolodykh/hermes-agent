"""Tests for decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kbc.connect() as conn:
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn)
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kbc.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert comments
    assert any(ev.kind == "decomposed" for ev in events)


def test_children_inherit_root_priority_exactly(kanban_home):
    """A fan-out of a priority-190 card must not sink to the SQL default 0;
    an explicit child priority wins over the root's."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="urgent")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 190 WHERE id = ?", (tid,))
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[
                {"title": "inherits", "assignee": "default"},
                {"title": "overrides", "assignee": "default", "priority": 5},
                {"title": "explicit zero", "assignee": "default", "priority": 0},
            ],
        )
    assert child_ids is not None
    with kbc.connect() as conn:
        inherited, overridden, explicit_zero = (kb.get_task(conn, c) for c in child_ids)
    assert inherited.priority == 190
    assert overridden.priority == 5
    assert explicit_zero.priority == 0


def test_children_inherit_root_dispatch_pins(kanban_home):
    """skills / runtime cap / model+provider pins / reasoning effort follow the
    root down the chain; a child's own values win."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="pinned")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = 900, skills = ?, "
                "model_override = ?, provider_override = ?, reasoning_effort = ? "
                "WHERE id = ?",
                ('["codebase-inspection", "plan"]', "deepseek-v4-flash", "letovo", "high", tid),
            )
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[
                {"title": "inherits", "assignee": "default"},
                {
                    "title": "overrides", "assignee": "default",
                    "skills": ["github-code-review"], "max_runtime_seconds": 60,
                    "model_override": "gpt-5", "provider_override": "openai",
                    "reasoning_effort": "low",
                },
            ],
        )
    assert child_ids is not None
    with kbc.connect() as conn:
        inherited = kb.get_task(conn, child_ids[0])
        overridden = kb.get_task(conn, child_ids[1])
    assert inherited.skills == ["codebase-inspection", "plan"]
    assert inherited.max_runtime_seconds == 900
    assert inherited.model_override == "deepseek-v4-flash"
    assert inherited.provider_override == "letovo"
    assert inherited.reasoning_effort == "high"
    assert overridden.skills == ["github-code-review"]
    assert overridden.max_runtime_seconds == 60
    assert overridden.model_override == "gpt-5"
    assert overridden.provider_override == "openai"
    assert overridden.reasoning_effort == "low"


def test_worktree_child_still_gets_no_workspace_path(kanban_home, tmp_path):
    """Regression: inheriting pins must not resurrect workspace_path sharing for
    worktrees — concurrent siblings would land in one checkout."""
    worktree_dir = tmp_path / "wt"
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="tree card")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, "
                "priority = 7 WHERE id = ?",
                (str(worktree_dir), tid),
            )
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orch",
            children=[{"title": "a", "assignee": "default"}, {"title": "b", "assignee": "default"}],
        )
    assert child_ids is not None
    with kbc.connect() as conn:
        for child_id in child_ids:
            child = kb.get_task(conn, child_id)
            assert child.workspace_kind == "worktree"
            assert child.workspace_path is None
            assert child.priority == 7


def test_dir_child_inherits_root_workspace_path(kanban_home, tmp_path):
    """Contrast case: a 'dir' child keeps sharing the root's directory (only
    worktrees are excluded), and inherits its pins."""
    shared = tmp_path / "shared"
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="dir card")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind = 'dir', workspace_path = ?, "
                "priority = 11 WHERE id = ?",
                (str(shared), tid),
            )
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orch",
            children=[{"title": "a", "assignee": "default"}],
        )
    assert child_ids is not None
    with kbc.connect() as conn:
        child = kb.get_task(conn, child_ids[0])
    assert child.workspace_kind == "dir"
    assert child.workspace_path == str(shared)
    assert child.priority == 11





