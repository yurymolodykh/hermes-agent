"""A session that could not be saved must be a CATEGORY on the board, not an ordinary reply.

19.11.2026, карточка МПД шаг 1: ``run_worker_turns`` вернул ``failed=True`` с
``failure_reason='session_persistence_failed:…'`` и пустым ``final_response``. На доске
это легло как ``worker_result`` с ``exit_code=1`` и без признака категории, поэтому
дежурный и статистика видели «ход упал», а не «сессия не записалась».

Контракт:
  * ``failure_kind == 'session_persist'`` при сбое записи сессии; код выхода ненулевой;
  * ``last_output`` несёт маркер ``[session-persist-failed]``;
  * успешный ход поля не получает (нет ``failure_kind``);
  * причина называется проверяемыми фактами, а не «возможными причинами».
"""
import json
import os

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.turn_exit import (
    FAILURE_KIND_SESSION_PERSIST, FAILURE_KIND_TURN_ERROR, SESSION_PERSIST_MARKER, failure_kind,
)


def _bound_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="persist fixture", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        assert task is not None
        context = {"db": str(path), "task_id": task_id,
                   "run_id": task.current_run_id, "claim_lock": task.claim_lock}
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound",
                             {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
    return path, context


def _result(payload):
    path, context = _bound_claim_for(payload)
    return path, context


@pytest.fixture
def claim(tmp_path, monkeypatch):
    return _bound_claim(tmp_path, monkeypatch)


def test_session_persist_failure_is_its_own_category(claim, monkeypatch):
    path, context = claim
    failed = {"failed": True, "failure_reason": "session_persistence_failed:locked",
              "final_response": "⚠️ Нет ответа: Hermes не смог сохранить этот разговор"}
    monkeypatch.setattr(session_kanban, "_run_task_turns", lambda *_a: failed)
    frame = {"policy": {"kanban_json": json.dumps(context)}}

    session_kanban.run_worker_turns(object(), frame, [])

    result = session_kanban.worker_result(path, context)
    assert result["exit_code"] != 0, "сбой записи сессии не может выглядеть успехом"
    assert result["failure_kind"] == FAILURE_KIND_SESSION_PERSIST
    assert result["last_output"].startswith(SESSION_PERSIST_MARKER), result["last_output"]


def test_a_clean_turn_carries_no_failure_kind(claim, monkeypatch):
    path, context = claim
    monkeypatch.setattr(session_kanban, "_run_task_turns",
                        lambda *_a: {"completed": True, "final_response": "готово"})
    session_kanban.run_worker_turns(object(), {"policy": {"kanban_json": json.dumps(context)}}, [])

    result = session_kanban.worker_result(path, context)
    assert result["exit_code"] == 0
    assert "failure_kind" not in result


def test_a_generic_failure_is_turn_error(claim, monkeypatch):
    path, context = claim
    monkeypatch.setattr(session_kanban, "_run_task_turns",
                        lambda *_a: {"failed": True, "final_response": "нет доступа к файлу"})
    session_kanban.run_worker_turns(object(), {"policy": {"kanban_json": json.dumps(context)}}, [])

    result = session_kanban.worker_result(path, context)
    assert result["failure_kind"] == FAILURE_KIND_TURN_ERROR


@pytest.mark.parametrize("payload, expected", [
    ({"completed": True}, None),
    ({"failed": True, "failure_reason": "session_persistence_failed:disk"}, FAILURE_KIND_SESSION_PERSIST),
    ({"failed": True, "turn_exit_reason": "session_persistence_failed"}, FAILURE_KIND_SESSION_PERSIST),
    ({"failed": True, "failure_reason": "approval_denied"}, "tool_denied"),
    ({"failed": True, "failure_reason": "rate_limit"}, FAILURE_KIND_TURN_ERROR),
    ({"interrupted": True}, "turn_interrupted"),
    (None, FAILURE_KIND_TURN_ERROR),
])
def test_failure_kind_classification(payload, expected):
    assert failure_kind(payload) == expected


def test_explanation_names_measured_facts_not_possible_causes(tmp_path):
    """Дефект 2, п.3: сообщение называет то, что РЕАЛЬНО проверено."""
    from agent.turn_explainers import storage_facts, TurnExplainersMixin

    db = tmp_path / "state.db"
    db.write_bytes(b"")
    facts = storage_facts(db)
    text = "\n".join(facts)
    assert any("свобод" in f.lower() or "free space" in f.lower() for f in facts), text
    assert any(str(db) in f for f in facts), text
    # Свободный байт — проверка шла, а не «возможно, диск полон».
    import re
    assert re.search(r"\d", text), text

    out = TurnExplainersMixin._format_turn_completion_explanation(
        "session_persistence_failed", "locked", db_path=db)
    assert SESSION_PERSIST_MARKER not in out  # маркер — для доски, не для человека
    assert "Проверено" in out or "Checked" in out, out
