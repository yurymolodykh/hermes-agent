"""F3 slice 3 (t_cdb88a42): PWA-подписки привязаны к owui_user_id для TG-доставки.

Acceptance (карточка t_cdb88a42):
  1) второй тестовый пользователь с привязкой получает итоги/артефакты/blocked
     через PWA-подписку (api_server, БЕЗ ``user_id``, только серверный маркер
     ``delivery_metadata[owui_user_id_attr]``);
  2) нет привязки -> нет доставки (fail-closed);
  3) существующие 233+ подписки (user_id-владелец) остаются валидными —
     backward compatibility (user_id приоритетнее маркера);
  + storage-раунд-трип маркера через ``kanban_db.set_notify_sub_owui_owner``.

Изоляция: временные БД/файлы, патч ``stage_terminal``, никакой записи в прод.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

# ---------------------------------------------------------------------------
# Fixtures/helpers (совместимы с test_kanban_mission_chat_silence_dual_lane.py)
# ---------------------------------------------------------------------------

OWNER_OWUI = "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"
OWNER_TG = "tg-owner-100200300"
# Второй привязанный пользователь (Мария) — acceptance 1.
FOREIGN_OWUI = "3daba661-feb8-4c1d-a823-4c5223f98d30"
FOREIGN_TG = "tg-maria-400500600"


@pytest.fixture
def pwa_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "pwa.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv("HERMES_OPENWEBUI_TELEGRAM_USERS",
                       json.dumps({OWNER_OWUI: OWNER_TG, FOREIGN_OWUI: FOREIGN_TG}))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join([OWNER_TG, FOREIGN_TG]))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.docs = []
        self.wakes = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append({"chat_id": chat_id, "file_path": file_path,
                          "file_name": file_name or os.path.basename(file_path),
                          "metadata": metadata or {}})
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def handle_message(self, event):
        self.wakes.append(getattr(event, "text", ""))


def _make_runner(adapters):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
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


def _mk(conn, title, assignee="worker"):
    return kb.create_task(conn, title=title, assignee=assignee,
                          initial_status="running")


def _pwa_sub(conn, tid, chat, user_id=None, metadata=None):
    """PWA/api_server subscription WITHOUT user_id — pure marker attribution."""
    kb.add_notify_sub(
        conn, task_id=tid, platform="api_server", chat_id=chat,
        user_id=user_id, delivery_mode="notify+wake",
        delivery_metadata=metadata,
    )


def _complete(conn, tid, *, summary="done", artifacts=None):
    metadata = {"artifacts": artifacts} if artifacts is not None else None
    return kb.complete_task(conn, tid, summary=summary, metadata=metadata)


def _setup_staged_sink(monkeypatch, staged):
    from gateway import kanban_origin_delivery as _kod
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), sub.get("user_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)


# ---------------------------------------------------------------------------
# 1. kanban_db storage round-trip: nullable owui_user_id attribute on a sub
# ---------------------------------------------------------------------------

def test_set_clear_owui_owner_marker_round_trip(pwa_env):
    """nullable-attribute: маркер пишется, читается и очищается (fail-closed)."""
    conn = pwa_env
    tid = _mk(conn, "storage-t")
    _pwa_sub(conn, tid, "pwa-chat-1")  # без user_id, без маркера

    # Пустой owui_user_id -> False-подобно: маркер не создаётся, подписка не
    # становится owui-привязанной (fail-closed).
    assert kb.set_notify_sub_owui_owner(
        conn, task_id=tid, platform="api_server", chat_id="pwa-chat-1",
        thread_id=None, owui_user_id="   ",
    ) is True
    subs = kb.list_notify_subs(conn, tid)
    assert subs[0]["delivery_metadata"] not in ({"owui_user_id_attr": "   "},)
    meta = subs[0]["delivery_metadata"] or {}
    assert "owui_user_id_attr" not in meta

    # Установка маркера.
    assert kb.set_notify_sub_owui_owner(
        conn, task_id=tid, platform="api_server", chat_id="pwa-chat-1",
        thread_id=None, owui_user_id=FOREIGN_OWUI,
    ) is True
    subs = kb.list_notify_subs(conn, tid)
    assert subs[0]["delivery_metadata"]["owui_user_id_attr"] == FOREIGN_OWUI

    # Очистка маркера.
    assert kb.set_notify_sub_owui_owner(
        conn, task_id=tid, platform="api_server", chat_id="pwa-chat-1",
        thread_id=None, owui_user_id="",
    ) is True
    subs = kb.list_notify_subs(conn, tid)
    meta = subs[0]["delivery_metadata"] or {}
    assert "owui_user_id_attr" not in meta

    # Несуществующая подписка -> False, без исключений.
    assert kb.set_notify_sub_owui_owner(
        conn, task_id=tid, platform="api_server", chat_id="no-such-chat",
        thread_id=None, owui_user_id=FOREIGN_OWUI,
    ) is False


def test_verified_origin_sub_write_seam_persists_marker(pwa_env, monkeypatch):
    """Server-side seam (tools.kanban_origin_subscribe) проставляет маркер."""
    from tools import kanban_origin_subscribe as _kos
    from gateway import origin_binding as _ob

    conn = pwa_env
    tid = _mk(conn, "origin-t")

    monkeypatch.setattr(
        _ob, "resolve_origin_binding",
        lambda: {"chat_id": "owui-00000000-0000-0000-0000-000000000001",
                 "session_id": "owui-00000000-0000-0000-0000-000000000001",
                 "user_id": FOREIGN_OWUI, "profile": "worker"},
    )
    assert _kos.subscribe_verified_origin(conn, tid) is True
    subs = kb.list_notify_subs(conn, tid)
    assert subs[0]["user_id"] == FOREIGN_OWUI
    assert subs[0]["delivery_metadata"]["owui_user_id_attr"] == FOREIGN_OWUI


# ---------------------------------------------------------------------------
# 2. Acceptance 1: marker-only PWA sub (no user_id) -> user's own TG + OWUI
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pwa_marker_only_sub_receives_itog_and_artifacts_to_own_tg(
    pwa_env, monkeypatch,
):
    """Второй пользователь (Мария) через PWA-подписку получает итог + артефакт
    в СВОЙ TG-чат (не home/Юрия): маркер ``owui_user_id_attr`` без ``user_id``.
    """
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    import tempfile

    conn = pwa_env
    root = _mk(conn, "mission-maria-pwa")
    # PWA-подписка: БЕЗ user_id, только серверный маркер привязки к Марии.
    _pwa_sub(conn, root, "pwa-maria-request",
             user_id=None,
             metadata={kb._OWUI_ATTR_META_KEY: FOREIGN_OWUI})
    report = os.path.join(tempfile.mkdtemp(), "maria-report.md")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("Отчёт Марии (PWA).\n")
    assert _complete(conn, root, summary="Миссия Марии завершена (PWA)",
                     artifacts=[report])

    from gateway import kanban_origin_delivery as _kod
    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _make_adapter_pair()
    await _run_one_branch_tick(monkeypatch, runner)

    assert branch_staged, f"OWUI итог должен быть staged: {branch_staged}"
    assert branch_staged[0][0] == "pwa-maria-request"
    # TG mirror: только СВОЙ чат Марии.
    tg_chats = [s["chat_id"] for s in tg.sent]
    assert tg_chats == [FOREIGN_TG], f"должен быть только FOREIGN_TG: {tg.sent}"
    assert "Миссия Марии завершена" in tg.sent[0]["text"]
    # Артефакт едет как файл.
    assert [d["file_name"] for d in tg.docs] == ["maria-report.md"]


def _make_adapter_pair():
    owui = RecordingAdapter()
    owui.supports_async_delivery = False
    tg = RecordingAdapter()
    return owui, tg, _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})


@pytest.mark.asyncio
async def test_pwa_marker_only_blocked_routes_to_own_tg(pwa_env, monkeypatch):
    """blocked (нужно действие) PWA-подписчика уходит в ЕГО TG-чат."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    conn = pwa_env
    root = _mk(conn, "mission-maria-block-pwa")
    _pwa_sub(conn, root, "pwa-maria-block",
             user_id=None,
             metadata={kb._OWUI_ATTR_META_KEY: FOREIGN_OWUI})
    kb.block_task(conn, root, reason="нужно решение Марии", kind="needs_input")

    from gateway import kanban_origin_delivery as _kod
    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _make_adapter_pair()
    await _run_one_branch_tick(monkeypatch, runner)

    # Primary OWUI lane: staged to Maria's PWA chat.
    assert branch_staged, f"blocked должен быть staged в OWUI: {branch_staged}"
    # TG mirror: блокировка достигает ЕЁ чата (нужно действие — loud).
    tg_chats = [s["chat_id"] for s in tg.sent]
    assert tg_chats == [FOREIGN_TG], f"blocked должен идти в FOREIGN_TG: {tg.sent}"


# ---------------------------------------------------------------------------
# 3. Acceptance 2: no mapping -> no delivery (fail-closed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pwa_marker_no_binding_stays_silent(pwa_env, monkeypatch):
    """Маркер привязан к пользователю БЕЗ записи в mapping -> TG-тишина."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    conn = pwa_env
    root = _mk(conn, "mission-unmapped-pwa")
    # Ксения (k_owui) — НЕ в mapping pwa_env (только owner+maria).
    KSENIA_OWUI = "0795993b-4cc5-431f-a7fb-45b18967a9ba"
    _pwa_sub(conn, root, "pwa-ksenia-request",
             user_id=None,
             metadata={kb._OWUI_ATTR_META_KEY: KSENIA_OWUI})
    assert _complete(conn, root, summary="миссия Ксении завершена")

    from gateway import kanban_origin_delivery as _kod
    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _make_adapter_pair()
    await _run_one_branch_tick(monkeypatch, runner)

    # OWUI (primary) lane работает всегда.
    assert branch_staged, "OWUI lane must still deliver for unmapped PWA"
    # TG mirror: МОЛЧИТ (нет привязки -> fail-closed).
    assert tg.sent == [], f"unmapped PWA должен молчать в TG: {tg.sent}"


@pytest.mark.asyncio
async def test_pwa_no_marker_at_all_stays_owui_only(pwa_env, monkeypatch):
    """Совсем без маркера и без user_id -> OWUI-only, TG-тишина."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    conn = pwa_env
    root = _mk(conn, "mission-nomarker-pwa")
    _pwa_sub(conn, root, "pwa-nomarker", user_id=None, metadata=None)
    assert _complete(conn, root, summary="миссия без маркера завершена")

    from gateway import kanban_origin_delivery as _kod
    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _make_adapter_pair()
    await _run_one_branch_tick(monkeypatch, runner)

    assert branch_staged, "OWUI lane всегда доставляет"
    assert tg.sent == [], f"без маркера -> TG-молчание: {tg.sent}"


# ---------------------------------------------------------------------------
# 4. Acceptance 3: existing user_id-based subs remain valid (backward compat)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_user_id_sub_unaffected_by_marker(pwa_env, monkeypatch):
    """Штатная user_id-подписка (как 234 боевые) НЕ читает маркер — user_id
    приоритетнее.  Подписка с чужим маркером, но СВОИМ user_id доставляет в СВОЙ
    чат (маркер игнорируется): существующие подписки остаются валидными.
    """
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    conn = pwa_env
    root = _mk(conn, "mission-legacy-pwa")
    # user_id = OWNER_OWUI, но маркер (ошибочно) = FOREIGN_OWUI.  Приоритет
    # user_id: итог должен идти в OWNER_TG, НЕ в FOREIGN_TG.
    _pwa_sub(conn, root, "owui-legacy-request",
             user_id=OWNER_OWUI,
             metadata={kb._OWUI_ATTR_META_KEY: FOREIGN_OWUI})
    assert _complete(conn, root, summary="миссия legacy завершена")

    from gateway import kanban_origin_delivery as _kod
    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _make_adapter_pair()
    await _run_one_branch_tick(monkeypatch, runner)

    assert branch_staged, f"OWUI итог: {branch_staged}"
    tg_chats = [s["chat_id"] for s in tg.sent]
    assert tg_chats == [OWNER_TG], (
        f"user_id-подписка должна идти в OWNER_TG (user_id приоритетнее "
        f"маркера): {tg.sent}"
    )
