"""F6-style terminal-gate tests for the kanban branch-summary TG mirror
(t_f8f38351, parent t_f285cb14).

Companion suite to tests/gateway/test_kanban_branch_summary_watcher.py,
living next to tests/cron/test_dual_lane_t2.py per the task contract.
Targets the `_branch_summary_terminal` gate introduced for the branch итог
("короткий итог ветки"):

  1) ПРОМЕЖУТОЧНЫЙ итог ветки — [split N/M] или дочерняя карточка с открытым
     предком (миссия выше ещё running) — НЕ доставляется в telegram, а в
     delivery-policy-suppressed.log появляется РОВНО одна запись
     (reason=routine status close, app=kanban, owui_to_tg=True);
  2) Итог ПОЛНОЙ миссии (корень; все вышестоящие карточки закрыты) — telegram
     получает доставку, журнал подавлений пуст;
  3) policy-off / foreign OWUI (нет валидной TG-ленты) — no lane: доставка не
     происходит, журнал пуст (fail-closed, OWUI-only сохраняется).

Предикат `_branch_summary_terminal` покрыт прямыми unit-тестами (быстрое,
без GatewayRunner); наблюдаемое поведение — watcher-уровнем (GatewayRunner +
RecordingAdapter, тот же стек, что в tests/gateway/…, но в cron-дереве).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import _branch_summary_terminal
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

# Same identity constants as tests/cron/test_dual_lane_t2.py §8.5.
OWNER_TG = "125805788"
ALLOWED = [OWNER_TG]
OWNER_OWUI = "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"
FOREIGN_OWUI = "3daba661-feb8-4c1d-a823-4c5223f98d30"


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    """Isolated kanban board: per-test DB + local assignee registry."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "branch.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, assignee="worker"):
    return kb.create_task(conn, title=title, assignee=assignee)


def _mk_bg(conn, title, parent=None, status="running", assignee="worker"):
    """Create a card (optionally linked under parent) and return its id."""
    tid = kb.create_task(
        conn, title=title, assignee=assignee, initial_status=status,
    )
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _complete_branch(conn, root, children):
    for tid in [root] + list(children):
        assert kb.complete_task(conn, tid, summary="done")


def _close_mid_tree(conn, *task_ids):
    """Simulate a terminal close that `complete_task` parent-gating forbids:
    a mid-tree [split N/M] / daughter card closed while its ancestor mission is
    still running (exactly the state the bug report describes)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='done' WHERE id IN (%s)"
            % (",".join("?" * len(task_ids))),
            task_ids,
        )


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_dispatcher_lock_handle = object()  # owns dispatcher -> include unowned subs
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner


async def _run_one_branch_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the branch-summary watcher."""
    real_sleep = asyncio.sleep
    runner._running = True  # a prior tick's cadence sleep clears it

    async def fake_sleep(delay):
        if delay >= 5:
            return None  # startup + cadence; keep running this tick
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


def _read_suppression_journal():
    """Jsonl-записи delivery-policy-suppressed.log в изолированном HERMES_HOME."""
    import json as _json

    from hermes_constants import get_hermes_home

    log_path = get_hermes_home() / "logs" / "delivery-policy-suppressed.log"
    if not log_path.exists():
        return []
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(_json.loads(line))
    return records


def _fake_stage(staged):
    from gateway import kanban_origin_delivery as _kod

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    return _kod, fake_stage


# ---------------------------------------------------------------------------
# Сценарий 1/2 (ядро решения): предикат _branch_summary_terminal —
# [split N/M] / дочерняя карточка с открытым предком vs полная миссия.
# ---------------------------------------------------------------------------

def test_terminal_predicate_top_of_whole_tree_true(board_env):
    # Корень без предков — это И ЕСТЬ терминальное закрытие миссии.
    root = _mk(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    _complete_branch(board_env, root, [a])
    assert _branch_summary_terminal(board_env, kb, root) is True


def test_terminal_predicate_split_open_mission_intermediate(board_env):
    # [split N/M]: сплит-ветка закрыта, а миссия выше ещё running →
    # итог ветки ПРОМЕЖУТОЧНЫЙ (terminal=False).
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    _close_mid_tree(board_env, child, split)
    assert _branch_summary_terminal(board_env, kb, split) is False


def test_terminal_predicate_daughter_card_open_parent_intermediate(board_env):
    # Дочерняя карточка (разовая) закрыта, её предок открыт → промежуточно.
    mission = _mk_bg(board_env, "mission")
    dau = _mk_bg(board_env, "one-off", parent=mission)
    _close_mid_tree(board_env, dau)
    assert _branch_summary_terminal(board_env, kb, dau) is False


def test_terminal_predicate_deep_open_ancestor_intermediate(board_env):
    # Миссия -> сплит -> подзадача: открыт даже дальний предок (миссия) →
    # закрытие самoй нижней подветки не терминально.
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    _close_mid_tree(board_env, child, split)  # миссия остаётся running
    assert _branch_summary_terminal(board_env, kb, child) is False


def test_terminal_predicate_full_mission_closed_terminal(board_env):
    # Вся миссия закрыта (корень + все ветки) → итог ТЕРМИНАЛЬНЫЙ для любого
    # узла поддерева: и для сплита, и для нижней подзадачи.
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    _complete_branch(board_env, mission, [split, child])
    assert _branch_summary_terminal(board_env, kb, child) is True
    assert _branch_summary_terminal(board_env, kb, split) is True
    assert _branch_summary_terminal(board_env, kb, mission) is True


def test_terminal_predicate_sibling_open_keeps_mission_open(board_env):
    # Открытый НЕ-предковый узел в той же миссии: сплит-ветка закрыта, но
    # параллельная ветка ещё running → миссия не закрыта → промежуточно.
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    sibling = _mk_bg(board_env, "split-2", parent=mission)
    _close_mid_tree(board_env, child, split)  # sibling остаётся running
    assert _branch_summary_terminal(board_env, kb, split) is False


# ---------------------------------------------------------------------------
# Сценарий 1 (наблюдаемо): промежуточный итог ветки — telegram НЕ получает
# доставку, delivery-policy-suppressed.log получает РОВНО одну запись.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_summary_intermediate_split_tg_suppressed_with_journal(
    board_env, monkeypatch,
):
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    # Root with a subscription but NO subscribed ancestor: `split` is the
    # branch root (mission above stays unsubscribed and still-running).
    kb.add_notify_sub(
        board_env, task_id=split, platform="telegram", chat_id="tg-chat-1",
    )
    # OWUI subscription on the same branch root (api_server) must NOT be lost.
    kb.add_notify_sub(
        board_env,
        task_id=split,
        platform="api_server",
        chat_id="owui-abc",
        user_id="sess-1",
        delivery_mode="notify+wake",
    )
    _close_mid_tree(board_env, child, split)  # миссия остаётся running

    staged = []
    _kod, fake_stage = _fake_stage(staged)
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    # TG mirror: SUPPRESSED (не терминальный итог).
    assert adapter.sent == [], "промежуточный итог ветки не должен уходить в TG"
    # OWUI (api_server): delivered as before.
    assert len(staged) == 1
    assert staged[0][0] == "owui-abc"
    assert staged[0][1] == "✅ Задача выполнена"

    # Journal: exactly one record for the suppressed TG channel.
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["reason"] == "routine status close"
    assert journal[0]["app"] == "kanban"
    assert journal[0]["target"] == "telegram:tg-chat-1"
    assert journal[0]["task_id"] == split
    assert journal[0]["event_kind"] == "branch_summary"
    assert journal[0]["owui_to_tg"] is True

    # TG-канал acked: повторный тик не дублирует журнал и не шлёт повторно.
    conn2 = kb.connect(board="default")
    try:
        marker = kb.has_branch_summary(conn2, split)
        assert kb.branch_summary_pending_subs(
            marker, [
                {"platform": "telegram", "chat_id": "tg-chat-1", "thread_id": ""},
            ]
        ) == []
    finally:
        conn2.close()
    adapter.sent.clear()
    staged.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [] and staged == []
    assert len(_read_suppression_journal()) == 1


# ---------------------------------------------------------------------------
# Сценарий 2 (наблюдаемо): итог полной миссии (корень, все вышестоящие
# карточки закрыты) — telegram получает доставку, журнал пуст.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_summary_full_mission_tg_delivered_no_journal(
    board_env, monkeypatch,
):
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-chat-9",
    )
    _complete_branch(board_env, root, [a])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)

    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена"]
    assert adapter.sent[0]["chat_id"] == "tg-chat-9"
    assert _read_suppression_journal() == [], (
        "терминальный итог миссии не должен писать в журнал подавлений"
    )


@pytest.mark.asyncio
async def test_branch_summary_full_mission_tg_delivered_with_owui_binding(
    board_env, monkeypatch,
):
    # Терминальный итог при валидной OWUI-привязке владельца: TG-зеркало
    # получает доставку (и OWUI тоже), журнал пуст.
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-chat-bind",
    )
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-owner",
        user_id=OWNER_OWUI,
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    staged = []
    _kod, fake_stage = _fake_stage(staged)
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена"]
    assert adapter.sent[0]["chat_id"] == "tg-chat-bind"
    assert len(staged) == 1  # OWUI-лента сохранена
    assert _read_suppression_journal() == []


# ---------------------------------------------------------------------------
# Сценарий 3: policy-off / foreign OWUI — нет валидной TG-ленты → no lane,
# доставка не происходит, журнал пуст (fail-closed, OWUI-only сохраняется).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_summary_foreign_owui_no_tg_lane_no_delivery(
    board_env, monkeypatch,
):
    # OWUI-миссия чужого (непривязанного) пользователя: telegram-подписки на
    # ветке нет вовсе → TG-ленты нет, доставка в TG не происходит даже для
    # терминального закрытия, журнал пуст (подавлять нечего — fail-closed).
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-foreign",
        user_id=FOREIGN_OWUI,
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    staged = []
    _kod, fake_stage = _fake_stage(staged)
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    assert adapter.sent == [], "чужой OWUI не должен получать TG-доставку"
    assert len(staged) == 1, "OWUI-лента (api_server) для чужого не теряется"
    assert _read_suppression_journal() == [], (
        "без TG-ленты нечего подавлять — журнал должен остаться пустым"
    )


@pytest.mark.asyncio
async def test_branch_summary_policy_off_no_tg_lane_no_delivery(
    board_env, monkeypatch,
):
    # Policy off: OWUI-миссия остаётся OWUI-only — OWUI→TG лента не создаётся,
    # TG-доставки нет, журнал пуст (T2 §8.2.2: policy off → без TG-ленты).
    from gateway.kanban_watchers import _tg_owui_dual_lane

    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )

    # F6-гейт: даже терминальное закрытие корня миссии при policy off не
    # создаёт TG-ленту (возвращает None = no lane).
    sub = {
        "platform": "api_server", "user_id": OWNER_OWUI,
        "chat_id": "owui-chat", "task_id": "t-root",
    }
    ev = SimpleNamespace(
        kind="completed", payload={"summary": "Результат", "artifacts": ["rpt.md"]},
    )
    task = SimpleNamespace(status="done", title="root", result="res")
    out = _tg_owui_dual_lane(
        sub, ev, "completed", "msg", task, list(ALLOWED),
        task_is_root=True, home_channel=OWNER_TG,
    )
    assert out is None, f"policy off → OWUI-only (no TG lane): {out}"
    assert _read_suppression_journal() == []

    # Watcher-уровень: терминальное закрытие с единственной apiserver-подпиской
    # → OWUI доставлен, в TG ничего не ушло, журнал пуст.
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-owner",
        user_id=OWNER_OWUI,
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])
    staged = []
    _kod, fake_stage = _fake_stage(staged)
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    assert adapter.sent == []
    assert len(staged) == 1
    assert _read_suppression_journal() == []
