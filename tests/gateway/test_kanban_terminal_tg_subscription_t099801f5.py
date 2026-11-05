"""t_099801f5 — терминальная карточка сохраняет Telegram-подписку.

Acceptance карточки: «терминальная карточка должна иметь Telegram-подписку в
общем хранилище подписок; существующая логика подписок переиспользована, а не
продублирована; проект собирается, тесты проходят».

Что проверяется здесь (на реальном kanban_db + реальном branch-watcher стеке):

  1. ``complete_task`` записывает в ``kanban_notify_subs`` строку
     ``telegram:<chat владельца>`` с флагом notify (``delivery_mode='notify'``)
     для карточки, у которой есть api_server-подписка с верифицированной
     OWUI→TG привязкой владельца.
  2. Тот же эффект у ``archive_task`` (админ-закрытие — тоже терминальный
     переход; инцидент sno-economics-r3 закрывался именно так).
  3. Идемпотентность: повторное завершение после reopen не добавляет вторую
     строку и не меняет chat_id.
  4. Fail-closed: без привязки владельца строка НЕ фабрикуется, исключения нет,
     подписка api_server остаётся единственной.
  5. Native telegram-подписка не дублируется (карточка уже адресуема).
  6. Одна карточка — один итог: новая адресная строка не превращается во второй
     канал, владелец получает РОВНО одно сообщение (проверяется watcher-тиком
     с реальным RecordingAdapter).
  7. Строка живёт в том же хранилище и видна штатному читателю
     ``list_notify_subs``; адресные строки отличимы от каналов
     (``include_terminal_mirror``).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

OWNER_OWUI = "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"
OWNER_TG = "125805788"


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "t0998.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", OWNER_TG)
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mission(conn, *, with_binding=True, with_native_tg=False):
    """Root card + api_server subscription of a verified OWUI owner."""
    root = kb.create_task(conn, title="mission", assignee="worker")
    kb.add_notify_sub(
        conn,
        task_id=root,
        platform="api_server",
        chat_id="owui-request" if with_binding else "owui-request-unbound",
        user_id=OWNER_OWUI if with_binding else "unbound-owner",
        delivery_mode="notify+wake",
    )
    if with_native_tg:
        kb.add_notify_sub(
            conn, task_id=root, platform="telegram", chat_id=OWNER_TG,
            user_id=OWNER_TG,
        )
    return root


def _tg_subs(conn, task_id):
    return [
        s for s in kb.list_notify_subs(conn, task_id)
        if str(s.get("platform") or "").lower() == "telegram"
    ]


# ---------------------------------------------------------------------------
# 1. complete_task → подписка в общем хранилище
# ---------------------------------------------------------------------------

def test_complete_task_stamps_owner_telegram_subscription(board_env):
    root = _mission(board_env)
    assert _tg_subs(board_env, root) == []

    assert kb.complete_task(board_env, root, summary="миссия закрыта")

    subs = _tg_subs(board_env, root)
    assert len(subs) == 1, f"ожидалась одна telegram-подписка: {subs}"
    sub = subs[0]
    assert sub["chat_id"] == OWNER_TG, "chat_id подписки = чат владельца"
    assert sub["user_id"] == OWNER_TG
    assert sub["delivery_mode"] == "notify", "флаг notify сохранён"
    assert sub["chat_type"] == "dm"
    assert sub["task_id"] == root

    # Строка — в ОДНОМ хранилище с остальными подписками (не второй store):
    # штатный читатель без фильтров видит обе строки карточки.
    all_platforms = {s["platform"] for s in kb.list_notify_subs(board_env, root)}
    assert all_platforms == {"api_server", "telegram"}

    # И это адрес итога, а не второй канал: адресная строка помечена, поэтому
    # перечисление каналов её не видит (иначе владелец получил бы два итога).
    channel_subs = kb.list_notify_subs(
        board_env, root, include_terminal_mirror=False,
    )
    assert [s["platform"] for s in channel_subs] == ["api_server"]
    assert kb.terminal_mirror_chats(board_env, root) == {OWNER_TG}


def test_complete_task_reuses_existing_subscription_store(board_env):
    """Запись делает существующий writer, а не параллельный INSERT."""
    root = _mission(board_env)
    kb.complete_task(board_env, root, summary="готово")

    row = board_env.execute(
        "SELECT platform, chat_id, delivery_mode, last_event_id "
        "  FROM kanban_notify_subs WHERE task_id = ? AND platform = 'telegram'",
        (root,),
    ).fetchone()
    assert row is not None
    # last_event_id стартует «догоняющим» (snapshot MAX(task_events.id)) —
    # ровно то поведение, которое задаёт единственный writer add_notify_sub;
    # параллельный INSERT оставил бы schema-default 0 и вызвал бы replay.
    assert int(row["last_event_id"]) > 0


# ---------------------------------------------------------------------------
# 2. archive_task → тот же эффект (админ-закрытие терминально)
# ---------------------------------------------------------------------------

def test_archive_task_stamps_owner_telegram_subscription(board_env):
    root = _mission(board_env)
    assert kb.archive_task(board_env, root)
    subs = _tg_subs(board_env, root)
    assert len(subs) == 1
    assert subs[0]["chat_id"] == OWNER_TG
    assert subs[0]["delivery_mode"] == "notify"


# ---------------------------------------------------------------------------
# 3. Идемпотентность
# ---------------------------------------------------------------------------

def test_repeat_terminal_transition_is_idempotent(board_env):
    root = _mission(board_env)
    kb.complete_task(board_env, root, summary="первое закрытие")
    first = _tg_subs(board_env, root)
    assert len(first) == 1

    # reopen + повторное закрытие: строка не размножается, chat_id тот же.
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (root,),
        )
    assert kb.complete_task(board_env, root, summary="второе закрытие")
    second = _tg_subs(board_env, root)
    assert len(second) == 1, f"повторный переход не должен добавлять подписку: {second}"
    assert second[0]["chat_id"] == first[0]["chat_id"]


def test_repeated_ensure_call_writes_nothing_new(board_env):
    root = _mission(board_env)
    kb.complete_task(board_env, root, summary="готово")
    before = _tg_subs(board_env, root)

    # Прямой повтор той же точки входа (например, ретрай транзакции).
    with kb.write_txn(board_env):
        chat = kb.ensure_terminal_delivery_subscription(board_env, root)
    assert chat == OWNER_TG
    after = _tg_subs(board_env, root)
    assert len(after) == len(before) == 1


# ---------------------------------------------------------------------------
# 4. Fail-closed: нет привязки владельца — нет подписки
# ---------------------------------------------------------------------------

def test_no_owner_binding_creates_no_subscription_and_does_not_raise(board_env):
    root = _mission(board_env, with_binding=False)
    assert kb.complete_task(board_env, root, summary="закрыто без привязки")
    assert _tg_subs(board_env, root) == [], "chat_id не фабрикуется"
    assert kb.terminal_mirror_chats(board_env, root) == set()


def test_card_without_any_subscription_is_untouched(board_env):
    plain = kb.create_task(board_env, title="no-subs", assignee="worker")
    assert kb.complete_task(board_env, plain, summary="готово")
    assert _tg_subs(board_env, plain) == []


# ---------------------------------------------------------------------------
# 5. Native telegram-подписка не дублируется
# ---------------------------------------------------------------------------

def test_existing_native_telegram_subscription_is_not_duplicated(board_env):
    root = _mission(board_env, with_native_tg=True)
    kb.complete_task(board_env, root, summary="готово")
    subs = _tg_subs(board_env, root)
    assert len(subs) == 1, f"native-подписка не должна дублироваться: {subs}"
    assert subs[0]["chat_id"] == OWNER_TG
    # Она осталась КАНАЛОМ: адресного маркера у native-строки нет.
    channel_subs = kb.list_notify_subs(
        board_env, root, include_terminal_mirror=False,
    )
    assert "telegram" in {s["platform"] for s in channel_subs}


# ---------------------------------------------------------------------------
# 6. Один итог на чат: watcher-тик отправляет ровно одно сообщение
# ---------------------------------------------------------------------------

class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.docs = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return SendResult(success=True, message_id=len(self.sent))

    async def send_document(self, chat_id, file_path, caption=None,
                            file_name=None, reply_to=None, metadata=None,
                            **kwargs):
        self.docs.append({"chat_id": chat_id, "file_path": str(file_path)})
        return SendResult(success=True)

    async def handle_message(self, event):
        return None


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.API_SERVER: adapter}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner


async def _run_one_branch_tick(monkeypatch, runner, interval=1):
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


@pytest.mark.asyncio
async def test_terminal_card_receives_exactly_one_message_per_chat(
    board_env, monkeypatch,
):
    staged: list = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    from gateway import kanban_origin_delivery as _kod
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)

    root = _mission(board_env)
    child = kb.create_task(board_env, title="child", assignee="worker")
    kb.link_tasks(board_env, root, child)
    kb.complete_task(board_env, root, summary="контроллер миссии закрыт")
    kb.complete_task(board_env, child, summary="исполнитель отчитался")

    # Подписка терминальной карточки на месте ДО доставки.
    assert [s["chat_id"] for s in _tg_subs(board_env, root)] == [OWNER_TG]

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)

    # Контракт владельца 2026-11-04 («в TG только по делу»): это api_server-
    # подписка OWUI-владельца, итог успешный и БЕЗ файла-артефакта.  Такой
    # итог — служебное сообщение, он не дублируется в TG-зеркало; вердикт
    # остаётся в OWUI-канале.  Проверяемое свойство исходного теста —
    # «ровно одно сообщение на чат, не два» — сохраняется на каждом лейне:
    # OWUI получает итог один раз, TG не получает вовсе.
    assert adapter.sent == [], (
        "текстовый успешный итог не дублируется в TG: "
        f"{adapter.sent}"
    )
    assert len(staged) == 1, "OWUI-лейн получил итог ровно один раз"

    # Повторный тик — ни новой отправки, ни нового стейджа.
    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_address_row_does_not_make_card_a_branch_root(
    board_env, monkeypatch,
):
    """Адресная строка не создаёт «ветку» там, где канала нет."""
    leaf = _mission(board_env)          # api_server-подписка есть
    kb.complete_task(board_env, leaf, summary="закрыто")
    roots = kb.subscribed_branch_roots(board_env)
    assert leaf in roots, "карточка с api_server-каналом остаётся корнем ветки"

    # Карточка ТОЛЬКО с адресной строкой (api_server-канал убран) не корень.
    only_addr = kb.create_task(board_env, title="addr-only", assignee="worker")
    with kb.write_txn(board_env):
        kb._write_notify_sub_row(
            board_env,
            task_id=only_addr,
            platform="telegram",
            chat_id=OWNER_TG,
            user_id=OWNER_TG,
            delivery_mode="notify",
            metadata_json=kb._encode_notify_delivery_metadata(
                {kb._TERMINAL_MIRROR_META_KEY: 1}
            ),
        )
    assert only_addr not in kb.subscribed_branch_roots(board_env)
    assert kb.list_notify_subs(
        board_env, only_addr, include_terminal_mirror=False,
    ) == []
