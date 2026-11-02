"""t_1fd3f22b — единая функция доставки финального отчёта владельцу в TG.

Карточка требует:

  1. одна функция ``send_final_report(card_or_mission)``, которая берёт
     НЕИЗМЕНЁННЫЙ текст итога и HTML-артефакт у существующего генератора;
  2. chat_id — из подписки терминальной карточки (никакого хардкода);
  3. текст через ``send`` и HTML через ``send_document``, байт-в-байт;
  4. функция без триггер-вайринга — вызывается напрямую;
  5. тесты с мок-Telegram: оба вызова, chat_id = значение подписки,
     HTML-полезная нагрузка = выход генератора.

Тесты поднимают НАСТОЯЩИЙ стек: живая kanban-БД (tmp), настоящие
``owui_root_final_summary`` / ``owui_root_artifact_paths`` / продовый
артефакт-лейн ``_deliver_kanban_artifacts`` и запись журнала
``telegram-report-delivery.log``. Мок — только транспорт адаптера.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from gateway.platforms.base import SendResult
from hermes_cli import kanban_db as kb

OWNER_OWUI = "sess-owui-owner"
OWNER_TG = "125805788"
OTHER_TG = "320318064"


class RecordingTelegram:
    """Мок Telegram-транспорта: текст + документы, с управляемым сбоем."""

    supports_async_delivery = True

    def __init__(self):
        self.sent: list[dict] = []
        self.docs: list[dict] = []
        self.raise_on_send: Exception | None = None
        self.raise_on_document: Exception | None = None

    async def send(self, chat_id, text, metadata=None):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
        return SendResult(success=True, message_id=len(self.sent))

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        if self.raise_on_document is not None:
            raise self.raise_on_document
        payload = Path(file_path).read_bytes()
        self.docs.append({
            "chat_id": chat_id,
            "file_path": str(file_path),
            "file_name": file_name or os.path.basename(str(file_path)),
            "bytes": payload,
        })
        return SendResult(success=True)

    def extract_local_files(self, text):
        import re

        return re.findall(r"(?m)^\s*([A-Za-z]:[^\s\n]+\.\w+)\s*$", text or ""), []

    async def handle_message(self, event):
        return None


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "finalreport.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER_TG},{OTHER_TG}")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    journal = tmp_path / "telegram-report-delivery.log"
    monkeypatch.setenv("HERMES_REPORT_DELIVERY_LOG", str(journal))
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn, journal
    finally:
        conn.close()


def _html_report(tmp_path: Path, name: str = "sno-economics-report.html") -> Path:
    """Отчёт в прежнем формате (HTML), байты которого обязаны уйти как есть."""
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Итоговый отчёт миссии</title></head><body>"
        + "".join(f"<section><h2>Раздел {i}</h2><p>Результаты и выводы.</p></section>"
                  for i in range(40))
        + "</body></html>"
    )
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _mission_with_report(conn, tmp_path: Path) -> tuple[str, Path]:
    root = kb.create_task(conn, title="миссия с отчётом", assignee="worker")
    report = _html_report(tmp_path)
    kb.complete_task(
        conn, root, summary="миссия завершена, отчёт приложен",
        metadata={"artifacts": [str(report)]},
    )
    return root, report


def _journal_lines(journal: Path) -> list[dict]:
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. Native TG-подписка карточки: текст + HTML уходят владельцу
# ---------------------------------------------------------------------------

def test_sends_text_and_html_to_subscription_chat(board_env, tmp_path):
    from gateway.final_report import send_final_report

    conn, journal = board_env
    root, report = _mission_with_report(conn, tmp_path)
    kb.add_notify_sub(
        conn, task_id=root, platform="telegram", chat_id=OTHER_TG,
        user_id=OTHER_TG, chat_type="dm", delivery_mode="notify",
    )

    adapter = RecordingTelegram()
    outcome = asyncio.run(send_final_report(root, adapter=adapter, board="default"))

    assert outcome.delivered is True, outcome
    assert outcome.chat_source == "native_telegram_sub"
    # Оба вызова состоялись.
    assert len(adapter.sent) == 1 and len(adapter.docs) == 1
    # chat_id — ровно значение подписки (никакого хардкода/фолбэка).
    assert adapter.sent[0]["chat_id"] == OTHER_TG
    assert adapter.docs[0]["chat_id"] == OTHER_TG
    # Текст итога — существующий генератор, не переписанный шаблон.
    assert "✅ Задача выполнена" in adapter.sent[0]["text"]
    assert "миссия завершена, отчёт приложен" in adapter.sent[0]["text"]
    # HTML ушёл файлом и байт-в-байт равен файлу генератора.
    assert adapter.docs[0]["file_name"] == report.name
    assert adapter.docs[0]["bytes"] == report.read_bytes()
    assert hashlib.sha256(adapter.docs[0]["bytes"]).hexdigest() == \
        hashlib.sha256(report.read_bytes()).hexdigest()
    assert outcome.report_type == "html"
    # Журнал: одна запись о доставке с маской chat_id, не полным id.
    records = _journal_lines(journal)
    assert records and records[-1]["result"] == "delivered"
    assert records[-1]["task_id"] == root
    assert records[-1]["report_type"] == "html"
    assert records[-1]["chat_id_masked"] != OTHER_TG
    assert records[-1]["chat_id_masked"].endswith(OTHER_TG[-3:])
    assert OTHER_TG not in json.dumps(records, ensure_ascii=False)
    # Запись структурированная и датированная (приёмка: «время» в логе).
    assert records[-1]["event"] == "report_delivery"
    assert records[-1]["ts"] and records[-1]["ts_epoch"] > 0
    assert records[-1]["trigger"] == "manual_call"
    assert records[-1]["manual"] is False
    # Реально ушедшие файлы не должны попасть в «найдено, но не загружено».
    assert records[-1]["artifacts_not_uploaded"] == []
    assert [Path(p).name for p in records[-1]["artifacts"]] == [report.name]


# ---------------------------------------------------------------------------
# 2. chat_id резолвится по OWUI→TG привязке, когда native-подписки нет
# ---------------------------------------------------------------------------

def test_chat_id_resolved_from_owner_binding_when_no_native_sub(board_env, tmp_path):
    from gateway.final_report import send_final_report

    conn, _journal = board_env
    root, report = _mission_with_report(conn, tmp_path)
    kb.add_notify_sub(
        conn, task_id=root, platform="api_server", chat_id="owui-request",
        user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )

    adapter = RecordingTelegram()
    outcome = asyncio.run(send_final_report(root, adapter=adapter, board="default"))

    assert outcome.delivered is True, outcome
    assert outcome.chat_source == "terminal_owner"
    assert adapter.sent[0]["chat_id"] == OWNER_TG
    assert adapter.docs[0]["chat_id"] == OWNER_TG
    assert adapter.docs[0]["bytes"] == report.read_bytes()


# ---------------------------------------------------------------------------
# 3. Нет владельческого лейна: fail-closed, без исключения
# ---------------------------------------------------------------------------

def test_no_owner_lane_is_fail_closed(board_env, tmp_path):
    from gateway.final_report import REASON_NO_OWNER_LANE, send_final_report

    conn, _journal = board_env
    root, _report = _mission_with_report(conn, tmp_path)

    adapter = RecordingTelegram()
    outcome = asyncio.run(send_final_report(root, adapter=adapter, board="default"))

    assert outcome.delivered is False
    assert outcome.reason == REASON_NO_OWNER_LANE
    assert adapter.sent == [] and adapter.docs == []


# ---------------------------------------------------------------------------
# 4. Исключение Telegram не выходит наружу и не рвёт вызов
# ---------------------------------------------------------------------------

def test_telegram_exception_is_isolated(board_env, tmp_path):
    from gateway.final_report import send_final_report

    conn, journal = board_env
    root, report = _mission_with_report(conn, tmp_path)
    kb.add_notify_sub(
        conn, task_id=root, platform="telegram", chat_id=OTHER_TG,
        user_id=OTHER_TG, chat_type="dm", delivery_mode="notify",
    )

    adapter = RecordingTelegram()
    adapter.raise_on_send = RuntimeError("telegram exploded: 502 Bad Gateway")

    outcome = asyncio.run(send_final_report(root, adapter=adapter, board="default"))

    assert outcome.delivered is False
    assert "502 Bad Gateway" in outcome.reason
    # Вложения не отправлялись вслед за упавшим текстом.
    assert adapter.docs == []
    records = _journal_lines(journal)
    assert records[-1]["result"] == "failed"
    assert "502 Bad Gateway" in records[-1]["reason"]
    assert records[-1]["report_type"] == "html"


def test_document_exception_is_isolated_but_text_counts_as_delivered(
    board_env, tmp_path,
):
    from gateway.final_report import send_final_report

    conn, journal = board_env
    root, _report = _mission_with_report(conn, tmp_path)
    kb.add_notify_sub(
        conn, task_id=root, platform="telegram", chat_id=OTHER_TG,
        user_id=OTHER_TG, chat_type="dm", delivery_mode="notify",
    )

    adapter = RecordingTelegram()
    adapter.raise_on_document = ConnectionError("upload aborted")

    outcome = asyncio.run(send_final_report(root, adapter=adapter, board="default"))

    assert outcome.delivered is True  # текст принят адаптером
    assert "artifact upload failed" in outcome.reason
    assert outcome.artifacts_failed  # видно, что файл не ушёл
    records = _journal_lines(journal)
    assert records[-1]["result"] == "delivered"
    assert "artifact upload failed" in records[-1]["reason"]


# ---------------------------------------------------------------------------
# 5. dry_run: собирает, ничего не отправляет
# ---------------------------------------------------------------------------

def test_dry_run_sends_nothing(board_env, tmp_path):
    from gateway.final_report import send_final_report

    conn, journal = board_env
    root, report = _mission_with_report(conn, tmp_path)
    kb.add_notify_sub(
        conn, task_id=root, platform="telegram", chat_id=OTHER_TG,
        user_id=OTHER_TG, chat_type="dm", delivery_mode="notify",
    )

    adapter = RecordingTelegram()
    outcome = asyncio.run(
        send_final_report(root, adapter=adapter, board="default", dry_run=True)
    )

    assert outcome.delivered is False
    assert adapter.sent == [] and adapter.docs == []
    assert [Path(p).name for p in outcome.artifacts] == [report.name]
    assert outcome.chat_id_masked.endswith(OTHER_TG[-3:])
    assert _journal_lines(journal) == []


# ---------------------------------------------------------------------------
# 6. Пустой итог без отчёта не отправляется (F1: никаких голых пингов)
# ---------------------------------------------------------------------------

def test_contentless_itog_is_not_sent(board_env):
    from gateway.final_report import REASON_CONTENTLESS, send_final_report

    conn, _journal = board_env
    root = kb.create_task(conn, title="пустая миссия", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root,))
    kb.add_notify_sub(
        conn, task_id=root, platform="telegram", chat_id=OTHER_TG,
        user_id=OTHER_TG, chat_type="dm", delivery_mode="notify",
    )

    adapter = RecordingTelegram()
    outcome = asyncio.run(send_final_report(root, adapter=adapter, board="default"))

    assert outcome.delivered is False
    assert outcome.reason == REASON_CONTENTLESS
    assert adapter.sent == [] and adapter.docs == []


# ---------------------------------------------------------------------------
# 7. Повторный вызов с dedup-ключом не дублирует отчёт
# ---------------------------------------------------------------------------

def test_dedup_key_prevents_duplicate_send(board_env, tmp_path):
    from gateway.final_report import send_final_report

    conn, _journal = board_env
    root, _report = _mission_with_report(conn, tmp_path)
    kb.add_notify_sub(
        conn, task_id=root, platform="telegram", chat_id=OTHER_TG,
        user_id=OTHER_TG, chat_type="dm", delivery_mode="notify",
    )

    adapter = RecordingTelegram()
    first = asyncio.run(send_final_report(
        root, adapter=adapter, board="default", dedup_key="marker:1",
    ))
    second = asyncio.run(send_final_report(
        root, adapter=adapter, board="default", dedup_key="marker:1",
    ))

    assert first.delivered is True
    assert second.already_delivered is True
    assert len(adapter.sent) == 1 and len(adapter.docs) == 1


# ---------------------------------------------------------------------------
# 8. Формат отчёта не тронут: файл на диске тот же, шаблон не переписан
# ---------------------------------------------------------------------------

def test_report_file_is_shipped_untouched(board_env, tmp_path):
    from gateway.final_report import send_final_report

    conn, _journal = board_env
    root, report = _mission_with_report(conn, tmp_path)
    kb.add_notify_sub(
        conn, task_id=root, platform="telegram", chat_id=OTHER_TG,
        user_id=OTHER_TG, chat_type="dm", delivery_mode="notify",
    )
    before_size = report.stat().st_size
    before_hash = hashlib.sha256(report.read_bytes()).hexdigest()

    adapter = RecordingTelegram()
    asyncio.run(send_final_report(root, adapter=adapter, board="default"))

    after_bytes = report.read_bytes()
    assert report.stat().st_size == before_size
    assert hashlib.sha256(after_bytes).hexdigest() == before_hash
    assert adapter.docs[0]["bytes"] == after_bytes
    # «Генератор» платформы не переписывает отчёт: отправленный путь — тот же
    # файл, что объявлен карточкой.
    assert Path(adapter.docs[0]["file_path"]).resolve() == report.resolve()
