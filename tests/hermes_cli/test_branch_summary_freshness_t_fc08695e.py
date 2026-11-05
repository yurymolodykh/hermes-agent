"""Свежесть итога и административная архивация (t_fc08695e).

Два дефекта, найденные по жалобе владельца «отчёт не должен приходить раньше
времени — ни по старым карточкам, ни пачкой»:

1. **Старый итог.** Ветка, закрытая сутки назад, отдавала итог владельцу
   только когда до неё доходил сканирующий тик — на живой доске 7 из 36
   итогов за сутки ушли одной пачкой в 00:02, спустя 14-23 ч после закрытия.
   Итог старше ``kanban.summary_max_age_hours`` (по умолчанию 6 ч)
   больше не отправляется: канал тихо подтверждается, причина пишется в
   маркер (``skipped_reason``) и в журнал.

2. **Административная архивация.** Массовая архивация пачкой (91 карточка за
   7 секунд в ночь на 04.11) — это уборка, а не завершение миссии. Она больше
   не ставит триггер доставки и не планирует итог для карточки, которая до
   архивации не была закрыта как результат (``done``): статус ``archived``
   сам по себе не является закрытием миссии.

Оба правила живут в одной точке — ``kanban_db.branch_summary_freshness``,
которую читают все три пути доставки (ветка-итог, per-event, ручной
final_report), поэтому разойтись они не могут.
"""

import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board_conn(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    db_path = tmp_path / "freshness.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _branch(conn, *, root_closed_ago_h: float, child_closed_ago_h: float = None):
    """Create a closing branch whose nodes were closed in the PAST."""
    root = kb.create_task(conn, title="корень", assignee="worker")
    child = kb.create_task(conn, title="дочерняя", assignee="worker")
    kb.link_tasks(conn, root, child)
    # Родитель закрывается ПЕРВЫМ: дочерняя карточка ждёт его (dependency).
    # Так ветка приходит к полному закрытию, как в реальной миссии.
    assert kb.complete_task(conn, root, summary="ok")
    assert kb.complete_task(conn, child, summary="ok")
    now = time.time()
    conn.execute(
        "UPDATE tasks SET completed_at = ? WHERE id = ?",
        (now - root_closed_ago_h * 3600, root),
    )
    conn.execute(
        "UPDATE tasks SET completed_at = ? WHERE id = ?",
        (
            now - (child_closed_ago_h if child_closed_ago_h is not None
                   else root_closed_ago_h) * 3600,
            child,
        ),
    )
    # The terminal events carry the same instant as the card close.
    for tid, age_h in (
        (root, root_closed_ago_h),
        (child, child_closed_ago_h if child_closed_ago_h is not None
         else root_closed_ago_h),
    ):
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? "
            "AND kind = 'completed'",
            (now - age_h * 3600, tid),
        )
    conn.commit()
    return root, child


# ---------------------------------------------------------------------------
# branch_summary_freshness — единственная точка решения
# ---------------------------------------------------------------------------

def test_fresh_branch_is_deliverable(board_conn):
    root, _ = _branch(board_conn, root_closed_ago_h=0.5)
    verdict = kb.branch_summary_freshness(board_conn, root, max_age_hours=6)
    assert verdict["deliver"] is True
    assert verdict["age_hours"] is not None and verdict["age_hours"] < 1
    assert verdict["reason"] == "fresh"


def test_stale_branch_is_not_deliverable(board_conn):
    root, _ = _branch(board_conn, root_closed_ago_h=18)
    verdict = kb.branch_summary_freshness(board_conn, root, max_age_hours=6)
    assert verdict["deliver"] is False
    assert verdict["reason"] == "stale"
    assert verdict["age_hours"] == pytest.approx(18, abs=0.2)


def test_freshness_uses_the_last_close_in_the_branch(board_conn):
    """Ветка «старая», но ребёнок закрылся только что → итог ещё свежий.

    Иначе законный итог миссии, чей корень закрыли утром, а приёмку вечером,
    был бы ошибочно заглушён: возраст считается по последнему закрытию.
    """
    root, _ = _branch(board_conn, root_closed_ago_h=30, child_closed_ago_h=0.2)
    verdict = kb.branch_summary_freshness(board_conn, root, max_age_hours=6)
    assert verdict["deliver"] is True
    assert verdict["reason"] == "fresh"


def test_zero_or_negative_limit_disables_the_gate(board_conn):
    root, _ = _branch(board_conn, root_closed_ago_h=900)
    verdict = kb.branch_summary_freshness(board_conn, root, max_age_hours=0)
    assert verdict["deliver"] is True
    assert verdict["reason"] == "disabled"


def test_open_branch_has_no_freshness_verdict(board_conn):
    root = kb.create_task(board_conn, title="открытая", assignee="worker")
    assert kb.branch_summary_freshness(board_conn, root)["deliver"] is True


# ---------------------------------------------------------------------------
# Административная архивация не является закрытием миссии
# ---------------------------------------------------------------------------

def test_archiving_a_never_finished_card_queues_no_delivery(board_conn):
    """Архивация «зависшей» карточки — уборка, а не итог миссии."""
    tid = kb.create_task(board_conn, title="зависшая", assignee="worker")
    kb.add_notify_sub(board_conn, task_id=tid, platform="api_server", chat_id="owui-x")
    assert kb.archive_task(board_conn, tid) is True
    queued = board_conn.execute(
        "SELECT COUNT(*) FROM terminal_delivery_queue WHERE task_id = ?", (tid,)
    ).fetchone()[0]
    assert queued == 0, "архивация незавершённой карточки не должна ставить триггер"


def test_archiving_a_completed_card_still_queues_delivery(board_conn):
    """Карточка была ``done`` → архивация лишь убирает её с доски, итог нужен."""
    tid = kb.create_task(board_conn, title="готовая", assignee="worker")
    kb.add_notify_sub(board_conn, task_id=tid, platform="api_server", chat_id="owui-x")
    assert kb.complete_task(board_conn, tid, summary="ok")
    board_conn.execute(
        "DELETE FROM terminal_delivery_queue WHERE task_id = ?", (tid,)
    )
    board_conn.commit()
    assert kb.archive_task(board_conn, tid) is True
    queued = board_conn.execute(
        "SELECT COUNT(*) FROM terminal_delivery_queue WHERE task_id = ?", (tid,)
    ).fetchone()[0]
    assert queued == 1


def _queued(conn, tid) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM terminal_delivery_queue WHERE task_id = ?", (tid,)
    ).fetchone()[0]


def test_admin_archive_of_a_closed_branch_is_not_a_new_mission_close(board_conn):
    """Закрытая ветка, убранная архивацией, нового итога не даёт, но и не теряет его."""
    root, _child = _branch(board_conn, root_closed_ago_h=1)
    kb.add_notify_sub(board_conn, task_id=root, platform="api_server", chat_id="owui-x")
    assert kb.archive_task(board_conn, root) is True
    decision = kb.branch_summary_decision(board_conn, root)
    assert decision is not None and decision["deliver"] is True
    # Архивация ПРЯЧЕТ уже отданный результат: триггер ставится один раз,
    # повторного «итога по старой карточке» из архивации не возникает.
    assert _queued(board_conn, root) == 1


def test_admin_archive_of_an_unfinished_card_is_flagged(board_conn):
    """Архивация висяка — уборка доски, а не завершение миссии."""
    tid = kb.create_task(board_conn, title="висяк", assignee="worker")
    board_conn.execute(
        "UPDATE tasks SET status='blocked', block_kind='capability' WHERE id = ?",
        (tid,),
    )
    board_conn.commit()
    assert kb.archive_task(board_conn, tid) is True
    assert _queued(board_conn, tid) == 0
