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


@pytest.fixture(autouse=True)
def _no_checkpoint_write(monkeypatch):
    """The lost-execution path tries to leave a durable note; that is another test's subject.

    Without this the best-effort write would print its refusal into the stderr these diagnostics
    tests assert on. The checkpoint itself is covered by
    ``test_kanban_worker_lost_is_a_platform_requeue``.
    """
    monkeypatch.setattr(kanban_worker_client, "_checkpoint_worker_lost", lambda *a, **k: None)


def _lost(capsys):
    """Run the no-source path and return (exit code, stderr diagnosis).

    OPS-16 replaced the raised ``GatewayClientError`` with the platform-loss exit code so the crash
    sweep requeues instead of charging the card — the diagnosis now goes to stderr, unchanged.
    """
    code = _exit_code("unknown", {})
    return code, capsys.readouterr().err


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
    code, message = _lost(capsys)
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE, "потеря исполнения — код платформы"
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
    code, message = _lost(capsys)
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE
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
    code, message = _lost(capsys)
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE
    assert SESSION_PERSIST_MARKER in message


def test_unknown_refusal_falls_back_to_the_last_log_line(capsys, worker_log):
    """Лог без известных маркеров: причина всё равно берётся из его последней строки."""
    worker_log("starting turn\nexit: provider returned 500\n")
    code, message = _lost(capsys)
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE
    assert "worker log ends: exit: provider returned 500" in message


def test_unknown_refusal_says_unreadable_not_empty_when_the_log_cannot_be_read(tmp_path, monkeypatch, capsys):
    """Дефект 1, критерий 2: ``None`` от ``read_worker_log`` — не доказательство пустого лога.

    ``read_worker_log`` возвращает ``None`` и когда файла нет, и когда чтение упало с ``OSError``
    (внутренний ``except OSError``). Клиент превращал оба случая в ``''`` и печатал
    «worker log empty» о логе, который существует и не был прочитан (ревью 19.11.2026, лог с
    ``chmod 0``). Проверяемый факт обязан различать эти два мира.
    """
    import os
    from hermes_cli import kanban_db

    log = tmp_path / "t.log"
    log.write_text("⚠️ Нет ответа: Hermes не смог сохранить этот разговор\n", encoding="utf-8")
    log.chmod(0)
    try:
        if os.access(log, os.R_OK):  # root читает что угодно — сценарий недостижим
            pytest.skip("running as a user that can read a 0-mode file")
        monkeypatch.setattr(kanban_db, "worker_log_path", lambda task_id, *, board=None: log)
        code, message = _lost(capsys)
    finally:
        log.chmod(0o600)
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE
    assert "worker log unreadable" in message, message
    assert "worker log empty" not in message, message


def test_unknown_refusal_says_unreadable_when_the_read_fails_without_a_permission_trick(
        tmp_path, monkeypatch, capsys):
    """Тот же разрыв без ``chmod``: файл есть, а чтение всё равно вернуло ``None``."""
    from hermes_cli import kanban_db

    log = tmp_path / "t.log"
    log.write_text("непустой лог\n", encoding="utf-8")
    monkeypatch.setattr(kanban_db, "worker_log_path", lambda task_id, *, board=None: log)
    monkeypatch.setattr(kanban_db, "read_worker_log", lambda *a, **k: None)
    code, message = _lost(capsys)
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE
    assert "worker log unreadable" in message, message
    assert "worker log empty" not in message, message


def test_unknown_refusal_says_empty_only_when_the_file_is_absent(tmp_path, monkeypatch, capsys):
    """Обратный случай: файла действительно нет — тогда «worker log empty» правда."""
    from hermes_cli import kanban_db

    monkeypatch.setattr(kanban_db, "worker_log_path",
                        lambda task_id, *, board=None: tmp_path / "absent.log")
    code, message = _lost(capsys)
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE
    assert "worker log empty" in message, message
    assert "worker log unreadable" not in message, message


def test_terminal_receipt_is_unchanged(capsys):
    """Регрессия: обычный terminal-путь читает worker_result ровно как раньше."""
    assert _exit_code("terminal", {"claim_lock": "cl", "exit_code": 0,
                                   "last_output": "готово"}) == 0
    assert "готово" in capsys.readouterr().out


def test_wrong_claim_lock_is_not_this_attempt(capsys):
    """worker_result чужой попытки — не наша; потеря исполнения остаётся потерей."""
    code = _exit_code("unknown", {"claim_lock": "other", "exit_code": 0})
    assert code == kanban_worker_client.WORKER_LOST_EXIT_CODE
    capsys.readouterr()
