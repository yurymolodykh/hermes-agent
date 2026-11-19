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


def test_unknown_without_a_saved_result_still_refuses_and_names_the_reason(capsys):
    """Дефект 1, критерий 2: записи нет — отказ, но сообщение объясняет «почему»."""
    with pytest.raises(GatewayClientError) as err:
        _exit_code("unknown", {})
    message = str(err.value)
    assert "unknown" in message
    # Сообщение обязано нести причину, а не только констатацию отказа.
    assert "причин" in message.lower() or "cause" in message.lower() or "reason" in message.lower()


def test_terminal_receipt_is_unchanged(capsys):
    """Регрессия: обычный terminal-путь читает worker_result ровно как раньше."""
    assert _exit_code("terminal", {"claim_lock": "cl", "exit_code": 0,
                                   "last_output": "готово"}) == 0
    assert "готово" in capsys.readouterr().out


def test_wrong_claim_lock_is_not_this_attempt(capsys):
    """worker_result чужой попытки — не наша; отказ остаётся отказом."""
    with pytest.raises(GatewayClientError):
        _exit_code("unknown", {"claim_lock": "other", "exit_code": 0})
