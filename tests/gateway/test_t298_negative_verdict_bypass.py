"""F10-FIX (t_5f76c8d0): «done» карточки ревьюера ≠ «приёмка приняла».

Происхождение: независимая проверка t_298a2513 (профиль 09) нашла, что
``summary_has_passed_review`` отвечал на вопрос «карточка ревьюера закрыта?»
(``status ∈ BRANCH_TERMINAL_SUCCESS``), а не «ревью закончилось положительно?»
Контракт профиля 09/15 обратный: ОТРИЦАТЕЛЬНЫЙ вердикт тоже ставится через
``kanban_complete`` — карточка становится ``done``, вердикт лежит в
``task_runs.metadata["verdict"]``. Итог: 32 живые пары (root, reviewer), где
«✅ Задача выполнена» ушло в канал ПОСЛЕ ``revision_required`` / ``CHANGES``.

Этот файл — тот самый репро независимого ревьюера
(``C:/Users/hermes/workspaces/09-independent-reviewer/
test_t298_negative_verdict_bypass.py``), перевёрнутый в регрессию: на
исправленной ревизии успешный итог НЕ ставится, а отказ доставляется
вердиктом «❌ Доработка отклонена».

Проверяется на УРОВНЕ ДОСТАВКИ (реальный ``_kanban_branch_summary_watcher``,
замокан только ``stage_terminal`` OWUI), а не только на предикате: именно
доставка была дефектом, предикат — его причиной.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb


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


def _mk(conn, title, *, status="running", parent=None, assignee="worker", reviewer=None):
    tid = kb.create_task(conn, title=title, assignee=assignee, initial_status=status)
    if reviewer:
        # create_task validates the profile on disk; write the reviewer profile
        # directly so this control pass stays hermetic (same trick as the file
        # under review, tests/gateway/test_f10_terminal_only_after_acceptance.py).
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (reviewer, tid))
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _close(conn, *ids):
    with kb.write_txn(conn):
        for tid in ids:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))


class _Rec:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.docs = []

    async def send(self, chat_id, text, metadata=None):
        from gateway.platforms.base import SendResult
        self.sent.append({"chat_id": chat_id, "text": text})
        return SendResult(success=True, message_id=len(self.sent))

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        from gateway.platforms.base import SendResult
        self.docs.append({"chat_id": chat_id, "file_path": str(file_path)})
        return SendResult(success=True)

    async def handle_message(self, event):
        return None


def _runner():
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    r = GatewayRunner.__new__(GatewayRunner)
    r._running = True
    a = _Rec()
    r.adapters = {Platform.API_SERVER: a}
    r._kanban_dispatcher_lock_handle = object()
    r._kanban_sub_fail_counts = {}
    r._kanban_branch_summary_fail_counts = {}
    r._profile_adapters = {}
    return r, a


async def _one_tick(monkeypatch, runner, interval=1):
    import asyncio as _a
    real = _a.sleep
    runner._running = True

    async def fake_sleep(d):
        if d >= 5:
            return None
        runner._running = False
        await real(0)

    monkeypatch.setattr(_a, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


def _sink(monkeypatch, staged):
    from gateway import kanban_origin_delivery as kod
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(kod, "stage_terminal", fake)


def _sub(conn, tid, chat="owui-t298"):
    kb.add_notify_sub(conn, task_id=tid, platform="api_server", chat_id=chat,
                      user_id="sess-t298", delivery_mode="notify+wake")


def _give_reviewer_a_verdict(conn, review_id, verdict, *, summary=None):
    """Emulate what ``kanban_complete(metadata={"verdict": …})`` leaves behind."""
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at,"
            " outcome, summary, metadata) VALUES (?,?,?,?,?,?,?,?)",
            (review_id, "09-independent-reviewer", "completed", 1, 2,
             "completed", summary or f"Вердикт: {verdict}",
             json.dumps({"verdict": verdict}, ensure_ascii=False)),
        )
        conn.execute(
            "UPDATE tasks SET status='done', current_run_id=? WHERE id=?",
            (cur.lastrowid, review_id),
        )


def _mission_with_review(board_env, title="S5: приёмка 09"):
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: отчёт собран", parent=mission)
    review = _mk(board_env, title, parent=stage, reviewer="09-independent-reviewer")
    _sub(board_env, stage)
    _close(board_env, mission, stage)
    return stage, review


@pytest.mark.asyncio
async def test_negative_verdict_on_done_reviewer_never_delivers_success(
    board_env, monkeypatch,
):
    """F10-FIX: done + metadata.verdict=revision_required ⇒ НЕ «✅ выполнено».

    До исправления (репро t_298a2513): staged == [(…, '✅ Задача выполнена', …)].
    После: предикат False, а владелец получает вердикт отказа — не тишину.
    """
    stage, review = _mission_with_review(board_env)
    _give_reviewer_a_verdict(board_env, review, "revision_required")

    st = kb.branch_state(board_env, stage)
    assert st is not None, "ветка формально закрыта"
    passed = kb.summary_has_passed_review(board_env, stage, st["outcome"])
    assert passed is False, "ревью с отрицательным вердиктом не пройдено"

    staged = []
    _sink(monkeypatch, staged)
    runner, _ad = _runner()
    await _one_tick(monkeypatch, runner)

    assert staged, "вердикт отказа обязан дойти до владельца (не тишина)"
    text = staged[0][1]
    assert not text.startswith("✅ Задача выполнена"), text
    assert text.startswith("❌ Доработка отклонена"), text
    assert review in text, f"вердикт должен называть карточку ревьюера: {text}"


@pytest.mark.asyncio
async def test_positive_verdict_delivers_success_exactly_once(board_env, monkeypatch):
    """Положительный контроль: verdict=approved ⇒ «✅ Задача выполнена» ровно раз."""
    stage, review = _mission_with_review(board_env)
    _give_reviewer_a_verdict(board_env, review, "approved")

    assert kb.summary_has_passed_review(
        board_env, stage, kb.branch_state(board_env, stage)["outcome"],
    ) is True

    staged = []
    _sink(monkeypatch, staged)
    runner, _ad = _runner()
    await _one_tick(monkeypatch, runner)

    assert len(staged) == 1, staged
    assert staged[0][1] == "✅ Задача выполнена"

    await _one_tick(monkeypatch, runner)
    assert len(staged) == 1, f"повторный тик не должен дублировать итог: {staged}"


@pytest.mark.asyncio
async def test_live_spelling_of_a_rejection_is_recognized(board_env, monkeypatch):
    """Живые написания отказа (``CHANGES``/``REWORK``/``ДОРАБОТАТЬ``) — отказ.

    На живой доске один и тот же класс записан 12+ способами; равенство одной
    константе здесь не работает — проверяется нормализация.
    """
    for verdict in ("CHANGES", "REWORK", "changes", "REQUEST_CHANGES",
                    "revision_required", "ДОРАБОТАТЬ", "changes (revision_required)"):
        stage, review = _mission_with_review(board_env, f"приёмка {verdict}")
        _give_reviewer_a_verdict(board_env, review, verdict)
        assert kb.summary_has_passed_review(
            board_env, stage, kb.branch_state(board_env, stage)["outcome"],
        ) is False, f"вердикт {verdict!r} должен считаться отказом"


@pytest.mark.asyncio
async def test_live_spelling_of_an_approval_is_recognized(board_env, monkeypatch):
    """Живые написания приёмки (``PASS``/``ok``/``APPROVED``) — приёмка."""
    for verdict in ("PASS", "ok", "APPROVED", "approve", "APPROVED_WITH_LIMITATIONS",
                    "approved_with_limitations", "PASS (approved_with_limitations)",
                    "ok (approved)"):
        stage, review = _mission_with_review(board_env, f"приёмка {verdict}")
        _give_reviewer_a_verdict(board_env, review, verdict)
        assert kb.summary_has_passed_review(
            board_env, stage, kb.branch_state(board_env, stage)["outcome"],
        ) is True, f"вердикт {verdict!r} должен считаться приёмкой"


@pytest.mark.asyncio
async def test_missing_verdict_on_done_reviewer_still_delivers(board_env, monkeypatch):
    """Принятое решение: отсутствие вердикта + done ⇒ итог доставляется.

    Явное решение (не «положительно по факту done» молча): 62 из 131 последних
    закрытых карточек ревьюеров на живой доске не несут машиночитаемого
    вердикта, и глушить их итоги запрещено правилом владельца №4. Класс
    записывается в gate payload как ``missing`` — решение аудируемо.
    """
    stage, review = _mission_with_review(board_env)
    _close(board_env, review)  # done без run'а и без вердикта

    gate = kb.branch_review_gate(board_env, stage, "success")
    assert gate["passed"] is True
    assert gate["reason"] == "verdict_unknown"
    assert gate["verdict_class"] == kb.REVIEW_VERDICT_MISSING

    staged = []
    _sink(monkeypatch, staged)
    runner, _ad = _runner()
    await _one_tick(monkeypatch, runner)
    assert len(staged) == 1
    assert staged[0][1] == "✅ Задача выполнена"


def test_negated_failure_phrase_is_not_a_rejection():
    """«no critical FAIL» — не отказ: отрицание перед фразой учитывается."""
    assert kb.classify_review_verdict("READY for production rollout - no critical FAIL") \
        == kb.REVIEW_VERDICT_POSITIVE
    assert kb.classify_review_verdict("PASS x4 (D3, D5)") == kb.REVIEW_VERDICT_POSITIVE
    assert kb.classify_review_verdict("CHANGES (содержание PASS, требуются правки)") \
        == kb.REVIEW_VERDICT_NEGATIVE


def test_reviewer_open_state_fails_closed(board_env):
    """Незакрытый ревьюер при формально success-ветке — итог не отдаётся."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4", parent=mission)
    review = _mk(board_env, "S5", parent=stage, reviewer="09-independent-reviewer")
    _close(board_env, mission, stage)
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='done' WHERE id=?", (review,))
    # Форсируем «противоречивое» состояние: ревьюер done, но ветка success
    # достижима только так; гейт обязан ответить fail-closed на открытом
    # ревьюере отдельно.
    kb_summary = kb.branch_summary_decision(board_env, stage)
    assert kb_summary is not None and kb_summary["deliver"] is True
    # Отдельно: открытая карточка ревьюера → gate False.
    review2 = _mk(board_env, "S5b", parent=stage, reviewer="15-sre-qa",
                  status="running")
    gate = kb.branch_review_gate(board_env, stage, "success")
    assert gate["passed"] is False
    assert gate["reason"] == "reviewer_open"
    assert gate["decided_by"]["task_id"] == review2


def test_later_positive_verdict_supersedes_an_earlier_rejection(board_env):
    """Перепроверка после доработки: поздний положительный вердикт открывает итог."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4", parent=mission)
    first = _mk(board_env, "S5: ревью (1)", parent=stage,
                reviewer="09-independent-reviewer")
    second = _mk(board_env, "S5: ревью (2)", parent=stage,
                 reviewer="09-independent-reviewer")
    _close(board_env, mission, stage)
    with kb.write_txn(board_env):
        for tid, verdict, ts in ((first, "revision_required", 1), (second, "approved", 2)):
            cur = board_env.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at,"
                " ended_at, outcome, summary, metadata) VALUES (?,?,?,?,?,?,?,?)",
                (tid, "09-independent-reviewer", "completed", ts, ts,
                 "completed", f"Вердикт: {verdict}",
                 json.dumps({"verdict": verdict}, ensure_ascii=False)),
            )
            board_env.execute(
                "UPDATE tasks SET status='done', current_run_id=? WHERE id=?",
                (cur.lastrowid, tid),
            )

    gate = kb.branch_review_gate(board_env, stage, "success")
    assert gate["passed"] is True, gate
    assert gate["decided_by"]["task_id"] == second
    assert gate["refusal_ids"] == [first]


def test_later_rejection_relocks_the_itog(board_env):
    """Обратный порядок: поздний отказ снова закрывает успешный итог."""
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4", parent=mission)
    first = _mk(board_env, "S5: ревью (1)", parent=stage,
                reviewer="09-independent-reviewer")
    second = _mk(board_env, "S5: ревью (2)", parent=stage,
                 reviewer="09-independent-reviewer")
    _close(board_env, mission, stage)
    with kb.write_txn(board_env):
        for tid, verdict, ts in ((first, "approved", 1), (second, "revision_required", 2)):
            cur = board_env.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at,"
                " ended_at, outcome, summary, metadata) VALUES (?,?,?,?,?,?,?,?)",
                (tid, "09-independent-reviewer", "completed", ts, ts,
                 "completed", f"Вердикт: {verdict}",
                 json.dumps({"verdict": verdict}, ensure_ascii=False)),
            )
            board_env.execute(
                "UPDATE tasks SET status='done', current_run_id=? WHERE id=?",
                (cur.lastrowid, tid),
            )

    gate = kb.branch_review_gate(board_env, stage, "success")
    assert gate["passed"] is False
    assert gate["reason"] == "negative_verdict"
    assert gate["decided_by"]["task_id"] == second


# ---------------------------------------------------------------------------
# Критерий приёмки №4: отказ приёмки НЕ глушится, только меняется формулировка
# (владелец: «молчание недопустимо»).  Путь (б) не должен давать ни ложного
# «✅», ни тишины.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_negative_verdict_delivers_a_refusal_not_silence(
    board_env, monkeypatch,
):
    """Закрытый done ревьюер с отказом ⇒ канал получает «❌ Доработка отклонена».

    Это и есть решение по п.4 карточки: success-путь для такого корня закрыт
    (ни одного «✅»), но владелец всё равно получает ВИДИМЫЙ вердикт, названный
    по карточке ревьюера, — а не тишину.
    """
    mission = _mk(board_env, "mission")
    stage = _mk(board_env, "S4: отчёт собран", parent=mission)
    review = _mk(board_env, "S5: приёмка 09", parent=stage,
                 reviewer="09-independent-reviewer")
    _sub(board_env, stage)
    _close(board_env, mission, stage)
    _give_reviewer_a_verdict(board_env, review, "revision_required")

    decision = kb.branch_summary_decision(board_env, stage)
    assert decision is not None
    assert decision["deliver"] is True, "отказ не глушится"
    assert decision["kind"] == "refusal"
    assert decision["refused_ids"] == [review]
    assert decision["msg"].startswith("❌ Доработка отклонена")

    staged = []
    _sink(monkeypatch, staged)
    runner, _ad = _runner()
    await _one_tick(monkeypatch, runner)

    assert len(staged) == 1, staged
    text = staged[0][1]
    assert text.startswith("❌ Доработка отклонена"), text
    assert review in text, "вердикт отказа обязан назвать карточку ревьюера"


def test_positive_verdict_still_delivers_exactly_one_success(board_env):
    """Положительный контроль: approved ⇒ ровно один «✅», гейт не мешает."""
    stage, review = _mission_with_review(board_env)
    _give_reviewer_a_verdict(board_env, review, "approved")

    decision = kb.branch_summary_decision(board_env, stage)
    assert decision is not None and decision["deliver"] is True
    assert decision["kind"] == "success"
    assert decision["msg"] == "✅ Задача выполнена"
    assert kb.summary_has_passed_review(board_env, stage, "success") is True


def test_deliver_and_review_passed_are_different_questions(board_env):
    """``deliver`` ≠ ``review_passed`` — разные вопросы, их нельзя путать.

    Обе дороги доставки читают этот payload. Ветка-итог спрашивает «можно ли
    отдать ✅» (``deliver and kind == success``), а per-event [final] — строго
    «ревью пройдено» (``review_passed``). Отказ приёмки доставляем
    (``deliver=True``), но ревью НЕ пройдено: если per-event слой подставит
    ``deliver`` вместо ``review_passed``, «✅ выполнено» вернётся — это и был
    дефект, который здесь пиннится.
    """
    stage, review = _mission_with_review(board_env)
    _give_reviewer_a_verdict(board_env, review, "revision_required")

    decision = kb.branch_summary_decision(board_env, stage)
    assert decision is not None
    assert decision["deliver"] is True, "отказ не глушится"
    assert decision["review_passed"] is False, "ревью не пройдено"
    assert decision["kind"] == "refusal"
    # Инвариант: успешный итог невозможен без пройденного ревью.
    assert not (decision["kind"] == "success" and not decision["review_passed"])


def test_marker_payload_records_the_review_decision(board_env):
    """Решение по ревью попадает в marker — аудируемо, а не «по факту done»."""
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    stage, review = _mission_with_review(board_env)
    _sub(board_env, stage)
    _give_reviewer_a_verdict(board_env, review, "revision_required")

    runner, _ad = _runner()
    plan = runner._plan_branch_summary(
        board_env, stage,
        kb.list_notify_subs(board_env, stage), "default",
    )
    assert plan is not None
    assert plan["kind"] == "refusal"

    marker = kb.has_branch_summary(board_env, stage)
    assert marker is not None
    assert marker["kind"] == "refusal"
    assert marker["review"]["passed"] is False
    assert marker["review"]["verdict_class"] == kb.REVIEW_VERDICT_NEGATIVE
    assert marker["review"]["reason"] == "negative_verdict"
    # outcome остаётся аудиторским значением branch_state
    assert marker["outcome"] == "success"
