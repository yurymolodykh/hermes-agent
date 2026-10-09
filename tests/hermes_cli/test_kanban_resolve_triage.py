"""An explicit triage resolution resumes work without erasing loop evidence."""
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    kb.init_db()
    with kb.connect_closing() as db:
        yield db


def _triage(conn, task_id):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage', block_kind='capability', block_recurrences=2 WHERE id=?", (task_id,))


def test_resolution_requires_reason_and_only_accepts_triage(conn):
    tid = kb.create_task(conn, title="triage")
    for reason in (None, "", "  "):
        with pytest.raises(ValueError, match="rationale"):
            kb.resolve_triage(conn, tid, rationale=reason, actor="orchestrator")
    assert kb.resolve_triage(conn, tid, rationale="reviewed", actor="orchestrator") is False
    _triage(conn, tid)
    assert kb.resolve_triage(conn, tid, rationale="\n  decision recorded  \n", actor="orchestrator") is True
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.block_recurrences == 2 and task.block_kind == "capability"
    events = [e for e in kb.list_events(conn, tid) if e.kind == "triage_resolved"]
    assert len(events) == 1
    assert events[0].payload["rationale"] == "decision recorded"
    assert events[0].payload["actor"] == "orchestrator"
    assert events[0].payload["status"] == "ready"
    assert kb.resolve_triage(conn, tid, rationale="repeat", actor="orchestrator") is False
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "triage_resolved"]) == 1


def test_resolution_gates_on_actual_parents_and_preserves_failure_state(conn):
    parent = kb.create_task(conn, title="parent")
    child = kb.create_task(conn, title="child", parents=[parent])
    _triage(conn, child)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures=3, last_failure_error='original failure' WHERE id=?", (child,))
    assert kb.resolve_triage(conn, child, rationale="dependency addressed", actor="operator")
    task = kb.get_task(conn, child)
    assert task.status == "todo"
    assert task.consecutive_failures == 3 and task.last_failure_error == "original failure"
    assert kb.list_events(conn, child)[-1].payload["status"] == "todo"


def test_resolution_reclaims_dangling_run_and_retriages_on_same_block(conn):
    tid = kb.create_task(conn, title="loop")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker")
    run_id = kb.get_task(conn, tid).current_run_id
    # Simulate an older/external writer that stranded the active run in triage.
    _triage(conn, tid)
    assert kb.resolve_triage(conn, tid, rationale="explicit disposition", actor="operator")
    assert kb.get_task(conn, tid).current_run_id is None
    row = conn.execute("SELECT status, outcome, ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == row["outcome"] == "reclaimed" and row["ended_at"] is not None
    assert kb.claim_task(conn, tid, claimer="worker")
    assert kb.block_task(conn, tid, reason="still blocked", kind="capability")
    assert kb.get_task(conn, tid).status == "triage"
