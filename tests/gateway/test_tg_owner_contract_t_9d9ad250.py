"""Контракт Telegram-доставки владельца от 2026-11-04 (карточка t_9d9ad250).

Первоисточник — прямое указание владельца:
  ``C:\\Users\\hermes\\AppData\\Local\\hermes\\profiles\\01-orchestrator\\state.db``,
  ``messages.id=973591``, ``session owui-b55bb405-…``, 2026-11-04 20:59:44 MSK:
  «в телеграм приходило только по делу — вопросы по миссиям, итоговые
  артефакты, алерты по невыполнению задач. а служебные и технические
  сообщения можно гасить».

Разрешённые классы (три) и запрещённый (один):

  1. вопросы по миссиям        -> [needs-decision] lane, не здесь;
  2. итог с файлом-артефактом  -> уходит в TG;
  3. алерт по невыполнению     -> kind=refusal|failure, уходит в TG;
  X. успешный текстовый итог без файла -> служебное, гасится (OWUI хранит).

Гейт реализован в ``DeliveryEngine.handle_branch_final`` и сужен тремя
условиями, каждое из которых пинится ниже отдельным тестом:

  * ``kind == "success"``      — иначе умерли бы вердикты ревью (класс 3);
  * ``platform == "api_server"`` — иначе умерла бы нативная TG-подписка,
    у которой TG единственный канал;
  * ``owui_healthy is not False`` — иначе при мёртвом OWUI итог терялся бы
    на ОБОИХ лейнах сразу.
"""

import json

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.docs = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append({"chat_id": chat_id, "file_path": file_path})

    async def handle_message(self, event):
        pass


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "contract.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, parent=None, assignee="worker", reviewer=None):
    tid = kb.create_task(
        conn, title=title, assignee=assignee, initial_status="running",
        **({"reviewer": reviewer} if reviewer else {}),
    )
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _read_suppression_journal():
    from hermes_constants import get_hermes_home

    log_path = get_hermes_home() / "logs" / "delivery-policy-suppressed.log"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _one_file_artifact(tmp_path, name="report.md"):
    path = tmp_path / name
    path.write_text("# отчёт\n\nреальный результат\n", encoding="utf-8")
    return str(path)


def _engine(owui_user, tg_chat):
    from gateway.delivery_engine import DeliveryEngine

    engine = DeliveryEngine(
        allowed_users=[tg_chat], owui_to_tg={owui_user: tg_chat}, home_channel=tg_chat,
    )
    adapter = RecordingAdapter()
    engine._tg_adapter = adapter
    sent, docs = [], []

    async def _send_tg(ad, chat_id, text):
        await ad.send(chat_id, text)
        sent.append((chat_id, text))

    async def _deliver_artifacts(*, adapter, chat_id, metadata=None,
                                 event_payload=None, task=None):
        import os
        for path in (event_payload or {}).get("artifacts") or []:
            if os.path.exists(path):
                await adapter.send_document(chat_id, path, metadata={})
                docs.append(str(path))

    return engine, adapter, sent, docs, _send_tg, _deliver_artifacts


def _api_sub(owui_user="sess-owner"):
    return {
        "platform": "api_server", "chat_id": "owui-1",
        "user_id": owui_user, "thread_id": None,
    }


# ---------------------------------------------------------------------------
# Класс X: успешный текстовый итог без файла -> в TG не идёт.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_text_only_итог_suppressed_in_tg_mirror(tmp_path):
    """X: success + нет артефакта + OWUI жив -> TG молчит, канал акается."""
    engine, adapter, sent, docs, send_tg, deliver_artifacts = _engine(
        "sess-owner", "tg-owner",
    )
    plan = {
        "root_id": "root-x", "board": "default", "terminal": True,
        "artifacts": [], "kind": "success",
    }
    res = await engine.handle_branch_final(
        plan=plan, sub=_api_sub(), msg="✅ Задача выполнена", adapter=adapter,
        sub_profile="mission", terminal=True, send_tg=send_tg,
        deliver_artifacts=deliver_artifacts, owui_healthy=True,
    )

    # Readback критерия 4: подавление наблюдаемо в результате, не только в логе.
    assert res.action == "advance"
    assert res.tg_suppressed is True
    assert sent == [], f"текстовый успешный итог не должен идти в TG: {sent}"
    assert adapter.sent == []
    assert docs == []
    journal = _read_suppression_journal()
    assert any(
        r.get("reason", "").startswith("success итог without artifact")
        for r in journal
    ), f"подавление обязано попасть в журнал: {journal}"


# ---------------------------------------------------------------------------
# Класс 2: итог С файлом-артефактом -> в TG идёт (гейт не переусердствовал).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_итог_with_artifact_still_delivered_to_tg(tmp_path):
    """2: success + есть файл -> TG получает и текст, и файл.

    Путь с явно переданными артефактами (caller-supplied): его использует
    ``final_report``, у которого контекст БД уже на руках.
    """
    artifact = await _one_file_artifact(tmp_path)
    engine, adapter, sent, docs, send_tg, deliver_artifacts = _engine(
        "sess-owner", "tg-owner",
    )
    plan = {
        "root_id": "root-a", "board": "default", "terminal": True,
        "artifacts": [artifact], "kind": "success",
    }
    res = await engine.handle_branch_final(
        plan=plan, sub=_api_sub(), msg="✅ Задача выполнена", adapter=adapter,
        sub_profile="mission", terminal=True, send_tg=send_tg,
        deliver_artifacts=deliver_artifacts, owui_healthy=True,
        artifacts=[artifact],
    )

    assert res.tg_suppressed is False
    assert len(sent) == 1, f"итог с артефактом обязан дойти в TG: {sent}"
    assert sent[0][0] == "tg-owner"
    assert docs == [artifact], f"файл обязан уехать вложением: {docs}"


# ---------------------------------------------------------------------------
# Класс 3: алерт по невыполнению -> в TG идёт ДАЖЕ без артефакта.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refusal", "failure"])
async def test_negative_verdict_итог_delivered_to_tg_without_artifact(kind):
    """3: refusal/failure — это алерт по невыполнению, гейт их не трогает.

    Без этого ограничения «только с артефактом» убило бы вердикты ревью
    (они почти всегда текстовые) и владелец перестал бы видеть отказы.
    """
    engine, adapter, sent, docs, send_tg, deliver_artifacts = _engine(
        "sess-owner", "tg-owner",
    )
    plan = {
        "root_id": f"root-{kind}", "board": "default", "terminal": True,
        "artifacts": [], "kind": kind,
    }
    verdict = "❌ Доработка отклонена — приёмка не приняла результат: t_rev"
    res = await engine.handle_branch_final(
        plan=plan, sub=_api_sub(), msg=verdict, adapter=adapter,
        sub_profile="mission", terminal=True, send_tg=send_tg,
        deliver_artifacts=deliver_artifacts, owui_healthy=True,
    )

    assert res.tg_suppressed is False, f"{kind} — не служебное сообщение"
    assert len(sent) == 1, f"алерт по невыполнению обязан дойти в TG: {sent}"
    assert "Доработка отклонена" in sent[0][1]
    assert not any(
        r.get("reason", "").startswith("success итог without artifact")
        for r in _read_suppression_journal()
    ), f"{kind} не должен попадать под success-гейт"


# ---------------------------------------------------------------------------
# Защитная сеть: мёртвый OWUI -> TG остаётся единственным путём.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_owui_keeps_tg_mirror_as_last_channel():
    """OWUI мёртв (unbound/failed) -> TG не гасим, иначе итог теряется на обоих."""
    engine, adapter, sent, docs, send_tg, deliver_artifacts = _engine(
        "sess-owner", "tg-owner",
    )
    plan = {
        "root_id": "root-dead", "board": "default", "terminal": True,
        "artifacts": [], "kind": "success",
    }
    res = await engine.handle_branch_final(
        plan=plan, sub=_api_sub(), msg="✅ Задача выполнена", adapter=adapter,
        sub_profile="mission", terminal=True, send_tg=send_tg,
        deliver_artifacts=deliver_artifacts, owui_healthy=False,
    )

    assert res.tg_suppressed is False
    assert len(sent) == 1, (
        f"при мёртвом OWUI TG — единственный путь до владельца: {sent}"
    )


# ---------------------------------------------------------------------------
# Нативная TG-подписка: TG — канал постановки, а не дубль. Не гасим.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_telegram_subscription_not_suppressed(board_env, monkeypatch):
    """platform=telegram: TG — единственный канал владельца по карточке."""
    root = _mk(board_env, "root")
    child = _mk(board_env, "child", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-chat-native",
    )
    for tid in (root, child):
        assert kb.complete_task(board_env, tid, summary="done")

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}

    import asyncio as _asyncio

    real_sleep = _asyncio.sleep

    async def fake_sleep(delay):
        if delay >= 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=1)

    assert len(adapter.sent) == 1, (
        f"нативная TG-подписка — не дубль, гасить нельзя: {adapter.sent}"
    )
    assert adapter.sent[0]["chat_id"] == "tg-chat-native"
    assert "Задача выполнена" in adapter.sent[0]["text"]
