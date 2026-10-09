"""The triage resolution tool is an explicitly authorized audit operation."""
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "01-orchestrator")
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="triaged")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    return tid


def test_resolve_triage_tool_requires_orchestrator_and_reason(board, monkeypatch):
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: False)
    assert not kt._check_kanban_orchestrator_mode()
    assert json.loads(kt._handle_resolve_triage({"task_id": board, "rationale": "approved"}))["error"]
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    assert json.loads(kt._handle_resolve_triage({"task_id": board, "rationale": "  "}))["error"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", "worker-card")
    assert not kt._check_kanban_orchestrator_mode()
    assert "orchestrator-only" in json.loads(kt._handle_resolve_triage({"task_id": board, "rationale": "approved"}))["error"]
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, board).status == "triage"
        assert not [e for e in kb.list_events(conn, board) if e.kind == "triage_resolved"]


def test_resolve_triage_tool_records_profile_and_only_changes_triage(board, monkeypatch):
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    assert "kanban_resolve_triage" in kt.KANBAN_RESOLVE_TRIAGE_SCHEMA["name"]
    out = json.loads(kt._handle_resolve_triage({"task_id": board, "rationale": "  explicitly reviewed  "}))
    assert out["ok"] and out["status"] == "ready"
    with kb.connect_closing() as conn:
        event = [e for e in kb.list_events(conn, board) if e.kind == "triage_resolved"][0]
        assert event.payload["actor"] == "01-orchestrator"
        assert event.payload["rationale"] == "explicitly reviewed"
    assert json.loads(kt._handle_resolve_triage({"task_id": board, "rationale": "again"}))["error"]
