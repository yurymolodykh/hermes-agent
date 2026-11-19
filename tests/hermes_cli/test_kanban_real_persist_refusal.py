"""A real persistence refusal (not an injected payload) is its own board category.

19.11.2026, карточка МПД шаг 1: сессия не смогла сохраниться, ход закрылся с exit 1, а
``worker_result`` не нёс признака категории — дежурный не мог отличить отказ записи от
любого другого падения. Плюс сама формулировка объяснения предлагала «возможные причины»
вместо проверенных фактов (и в отказе прав советовала «базу держит другой процесс»).

Здесь отказ воспроизводится ПО-НАСТОЯЩЕМУ: ``state.db`` делается недоступным для записи вне
``HERMES_HOME`` (внутри дома preflight сам чинит права, и отказа бы не было), после чего
проверяется, что категория, маркер и названные факты соответствуют реальной причине.
"""
import json
import os
import stat
from types import SimpleNamespace

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_client as wc
from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.turn_exit import SESSION_PERSIST_MARKER, failure_kind


def _attempt(path, title):
    with connect_closing(path) as conn:
        tid = kb.create_task(conn, title=title, assignee="default")
        task = kb.claim_task(conn, tid, claimer="host:owner")
        assert task is not None
        ctx = {"db": str(path), "task_id": tid, "run_id": task.current_run_id,
               "claim_lock": task.claim_lock, "board": "default"}
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_bound",
                             {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
    return ctx


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return kb.kanban_db_path()


def test_a_saved_result_outranks_an_unknown_receipt(board, capsys):
    """Дефект 1: сданная работа не превращается в «доска не признала исполнителя»."""
    ctx = _attempt(board, "saved-result")
    with connect_closing(board) as conn, kb.write_txn(conn):
        kb._append_event(conn, ctx["task_id"], "worker_result",
                         {"pid": os.getpid(), "claim_lock": ctx["claim_lock"], "exit_code": 1,
                          "failure_kind": "session_persist",
                          "last_output": f"{SESSION_PERSIST_MARKER} Hermes не смог сохранить этот разговор"},
                         run_id=ctx["run_id"])

    assert wc._exit_code({"status": "unknown"}, ctx, str(board)) == 1
    saved = session_kanban.worker_result(str(board), ctx)
    assert saved["failure_kind"] == "session_persist"
    assert SESSION_PERSIST_MARKER in capsys.readouterr().out


def test_an_unknown_receipt_without_a_result_still_refuses_and_says_why(board):
    """Дефект 1, п.2: отказ приёма обязан объяснять, а не только констатировать."""
    ctx = _attempt(board, "no-result")
    with pytest.raises(GatewayClientError) as err:
        wc._exit_code({"status": "unknown"}, ctx, str(board))
    message = str(err.value)
    assert "unknown" in message and "retained by owner" in message
    assert "Cause" in message, message


def test_a_real_read_only_store_is_a_category_and_names_the_cause(tmp_path, monkeypatch):
    """Дефект 2: настоящий отказ записи → категория и ПРОВЕРЕННЫЕ факты."""
    from hermes_state import SessionDB
    from hermes_state_errors import classify_persistence_error

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    store_dir = tmp_path / "foreign-store"
    store_dir.mkdir()
    db = SessionDB(db_path=store_dir / "state.db")
    db.create_session("s", source="test")
    db.close()
    (store_dir / "state.db").chmod(stat.S_IRUSR)
    store_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        cause = None
        try:
            SessionDB(db_path=store_dir / "state.db").append_message(
                "s", role="user", content="после отказа записи")
        except Exception as exc:
            cause = classify_persistence_error(exc)
        assert cause in ("disk", "permission_denied"), cause
        assert failure_kind(
            {"failed": True, "failure_reason": f"session_persistence_failed:{cause}"}
        ) == "session_persist"

        from agent.turn_explainers import TurnExplainersMixin
        out = TurnExplainersMixin._format_turn_completion_explanation(
            "session_persistence_failed", cause, db_path=store_dir / "state.db")
        assert "Проверено" in out or "Checked" in out
        assert "NOT writable" in out or "НЕ доступен" in out, out
        # Права запрещают запись — винить чужой процесс за блокировку нельзя.
        assert "another process holds" not in out, out
    finally:
        store_dir.chmod(stat.S_IRWXU)
        (store_dir / "state.db").chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_a_failed_turn_reaches_the_board_as_a_category(board, monkeypatch):
    """Дефект 2: категория и маркер доезжают до worker_result."""
    ctx = _attempt(board, "persist-turn")
    failed = {"failed": True, "failure_reason": "session_persistence_failed:permission_denied",
              "final_response": "⚠️ Нет ответа: Hermes не смог сохранить этот разговор"}
    monkeypatch.setattr(session_kanban, "_run_task_turns", lambda *_a: failed)
    session_kanban.run_worker_turns(SimpleNamespace(), {"policy": {"kanban_json": json.dumps(ctx)}}, [])
    result = session_kanban.worker_result(board, ctx)
    assert result["exit_code"] != 0
    assert result["failure_kind"] == "session_persist"
    assert result["last_output"].startswith(SESSION_PERSIST_MARKER)
