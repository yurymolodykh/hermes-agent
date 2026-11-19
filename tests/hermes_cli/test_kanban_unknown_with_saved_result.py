"""A settled receipt of ``unknown`` must not hide a worker_result the owner already wrote.

Ловушка (19.11.2026, карточка МПД шаг 1, run 102): сессия воркера не смогла сохраниться,
ход завершился с ``exit_code=1`` и корректным ``last_output`` — ``worker_result`` записан.
Следующая попытка прочитала ``receipt.status == 'unknown'`` и воркер вышел с сообщением
«Kanban execution unknown; retained by owner», начисто скрыв настоящую причину: на доске
падение выглядело как «доска не признала исполнителя».

Правило: ``worker_result`` этой попытки (совпадающий ``claim_lock``) — первоисточник.
``unknown`` вторично и не должен ни отменять сданную работу, ни затирать её причину.
"""
import asyncio

import pytest

from hermes_cli import kanban_worker_client
from hermes_cli.gateway_client import GatewayClientError


def _exit_code(receipt_status, worker_result_payload, capsys=None):
    """Call the real ``_exit_code`` with a stubbed primary-source read.

    The stub keeps the real contract: a ``worker_result`` of another claim is not this attempt.
    """
    from gateway import session_kanban
    def fake_worker_result(_db, params):
        return dict(worker_result_payload) if worker_result_payload.get("claim_lock") == params.get("claim_lock") else {}
    original = session_kanban.worker_result
    session_kanban.worker_result = fake_worker_result
    try:
        receipt = {"status": receipt_status}
        return kanban_worker_client._exit_code(
            receipt, {"task_id": "t", "run_id": 1, "claim_lock": "cl"}, "/nonexistent.db")
    finally:
        session_kanban.worker_result = original


def test_unknown_with_a_saved_result_is_the_result_not_a_refusal(capsys):
    """Дефект 1, критерий 1: работа сдана — значит исход берётся из worker_result."""
    code = _exit_code("unknown", {"claim_lock": "cl", "exit_code": 0,
                                  "last_output": "карточка закрыта"})
    assert code == 0, "сданная работа при unknown не имеет права считаться падением"
    assert "карточка закрыта" in capsys.readouterr().out


def test_unknown_with_a_failed_saved_result_keeps_its_own_exit_code(capsys):
    """Причина неудачи сохраняется: код выхода и текст — из worker_result, не 1 по умолчанию."""
    marker = "[session-persist-failed] Hermes не смог сохранить этот разговор"
    code = _exit_code("unknown", {"claim_lock": "cl", "exit_code": 1, "last_output": marker})
    assert code == 1
    out = capsys.readouterr().out
    assert marker in out, "настоящая причина обязана дойти до лога воркера"
    assert "execution unknown" not in out


@pytest.fixture(name="worker_log")
def worker_log_fixture(monkeypatch):
    """Подменить лог воркера — единственный носитель причины, когда доски нет."""
    from hermes_cli import kanban_db
    def install(text):
        monkeypatch.setattr(kanban_db, "read_worker_log", lambda *a, **k: text)
    return install


def test_unknown_without_any_source_refuses_and_does_not_claim_a_silent_log(capsys, worker_log):
    """Дефект 1, критерий 2 (база): записи нет, оба источника молчат — отказ, без ложных фактов."""
    worker_log("")
    with pytest.raises(GatewayClientError) as err:
        _exit_code("unknown", {})
    message = str(err.value)
    assert "unknown" in message
    assert "cause" in message.lower() or "причин" in message.lower()
    # Утверждать «лог пуст» можно только после того, как его прочитали: это проверено выше.
    assert "worker log empty" in message


def test_unknown_refusal_quotes_the_real_cause_from_the_worker_log(capsys, worker_log):
    """Дефект 1, критерий 2: причина известна из лога — она обязана попасть в сообщение.

    Ревизия 4b5a67b85f не импортировала ``SESSION_PERSIST_MARKER``: ``NameError`` внутри
    ``any(...)`` глотался ``except Exception: pass``, и сообщение утверждало «the worker log is
    empty» при непустом логе. Тест ловит именно это: он требует фрагмент НАСТОЯЩЕЙ причины.
    """
    worker_log("...\n⚠️ Нет ответа: Hermes не смог сохранить этот разговор и остановился\n")
    with pytest.raises(GatewayClientError) as err:
        _exit_code("unknown", {})
    message = str(err.value)
    assert "не смог сохранить" in message, message
    assert "worker log: " in message
    # Доска здесь недоступна (/nonexistent.db) — это должно быть сказано, а не выдумано.
    assert "board unreadable" in message


def test_unknown_refusal_reads_the_persist_marker_alone(capsys, worker_log):
    """Маркер ``[session-persist-failed]`` распознаётся сам по себе — значит он импортирован.

    Косвенная проверка импорта: без него строка-маркер не опознаётся и ветка чтения лога мертва.
    """
    from hermes_cli.turn_exit import SESSION_PERSIST_MARKER
    worker_log(f"{SESSION_PERSIST_MARKER} turn aborted: session store refused the write\n")
    with pytest.raises(GatewayClientError) as err:
        _exit_code("unknown", {})
    assert SESSION_PERSIST_MARKER in str(err.value)


def test_unknown_refusal_falls_back_to_the_last_log_line(capsys, worker_log):
    """Лог без известных маркеров: причина всё равно берётся из его последней строки."""
    worker_log("starting turn\nexit: provider returned 500\n")
    with pytest.raises(GatewayClientError) as err:
        _exit_code("unknown", {})
    message = str(err.value)
    assert "worker log ends: exit: provider returned 500" in message


def test_terminal_receipt_is_unchanged(capsys):
    """Регрессия: обычный terminal-путь читает worker_result ровно как раньше."""
    assert _exit_code("terminal", {"claim_lock": "cl", "exit_code": 0,
                                   "last_output": "готово"}) == 0
    assert "готово" in capsys.readouterr().out


def test_wrong_claim_lock_is_not_this_attempt(capsys):
    """worker_result чужой попытки — не наша; отказ остаётся отказом."""
    with pytest.raises(GatewayClientError):
        _exit_code("unknown", {"claim_lock": "other", "exit_code": 0})
