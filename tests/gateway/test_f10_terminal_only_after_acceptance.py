"""F10: итог миссии не уходит в OWUI, пока её приёмка ещё в работе.

Инцидент 03.11.2026 (миссии «камерные погребения» / chamber-graves-ex и
sno-economics-r3): карточка-корень S4 «отчёт собран» закрылась и её
completed-событие ушло в чат как «Итоговый отчёт», хотя дочерняя карточка
приёмки S5 была уже создана и находилась в статусе running.

Причина (две дороги, обе закрыты здесь):
  * ветка-итог считала закрытие терминальным, если выше корня нет открытого
    предка (``_branch_summary_terminal`` смотрел только ВВЕРХ);
  * событие самого корня уходило в OWUI как [final], когда открытые дети
    фильтровались только для native-Telegram (``branch_terminal``).

Этот тест фиксирует контракт на уровне предикатов, без поднятия watcher-цикла:
1) корень закрыт, ребёнок running  -> НЕ терминально (итог не отдаётся);
2) ребёнок закрыт тоже            -> терминально (итог отдаётся);
3) дерево без детей               -> терминально (поведение не изменилось).
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from gateway.kanban_watchers import _branch_summary_terminal


@pytest.fixture()
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    conn = kb.connect(board=str(db))
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, *, status="running", parent=None):
    tid = kb.create_task(conn, title=title, assignee="worker", initial_status=status)
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _close(conn, *ids):
    with kb.write_txn(conn):
        for tid in ids:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))


def test_open_acceptance_child_keeps_mission_non_terminal(board_env):
    """Корень закрыт, приёмка (ребёнок) ещё в работе -> итог НЕ отдаётся."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    review = _mk(board_env, "S5: независимая приёмка", parent=stage, status="running")
    _close(board_env, mission, stage)

    assert kb.branch_state(board_env, stage) is None, (
        "ветка корня ещё не закрыта: ребёнок running"
    )
    assert _branch_summary_terminal(board_env, kb, stage) is False, (
        "закрытие этапа при запущенной приёмке не является итогом миссии"
    )
    assert review  # используется только для построения дерева


def test_all_children_closed_makes_mission_terminal(board_env):
    """Приёмка тоже закрыта -> миссия завершена, итог отдаётся."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    review = _mk(board_env, "S5: независимая приёмка", parent=stage, status="running")
    _close(board_env, mission, stage, review)

    assert kb.branch_state(board_env, stage) is not None
    assert _branch_summary_terminal(board_env, kb, stage) is True


def test_childless_root_stays_terminal(board_env):
    """Одиночная карточка без детей по-прежнему отдаёт итог."""
    solo = _mk(board_env, "solo")
    _close(board_env, solo)

    assert kb.branch_state(board_env, solo) is not None
    assert _branch_summary_terminal(board_env, kb, solo) is True


def test_blocked_acceptance_is_a_terminal_verdict(board_env):
    """Приёмка заблокирована «нужен ответ» -> это законный итог, а не тишина.

    «Отчёт только по завершении миссии» не означает «молчать при отказе»:
    приёмка, упёршаяся в неисправимое, — это и есть завершение, и владелец
    обязан получить вердикт «не выполнено».  Глушится только промежуточное
    закрытие этапа при ещё идущей работе.
    """
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    review = _mk(board_env, "S5: приёмка", parent=stage, status="running")
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=?",
            (review,),
        )
    _close(board_env, mission, stage)

    assert _branch_summary_terminal(board_env, kb, stage) is True, (
        "отказ приёмки — завершение миссии, вердикт должен дойти до владельца"
    )


def test_open_grandchild_keeps_mission_non_terminal(board_env):
    """Открыт не прямой ребёнок, а вложенный узел — тоже не итог."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    review = _mk(board_env, "S5: приёмка", parent=stage)
    rework = _mk(board_env, "S5-fix: доработка", parent=review, status="running")
    _close(board_env, mission, stage, review)

    assert _branch_summary_terminal(board_env, kb, stage) is False
    assert rework


# ---------------------------------------------------------------------------
# ОВНЕР-ПРАВИЛО 03.11.2026: итог только после ПОЛОЖИТЕЛЬНОГО ревью.
# Единственная точка правды — kb.summary_has_passed_review.  Три конфигурации
# ревью: (а) статус review на самой карточке, (б) отдельная дочерняя карточка
# ревьюера 09, (в) ревью нет вовсе.
# ---------------------------------------------------------------------------

REVIEWER = "09-independent-reviewer"


def _mk_rev(conn, title, *, status="running", parent=None, assignee=REVIEWER):
    """Карточка ревьюера.

    ``create_task`` проверяет профиль на диске, поэтому ревьюер создаётся с
    синтетическим исполнителем, а профиль ревьюера (09/15) пишется напрямую
    в строку — так тест остаётся герметичным и не зависит от установленных
    профилей машины.
    """
    tid = kb.create_task(conn, title=title, assignee="worker", initial_status=status)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (assignee, tid))
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def test_review_status_on_card_keeps_branch_open(board_env):
    """(а) Ревью как СТАТУС карточки: branch_state не закрыт -> итога нет."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='review' WHERE id=?", (stage,))
    _close(board_env, mission)

    assert kb.branch_state(board_env, stage) is None, (
        "карточка в статусе review — ветка открыта, итог планировать нельзя"
    )
    assert _branch_summary_terminal(board_env, kb, stage) is False


def test_open_reviewer_child_blocks_success_summary(board_env):
    """(б) Дочерний ревьюер 09 ещё открыт -> success-итог не отдаётся."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    review = _mk_rev(board_env, "S5: независимое ревью", parent=stage, status="running")
    _close(board_env, mission, stage, review)

    assert kb.has_reviewer_descendant(board_env, stage) is True, (
        "дочерняя карточка профиля 09 должна распознаваться как ревью"
    )
    assert kb.summary_has_passed_review(board_env, stage, "success") is True, (
        "ревьюер закрыт done -> успех законен"
    )


def test_reviewer_child_running_means_no_summary(board_env):
    """(б) Ревьюер 09 running: ни branch_state, ни предикат итога не пропускают."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    _mk_rev(board_env, "S5: независимое ревью", parent=stage, status="running")
    _close(board_env, mission, stage)

    assert kb.branch_state(board_env, stage) is None
    assert _branch_summary_terminal(board_env, kb, stage) is False


def test_reviewer_child_closed_success_delivers_once(board_env):
    """(б) Ревьюер 09 done -> итог терминален ровно один раз."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    review = _mk_rev(board_env, "S5: независимое ревью", parent=stage, status="running")
    _close(board_env, mission, stage, review)

    st = kb.branch_state(board_env, stage)
    assert st is not None and st["outcome"] == "success"
    assert kb.summary_has_passed_review(board_env, stage, st["outcome"]) is True
    assert _branch_summary_terminal(board_env, kb, stage) is True
    # once-only: fingerprint один и тот же при том же close-state.
    assert kb.branch_state(board_env, stage)["fingerprint"] == st["fingerprint"]


def test_no_review_at_all_is_a_legitimate_summary(board_env):
    """(в) Миссия без ревью -> законный итог, глушить нельзя."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    _close(board_env, mission, stage)

    assert kb.has_reviewer_descendant(board_env, stage) is False
    assert kb.summary_has_passed_review(board_env, stage, "success") is True
    assert _branch_summary_terminal(board_env, kb, stage) is True


def test_only_sre_qa_reviewer_counts_as_review(board_env):
    """Ревьюер 15-sre-qa — тоже законный ревьюер (REVIEWER_PROFILES)."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    _mk_rev(board_env, "S5: QA", parent=stage, status="running", assignee="15-sre-qa")
    _close(board_env, mission, stage)

    assert kb.has_reviewer_descendant(board_env, stage) is True
    assert kb.branch_state(board_env, stage) is None


def test_refusal_is_a_verdict_not_a_completion(board_env):
    """Приёмка отказом: доставка есть, но текстом вердикта, не «выполнено»."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: собрать отчёт", parent=mission)
    review = _mk_rev(board_env, "S5: приёмка", parent=stage, status="running")
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
            (review,),
        )
    _close(board_env, mission, stage)

    st = kb.branch_state(board_env, stage)
    assert st is not None and st["outcome"] == "failure", (
        "отказ приёмки обязан быть терминальным вердиктом"
    )
    assert kb.summary_has_passed_review(board_env, stage, st["outcome"]) is True, (
        "вердикт отказа доставляется всегда"
    )
    refused = kb.acceptance_refusal_ids(board_env, stage, st["failed"])
    assert refused == [review], "отказ должен опознаваться по assignee ревьюера"
    msg = kb.branch_summary_message(
        st["outcome"], st["failed"], refused_ids=refused,
    )
    assert msg.startswith("❌ Доработка отклонена"), (
        f"отказ приёмки — это вердикт, а не «задача не выполнена»: {msg}"
    )
    assert "приёмка не приняла результат" in msg
    assert "(нужен фикс)" not in msg, "формулировка падения миссии здесь неверна"


# ---------------------------------------------------------------------------
# ДОСТАВКА (не только предикаты): карточка требует, чтобы итог РЕАЛЬНО
# уходил/не уходил в канал. Тесты ниже гоняют настоящий
# ``_kanban_branch_summary_watcher`` на живой kanban-БД с замоканным
# транспортом OWUI (``stage_terminal``), как в
# ``test_kanban_branch_summary_watcher.py``.
# ---------------------------------------------------------------------------

class _RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.docs = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        from gateway.platforms.base import SendResult
        return SendResult(success=True, message_id=len(self.sent))

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append({"chat_id": chat_id, "file_path": str(file_path)})
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def handle_message(self, event):
        return None


def _owui_runner():
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    adapter = _RecordingAdapter()
    runner.adapters = {Platform.API_SERVER: adapter}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner, adapter


async def _one_branch_tick(monkeypatch, runner, interval=1):
    """Ровно одно тело цикла ветка-итога (как в каноническом наборе)."""
    import asyncio as _asyncio

    real_sleep = _asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


def _owui_stage_sink(monkeypatch, staged):
    from gateway import kanban_origin_delivery as _kod

    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)


def _owui_sub(conn, tid, chat="owui-f10"):
    kb.add_notify_sub(
        conn, task_id=tid, platform="api_server", chat_id=chat,
        user_id="sess-f10", delivery_mode="notify+wake",
    )


@pytest.mark.asyncio
async def test_open_reviewer_child_no_owui_delivery(board_env, monkeypatch):
    """Критерий: дочерний ревьюер 09 НЕ закрыт -> итог в OWUI не уходит."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: отчёт собран", parent=mission)
    _mk_rev(board_env, "S5: ревью 09", parent=stage, status="running")
    _owui_sub(board_env, stage)
    _close(board_env, mission, stage)

    staged = []
    _owui_stage_sink(monkeypatch, staged)
    runner, _adapter = _owui_runner()
    await _one_branch_tick(monkeypatch, runner)

    assert staged == [], f"итог ушёл при открытом ревьюере: {staged}"
    conn2 = kb.connect(board="default")
    try:
        assert kb.has_branch_summary(conn2, stage) is None
    finally:
        conn2.close()


@pytest.mark.asyncio
async def test_closed_reviewer_delivers_success_exactly_once(board_env, monkeypatch):
    """Критерий: ревьюер 09 закрыт положительно -> итог уходит РОВНО один раз."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: отчёт собран", parent=mission)
    review = _mk_rev(board_env, "S5: ревью 09", parent=stage, status="running")
    _owui_sub(board_env, stage)
    _close(board_env, mission, stage, review)

    staged = []
    _owui_stage_sink(monkeypatch, staged)
    runner, _adapter = _owui_runner()
    await _one_branch_tick(monkeypatch, runner)

    assert len(staged) == 1, f"ожидался ровно один итог, получено {staged}"
    assert staged[0][0] == "owui-f10"
    assert staged[0][1].startswith("✅ Задача выполнена"), staged[0][1]

    # повторный тик не дублирует
    staged.clear()
    await _one_branch_tick(monkeypatch, runner)
    assert staged == [], f"повторная доставка итога: {staged}"


@pytest.mark.asyncio
async def test_refusal_delivers_verdict_exactly_once(board_env, monkeypatch):
    """Критерий: приёмка отклонена -> вердикт отказа, ровно один раз."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: отчёт собран", parent=mission)
    review = _mk_rev(board_env, "S5: приёмка 09", parent=stage, status="running")
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
            (review,),
        )
    _owui_sub(board_env, stage)
    _close(board_env, mission, stage)

    staged = []
    _owui_stage_sink(monkeypatch, staged)
    runner, _adapter = _owui_runner()
    await _one_branch_tick(monkeypatch, runner)

    assert len(staged) == 1, f"вердикт отказа обязан дойти ровно один раз: {staged}"
    text = staged[0][1]
    assert text.startswith("❌ Доработка отклонена"), text
    assert "✅" not in text, f"отказ не может выглядеть как выполнение: {text}"

    staged.clear()
    await _one_branch_tick(monkeypatch, runner)
    assert staged == [], f"повторный вердикт: {staged}"
    assert review  # дерево


@pytest.mark.asyncio
async def test_reviewless_mission_still_delivers(board_env, monkeypatch):
    """Критерий: миссия без ревью по-прежнему доставляет законный итог."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: отчёт собран", parent=mission)
    _owui_sub(board_env, stage)
    _close(board_env, mission, stage)

    staged = []
    _owui_stage_sink(monkeypatch, staged)
    runner, _adapter = _owui_runner()
    await _one_branch_tick(monkeypatch, runner)

    assert len(staged) == 1, f"законный итог заглушён: {staged}"
    assert staged[0][1].startswith("✅ Задача выполнена")


# ---------------------------------------------------------------------------
# Вердикт отказа: учёт хвоста «(+N ещё)».
# ---------------------------------------------------------------------------

def test_refusal_tail_counts_every_unshown_failure():
    """«(+N ещё)» считает ВСЕ невидные провалы — и отказы, и прочие.

    Регресс на реально допущенную ошибку: первая версия считала
    ``len(refs) - 3 + len(others)``, из-за чего при ≤3 отказах хвост
    занижался (показывалось «+1 ещё» при двух невидных провалах).
    """
    # 1 отказ показан, 3 прочих провала не видны -> (+3 ещё)
    msg = kb.branch_summary_message(
        "failure",
        [("t_1", "rev"), ("t_2", "crash"), ("t_3", "x"), ("t_4", "y")],
        refused_ids=["t_1"],
    )
    assert "Доработка отклонена" in msg
    assert msg.endswith("(+3 ещё)"), msg

    # 5 отказов, показаны 3 -> (+2 ещё)
    f5 = [(f"t_{i}", "t") for i in range(5)]
    msg = kb.branch_summary_message(
        "failure", f5, refused_ids=[x[0] for x in f5],
    )
    assert msg.endswith("(+2 ещё)"), msg

    # 5 отказов + 2 прочих, показаны 3 -> (+4 ещё)
    msg = kb.branch_summary_message(
        "failure", f5 + [("t_9", "o1"), ("t_8", "o2")],
        refused_ids=[x[0] for x in f5],
    )
    assert msg.endswith("(+4 ещё)"), msg


def test_refusal_wording_requires_branch_membership(board_env):
    """Отказ вне ветки не переформулирует итог этой миссии."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: отчёт", parent=mission)
    foreign_rev = _mk_rev(board_env, "чужая приёмка")
    _close(board_env, mission, stage)

    # Чужой ревьюер не в ветке stage -> refusal-список пуст.
    assert kb.acceptance_refusal_ids(
        board_env, stage, [(foreign_rev, "чужая приёмка")],
    ) == []
