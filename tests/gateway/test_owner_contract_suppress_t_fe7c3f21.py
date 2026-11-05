"""Регресс P1: подавление по контракту владельца не должно съедать итог.

Карточка t_fe7c3f21 (ремонт дефекта, найденного ревью ревизии ``ebd305b812``,
карточка ревью ``t_87bc0d5b``).

Дефект: при подавлении успешного текстового итога (``owui_healthy=True``,
OWUI-стадия ``pending``) движок оставляет ``tg_delivered=None``, а watcher
считал это «не провал» и писал once-only dedup-маркер ``mirror``. Если затем
OWUI умирал (``unbound``/``failed``), движок был готов доставить
(``owui_healthy=False``), но watcher уже не доходил до него: dedup-маркер
поднимал ветку «уже зеркалено», а гейт ack закрывал канал. Итог терялся на
ОБОИХ лейнах — инцидент ``t_f4e43cb2`` / ``t_08a192ec``.

Требуемое поведение (три состояния вместо двух):
  * доставлено        — ``tg_delivered is True``  -> dedup ``mirror``;
  * подавлено штатно  — ``tg_suppressed is True`` -> журнал пишется ОДИН раз,
                        dedup ``mirror`` НЕ пишется (мёртвый OWUI позже
                        доставит итог в TG);
  * не отправлено     — ``tg_delivered is False`` -> маркер не пишется,
                        канал остаётся pending для повтора.

Все проверки — на уровне watcher (``_kanban_branch_summary_watcher``), а не
движка: прямой вызов ``handle_branch_final`` дыру не ловит
(``test_dead_owui_keeps_tg_mirror_as_last_channel`` зелёный и до ремонта).
"""

import json

import pytest

from hermes_cli import kanban_db as kb

from tests.gateway.test_kanban_branch_summary_watcher import (  # noqa: E402
    RecordingAdapter,
    _complete_branch,
    _make_bs_runner,
    _mk,
    _run_one_branch_tick,
)

SUPPRESS_REASON = "success итог without artifact (owner contract 2026-11-04)"


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "contract_dedup.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _suppression_records():
    """Записи журнала подавления текущего (изолированного) HERMES_HOME."""
    from hermes_constants import get_hermes_home

    log_path = get_hermes_home() / "logs" / "delivery-policy-suppressed.log"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _owner_contract_records():
    return [r for r in _suppression_records() if r.get("reason") == SUPPRESS_REASON]


def _dedup_rows(root_id):
    conn = kb.connect(board="default")
    try:
        rows = conn.execute(
            "SELECT task_id, kind, cause_hash FROM delivery_dedup WHERE task_id LIKE ?",
            (f"branch:{root_id}:%",),
        ).fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


def _pending_subs(root_id, chat_id):
    conn = kb.connect(board="default")
    try:
        marker = kb.has_branch_summary(conn, root_id)
        return marker, kb.branch_summary_pending_subs(
            marker,
            [{"platform": "api_server", "chat_id": chat_id, "thread_id": ""}],
        )
    finally:
        conn.close()


def _setup_branch(conn, chat_id, monkeypatch):
    """OWUI-подписка инициатора + закрытая ветка; TG-зеркало включено."""
    root = _mk(conn, "root")
    a = _mk(conn, "a")
    kb.link_tasks(conn, root, a)
    kb.add_notify_sub(
        conn,
        task_id=root,
        platform="api_server",
        chat_id=chat_id,
        user_id="sess-initiator-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(conn, root, [a])
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    monkeypatch.setenv("HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        json.dumps({"sess-initiator-1": "tg-owner-1"}),
    )
    return root


@pytest.mark.asyncio
async def test_pending_suppression_then_dead_owui_delivers_once(
    board_env, monkeypatch,
):
    """pending (подавлено) -> unbound: итог обязан дойти в TG ровно один раз."""
    from gateway import kanban_origin_delivery as _kod

    chat_id = "owui-tfe7c3f21-1"
    root = _setup_branch(board_env, chat_id, monkeypatch)

    mode = {"state": "pending"}

    def flaky_stage(outbox, registry, sub, profile, board, event_id, message):
        return (mode["state"], event_id if mode["state"] == "acknowledged" else None)

    monkeypatch.setattr(_kod, "stage_terminal", flaky_stage)

    owui = RecordingAdapter()
    tg = RecordingAdapter()
    runner = _make_bs_runner(owui, tg_adapter=tg, profile="01-orchestrator")

    # Тик 1: OWUI жив, но не подтверждён -> успешный текстовый итог подавлен.
    await _run_one_branch_tick(monkeypatch, runner)
    assert tg.sent == [], f"pending: TG-зеркало обязано молчать, got {tg.sent}"

    # Подавление наблюдаемо в журнале РОВНО одной записью.
    records = _owner_contract_records()
    assert len(records) == 1, (
        "подавление по контракту обязано попасть в журнал одной записью: "
        f"{records}"
    )

    # Канал НЕ закрыт: итог ещё не доставлен ни на один лейн.
    _, pending = _pending_subs(root, chat_id)
    assert pending, "канал нельзя акать до фактической доставки итога"

    # Повторные pending-тики: журнал не растёт (однократность сохранена).
    for _ in range(2):
        await _run_one_branch_tick(monkeypatch, runner)
    assert tg.sent == [], f"pending: TG-зеркало по-прежнему молчит: {tg.sent}"
    assert len(_owner_contract_records()) == 1, (
        "каждый pending-тик не должен добавлять новую запись подавления: "
        f"{_owner_contract_records()}"
    )

    # Тик 4: OWUI умер -> TG остаётся последним путём до владельца.
    mode["state"] = "unbound"
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(tg.sent) == 1, (
        "при мёртвом OWUI итог обязан уйти в TG-зеркало; фактически "
        f"TG={tg.sent}, dedup={_dedup_rows(root)}"
    )
    assert "Задача выполнена" in tg.sent[0]["text"]

    # Повтор не дублирует: доставленный итог дедуплицирован.
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(tg.sent) == 1, f"итог доставляется ровно один раз: {tg.sent}"

    # После доставки канал закрывается (owui unbound -> ack).
    marker, pending_after = _pending_subs(root, chat_id)
    assert pending_after == [], (
        f"после доставки канал закрывается: {pending_after}"
    )
    assert marker is not None


@pytest.mark.asyncio
async def test_pending_suppression_not_marked_as_mirrored(board_env, monkeypatch):
    """Подавление не пишет dedup ``mirror`` — иначе мёртвый OWUI не доставит."""
    from gateway import kanban_origin_delivery as _kod

    chat_id = "owui-tfe7c3f21-2"
    root = _setup_branch(board_env, chat_id, monkeypatch)

    def pending_stage(outbox, registry, sub, profile, board, event_id, message):
        return ("pending", None)

    monkeypatch.setattr(_kod, "stage_terminal", pending_stage)

    tg = RecordingAdapter()
    runner = _make_bs_runner(RecordingAdapter(), tg_adapter=tg, profile="01-orchestrator")
    await _run_one_branch_tick(monkeypatch, runner)

    assert tg.sent == []
    mirror_rows = [
        row for row in _dedup_rows(root) if row[2] == "mirror"
    ]
    assert mirror_rows == [], (
        "подавленный итог не зеркален: dedup-маркер mirror писать нельзя, "
        f"иначе доставка при мёртвом OWUI не состоится: {_dedup_rows(root)}"
    )


@pytest.mark.asyncio
async def test_pending_suppression_then_owui_ack_closes_channel(
    board_env, monkeypatch,
):
    """pending (подавлено) -> acknowledged: TG молчит, канал закрывается."""
    from gateway import kanban_origin_delivery as _kod

    chat_id = "owui-tfe7c3f21-3"
    root = _setup_branch(board_env, chat_id, monkeypatch)

    mode = {"state": "pending"}

    def flaky_stage(outbox, registry, sub, profile, board, event_id, message):
        return (mode["state"], event_id if mode["state"] == "acknowledged" else None)

    monkeypatch.setattr(_kod, "stage_terminal", flaky_stage)

    tg = RecordingAdapter()
    runner = _make_bs_runner(RecordingAdapter(), tg_adapter=tg, profile="01-orchestrator")
    await _run_one_branch_tick(monkeypatch, runner)

    # OWUI подтвердил: итог лежит в живом канале -> TG не дублирует, канал ack.
    mode["state"] = "acknowledged"
    await _run_one_branch_tick(monkeypatch, runner)
    await _run_one_branch_tick(monkeypatch, runner)

    assert tg.sent == [], f"подтверждённый OWUI не дублируется в TG: {tg.sent}"
    _, pending = _pending_subs(root, chat_id)
    assert pending == [], f"после OWUI-ack канал закрывается: {pending}"
    assert len(_owner_contract_records()) == 1, (
        "запись подавления остаётся одна: " f"{_owner_contract_records()}"
    )


@pytest.mark.asyncio
async def test_pending_many_ticks_does_not_drop_channel_before_delivery(
    board_env, monkeypatch,
):
    """Подавление не сжигает бюджет MAX_FAILURES (12) на канале.

    Найдено при ремонте t_fe7c3f21 сверх исходного дефекта: каждый pending-тик
    возвращал ``False``, счётчик неудач рос и на 12-м тике watcher принудительно
    акал канал с комментарием ``[delivery-drop]`` — хотя итог не достиг владельца
    ни на одном лейне. Мёртвый OWUI после такого дропа уже не мог доставить
    ничего: канал был закрыт.

    Ожидание: 12 подавленных тиков не дропают канал, а последующая смерть OWUI
    по-прежнему доставляет итог в TG (один раз).
    """
    from gateway import kanban_origin_delivery as _kod

    chat_id = "owui-tfe7c3f21-4"
    root = _setup_branch(board_env, chat_id, monkeypatch)

    mode = {"state": "pending"}

    def flaky_stage(outbox, registry, sub, profile, board, event_id, message):
        return (mode["state"], event_id if mode["state"] == "acknowledged" else None)

    monkeypatch.setattr(_kod, "stage_terminal", flaky_stage)

    tg = RecordingAdapter()
    runner = _make_bs_runner(RecordingAdapter(), tg_adapter=tg, profile="01-orchestrator")
    for _ in range(12):
        await _run_one_branch_tick(monkeypatch, runner)

    assert tg.sent == [], f"подавление не должно пинговать TG: {tg.sent}"
    conn = kb.connect(board="default")
    try:
        comments = kb.list_comments(conn, root)
    finally:
        conn.close()
    drops = [c.body for c in comments if c.body.startswith("[delivery-drop]")]
    assert drops == [], (
        "штатное подавление не является сбоем доставки — канал дропать нельзя: "
        f"{drops}"
    )
    _, pending = _pending_subs(root, chat_id)
    assert pending, "канал обязан остаться pending: итог ещё не доставлен"

    mode["state"] = "unbound"
    for _ in range(2):
        await _run_one_branch_tick(monkeypatch, runner)
    assert len(tg.sent) == 1, (
        "после 12 подавленных тиков смерть OWUI обязана доставить итог в TG: "
        f"{tg.sent}"
    )
