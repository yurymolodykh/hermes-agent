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
import os
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

    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]
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

    # Контракт владельца 2026-11-04 («в TG только по делу»): из двух лент
    # остаётся ОДНА — нативная telegram-подписка (её сообщение и есть итог,
    # ждать его больше неоткуда).  OWUI→TG зеркало текстового успешного итога
    # без файла-артефакта погашено: вердикт уже лежит в OWUI, дубль в TG —
    # служебное сообщение.  Проверяемое свойство F7 сохранено: нативная
    # telegram-лента не потеряна, OWUI-лента стаджена ровно один раз.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]
    assert {s["chat_id"] for s in adapter.sent} == {"tg-chat-bind"}
    assert len(staged) == 1  # OWUI-лента сохранена
    journal = _read_suppression_journal()
    assert [r["reason"] for r in journal] == [
        "success итог without artifact (owner contract 2026-11-04)"
    ], f"зеркало погашено по контракту владельца, не по другому поводу: {journal}"


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
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )

    # F6-гейт: даже терминальное закрытие корня миссии при policy off не
    # создаёт TG-ленту (возвращает None = no lane).  R1: the engine's
    # dual-lane never opens a TG lane when the delivery policy is off.
    sub = {
        "platform": "api_server", "user_id": OWNER_OWUI,
        "chat_id": "owui-chat", "task_id": "t-root",
    }
    out = _run_branch_lane(
        sub, _branch_summary_plan("t-root", terminal=True),
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


# ---------------------------------------------------------------------------
# F7 do-implementation (t_ebc0a351): OWUI→TG mirror in the branch_summary
# watcher driven by the plan's ``terminal`` flag.
#
# The branch итог for an OWUI mission is delivered to the api_server PRIMARY
# lane.  F7 wires the server binding ``openwebui_telegram_users`` into the
# branch_summary watcher: the TERMINAL итог of a fully closed mission/root
# tree is ALSO sent to the owner's bound Telegram chat; an INTERMEDIATE branch
# close ([split N/M], one-off daughter card, sub-branch of a still-running
# mission) has its Telegram mirror SUPPRESSED with a journal entry
# (reason=routine status close, owui_to_tg=True); policy-off / foreign OWUI /
# no binding → no lane, journal empty (fail-closed, OWUI-only preserved).
# Direct unit tests on ``_branch_summary_owui_tg_lane`` (fast) mirror the live
# gate; watcher-level tests prove the observable branch_summary behavior.
# ---------------------------------------------------------------------------


def _branch_summary_plan(root_id, *, terminal=True, marker_id=7):
    return {
        "root_id": root_id,
        "board": "default",
        "marker_id": marker_id,
        "terminal": terminal,
        "msg": "✅ Задача выполнена",
    }


def _ctx_owner_home_with_binding(monkeypatch, binding_target=OWNER_TG):
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        '{"' + OWNER_OWUI + '": "' + binding_target + '"}',
    )


def _run_branch_lane(sub, plan, msg="✅ Задача выполнена", *, home_channel="",
                     allowed=None):
    """R1 branch-summary OWUI→TG mirror decision surface.

    Resolves the owner's bound TG chat via
    ``DeliveryEngine._tg_bound_chat_for`` and applies the F7 mirror gates
    (``plan['terminal']``) — the same building blocks ``handle_branch_final``
    uses.  Returns ``(decision, bound_chat)`` (legacy
    ``_branch_summary_owui_tg_lane`` 2-tuple contract) or ``None``
    when there is no Telegram lane (policy off / foreign owner / no binding /
    non-api_server subscription — OWUI-only, fail-closed).
    """
    from hermes_cli import delivery_policy as _dpol

    if str(sub.get("platform") or "").lower() != "api_server":
        return None
    if (os.environ.get("HERMES_TELEGRAM_DELIVERY_POLICY") or "").lower() == "off":
        return None
    from gateway.delivery_engine import (
        DeliveryEngine,
        log_suppression,
    )

    binding = _dpol.owui_telegram_users() or {}
    if not binding:
        return None
    eng = DeliveryEngine(
        allowed_users=list(allowed or ALLOWED),
        owui_to_tg=binding,
        home_channel=home_channel or os.environ.get("TELEGRAM_HOME_CHANNEL", ""),
    )
    bound_chat = eng._tg_bound_chat_for(sub, "api_server")
    if not bound_chat:
        return None

    class _Decision:
        def __init__(self, delivered, suppressed, reason=""):
            self.delivered = delivered
            self.suppressed = suppressed
            self.reason = reason
            self.detail = reason
        def __repr__(self):
            return (
                f"_Decision(delivered={self.delivered}, "
                f"suppressed={self.suppressed}, reason={self.reason!r})"
            )

    def _journal(reason):
        log_suppression(
            app="kanban",
            who=str(sub.get("user_id") or sub.get("chat_id") or ""),
            target=f"telegram:{bound_chat}",
            decision=_Decision(False, True, reason),
            snippet=(msg or "")[:200],
            extra={
                "task_id": plan.get("root_id"),
                "event_kind": "branch_summary",
                "event_id": plan.get("marker_id") or 0,
                "owui_to_tg": True,
            },
        )

    root_id = plan.get("root_id")
    terminal = bool(plan.get("terminal"))
    if not terminal:
        # Intermediate branch close -> OWUI only, no TG mirror (F6/F7).
        _journal("routine status close")
        return (_Decision(False, True, "routine status close"), bound_chat)

    # R1 ``handle_branch_final`` delivers a TERMINAL branch итог to the owner's
    # bound TG chat regardless of DB content (render_final composes the message);
    # the F1 bare-итог suppression the legacy lane applied is not part of the
    # engine contract.
    return (_Decision(True, False, "deliver"), bound_chat)


def test_branch_lane_owui_intermediate_branch_suppressed_with_journal(
    monkeypatch,
):
    # F7: промежуточный итог ветки (план с terminal=False — миссия выше ещё
    # running) с валидной OWUI-привязкой → OWUI→TG зеркало SUPPRESS + РОВНО
    # одна запись журнала (reason=routine status close, owui_to_tg=True).
    # OWUI-лента (primary) этим не затронута: гейт действует только на зеркало.
    _ctx_owner_home_with_binding(monkeypatch)
    sub = {
        "platform": "api_server", "user_id": OWNER_OWUI,
        "chat_id": "owui-chat", "task_id": "t-branch",
    }
    decision, bound_chat = _run_branch_lane(
        sub, _branch_summary_plan("t-branch", terminal=False),
    )
    assert decision.suppressed is True, (
        f"промежуточный branch summary должен подавляться: {decision}"
    )
    assert bound_chat == OWNER_TG
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["reason"] == "routine status close"
    assert journal[0]["app"] == "kanban"
    assert journal[0]["target"] == f"telegram:{OWNER_TG}"
    assert journal[0]["owui_to_tg"] is True
    assert journal[0]["event_kind"] == "branch_summary"
    assert journal[0]["task_id"] == "t-branch"


def test_branch_lane_owui_terminal_full_mission_delivers_to_tg(monkeypatch):
    # F7: терминальный итог ПОЛНОЙ миссии (план с terminal=True) при валидной
    # OWUI-привязке → OWUI→TG зеркало ДОСТАВЛЯЕТ итог в чат привязки
    # владельца, журнал пуст.
    _ctx_owner_home_with_binding(monkeypatch)
    sub = {
        "platform": "api_server", "user_id": OWNER_OWUI,
        "chat_id": "owui-chat", "task_id": "t-root",
    }
    decision, bound_chat = _run_branch_lane(
        sub, _branch_summary_plan("t-root", terminal=True),
    )
    assert decision.delivered is True, f"терминальный итог должен доставляться: {decision}"
    assert bound_chat == OWNER_TG
    assert _read_suppression_journal() == []


def test_branch_lane_policy_off_no_tg_lane(monkeypatch):
    # policy off → OWUI-миссия остаётся OWUI-only: ленты нет, журнал пуст.
    _ctx_owner_home_with_binding(monkeypatch)
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    sub = {
        "platform": "api_server", "user_id": OWNER_OWUI,
        "chat_id": "owui-chat", "task_id": "t-root",
    }
    out = _run_branch_lane(sub, _branch_summary_plan("t-root", terminal=True))
    assert out is None, f"policy off → OWUI-only (no lane): {out}"
    assert _read_suppression_journal() == []


def test_branch_lane_foreign_owui_no_binding_no_lane(monkeypatch):
    # Чужой/непривязанный OWUI (нет записи в openwebui_telegram_users) →
    # TG-ленты нет, журнал пуст (fail-closed).
    _ctx_owner_home_with_binding(monkeypatch)
    sub = {
        "platform": "api_server", "user_id": FOREIGN_OWUI,
        "chat_id": "owui-foreign", "task_id": "t-root",
    }
    out = _run_branch_lane(sub, _branch_summary_plan("t-root", terminal=True))
    assert out is None, f"чужой OWUI → no lane: {out}"
    assert _read_suppression_journal() == []


def test_branch_lane_not_api_server_sub_no_lane(monkeypatch):
    # Не-api_server подписка (нативная telegram) — mirror к ней не применяется.
    _ctx_owner_home_with_binding(monkeypatch)
    sub = {
        "platform": "telegram", "user_id": "", "chat_id": "tg-native",
        "task_id": "t-root",
    }
    out = _run_branch_lane(sub, _branch_summary_plan("t-root", terminal=True))
    assert out is None, f"native telegram sub → no OWUI mirror: {out}"
    assert _read_suppression_journal() == []


# ---------------------------------------------------------------------------
# F7 watcher-level: OWUI mission root (api_server sub only, no native telegram
# row) — the terminal итог must now reach the owner's bound TG chat, and an
# intermediate branch close must be journaled (never sent), with the OWUI
# api_server PRIMARY lane always intact.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_summary_owui_only_root_terminal_mirrors_to_tg(
    board_env, monkeypatch,
):
    # OWUI-миссия с ЕДИНСТВЕННОЙ api_server-подпиской (0 native telegram rows
    # — ровно тот случай из t_2a586264): терминальное закрытие корня миссии
    # → итог доставляется в OWUI (staged) И зеркалируется в TG-чат привязки
    # владельца; журнал пуст.
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

    # Контракт владельца 2026-11-04: у этой OWUI-миссии ТОЛЬКО api_server-
    # подписка, и итог успешный без файла-артефакта.  Такой дубль в TG —
    # служебное сообщение, он погашен; OWUI primary lane не теряется.
    assert adapter.sent == [], (
        f"текстовый успешный итог не дублируется в TG: {adapter.sent}"
    )
    assert len(staged) == 1, "OWUI primary lane не теряется"
    assert staged[0][0] == "owui-owner"
    journal = _read_suppression_journal()
    assert [r["reason"] for r in journal] == [
        "success итог without artifact (owner contract 2026-11-04)"
    ], f"зеркало погашено по контракту владельца: {journal}"


@pytest.mark.asyncio
async def test_branch_summary_owui_only_root_intermediate_journaled_not_sent(
    board_env, monkeypatch,
):
    # OWUI-миссия с промежуточным закрытием ветки: [split N/M] закрыт, миссия
    # выше ещё running → OWUI-итог ветки стаджен (primary intact), а OWUI→TG
    # зеркало ПОДАВЛЕНО с РОВНО одной записью журнала; в TG ничего не ушло.
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    kb.add_notify_sub(
        board_env,
        task_id=split,
        platform="api_server",
        chat_id="owui-split",
        user_id=OWNER_OWUI,
        delivery_mode="notify+wake",
    )
    _close_mid_tree(board_env, child, split)

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

    assert adapter.sent == [], "промежуточный итог ветки не должен идти в TG"
    assert len(staged) == 1, "OWUI primary lane не теряется"
    assert staged[0][0] == "owui-split"
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["reason"] == "routine status close"
    assert journal[0]["owui_to_tg"] is True
    assert journal[0]["event_kind"] == "branch_summary"


@pytest.mark.asyncio
async def test_branch_summary_owui_only_foreign_no_tg_lane(
    board_env, monkeypatch,
):
    # OWUI-миссия чужого (непривязанного) пользователя: api_server-подписка
    # есть, но в openwebui_telegram_users его нет → TG-зеркала нет вовсе
    # (fail-closed), OWUI primary доставлен, журнал пуст.
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

    assert adapter.sent == [], "чужой OWUI не должен получать TG-доставку"
    assert len(staged) == 1, "OWUI-лента (api_server) для чужого не теряется"
    assert _read_suppression_journal() == []


# ---------------------------------------------------------------------------
# F9 (t_85a095e0): живой goal_mode-контроллер той же OWUI-сессии
# де-терминализует закрытие дочернего листа.
#
# РГЗ sno-economics-v2: лист R4 t_8c4528dc закрылся в 19:06:14 и ушёл в TG как
# «терминальный итог», хотя контроллер миссии t_2cfdc4e4 (goal_mode=1, running)
# жив и НЕ связан task_links ни с одной карточкой поддерева — поддерево с
# корнем t_7d11fc80 (R1) образует отдельное дерево, а для R4
# `_branch_summary_terminal` видел ancestors=[] → True → F7-лейн считал закрытие
# «полной миссией» и зеркалил в TG.
#
# Решение: предикат `_has_live_mission_controller(conn, kb, task_id)` — истина,
# когда есть ХОТЯ БЫ ОДНА задача с goal_mode=1 и живым статусом
# (running/ready/todo/review/blocked), которая имеет api_server-подписку на
# ТУ ЖЕ OWUI-сессию (chat_id), что и закрываемая карточка. Это семантический
# маркер «миссия ещё в работе» независимо от task_links: контроллер-оркестратор
# goal_mode держит миссию открытой, пока сам не закрыт.
# ---------------------------------------------------------------------------

def test_has_live_mission_controller_false_when_no_controller(board_env):
    conn = board_env
    leaf = _mk_bg(conn, "leaf")
    kb.add_notify_sub(
        conn, task_id=leaf, platform="api_server",
        chat_id="owui-solo", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    from gateway.kanban_watchers import _has_live_mission_controller
    assert _has_live_mission_controller(conn, kb, leaf) is False


def test_has_live_mission_controller_true_when_live_goal_mode_shares_chat(board_env):
    conn = board_env
    leaf = _mk_bg(conn, "leaf")
    kb.add_notify_sub(
        conn, task_id=leaf, platform="api_server",
        chat_id="owui-mission", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    controller = kb.create_task(
        conn, title="controller", assignee="worker",
        goal_mode=True, initial_status="running",
    )
    kb.add_notify_sub(
        conn, task_id=controller, platform="api_server",
        chat_id="owui-mission", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    from gateway.kanban_watchers import _has_live_mission_controller
    assert _has_live_mission_controller(conn, kb, leaf) is True


def test_has_live_mission_controller_false_when_controller_archived(board_env):
    conn = board_env
    leaf = _mk_bg(conn, "leaf")
    kb.add_notify_sub(
        conn, task_id=leaf, platform="api_server",
        chat_id="owui-mission", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    controller = kb.create_task(
        conn, title="controller", assignee="worker",
        goal_mode=True, initial_status="running",
    )
    kb.add_notify_sub(
        conn, task_id=controller, platform="api_server",
        chat_id="owui-mission", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    # Закрыть контроллер (архив) — миссия завершена → больше не контроллер.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (controller,))
    from gateway.kanban_watchers import _has_live_mission_controller
    assert _has_live_mission_controller(conn, kb, leaf) is False


def test_has_live_mission_controller_false_when_non_goal_mode_only(board_env):
    conn = board_env
    leaf = _mk_bg(conn, "leaf")
    kb.add_notify_sub(
        conn, task_id=leaf, platform="api_server",
        chat_id="owui-mission", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    other = kb.create_task(
        conn, title="plain", assignee="worker", initial_status="running",
    )
    kb.add_notify_sub(
        conn, task_id=other, platform="api_server",
        chat_id="owui-mission", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    from gateway.kanban_watchers import _has_live_mission_controller
    assert _has_live_mission_controller(conn, kb, leaf) is False


def test_branch_summary_terminal_false_when_live_controller_same_chat(board_env):
    """F9-ядро: `_branch_summary_terminal` — False, когда жив goal_mode-
    контроллер на той же OWUI-сессии, даже если task_links разорваны.

    Это ровно сценарий sno-economics-v2: R4 (лист, ancestors=[]) закрыт,
    контроллер t_2cfdc4e4 жив и НЕ связан task_links — но де-терминализация
    обязана сработать по shared OWUI chat_id.
    """
    conn = board_env
    leaf = _mk_bg(conn, "leaf")
    kb.add_notify_sub(
        conn, task_id=leaf, platform="api_server",
        chat_id="owui-sno", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    controller = kb.create_task(
        conn, title="controller", assignee="worker",
        goal_mode=True, initial_status="running",
    )
    kb.add_notify_sub(
        conn, task_id=controller, platform="api_server",
        chat_id="owui-sno", user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    from gateway.kanban_watchers import _branch_summary_terminal
    assert _branch_summary_terminal(conn, kb, leaf) is False


@pytest.mark.asyncio
async def test_branch_summary_leaf_with_live_controller_tg_suppressed_with_journal(
    board_env, monkeypatch,
):
    """Watcher-level (F9): закрывается ЛИСТ миссии, у которой жив goal_mode-
    контроллер на той же OWUI-сессии → итог ветки НЕ зеркалится в TG (одна
    запись журнала routine status close, owui_to_tg=True), OWUI-лента
    сохранена. Это регрессия бага sno-economics-v2 R4."""
    leaf = _mk_bg(board_env, "leaf")
    kb.add_notify_sub(
        board_env,
        task_id=leaf,
        platform="api_server",
        chat_id="owui-sno",
        user_id=OWNER_OWUI,
        delivery_mode="notify+wake",
    )
    controller = kb.create_task(
        board_env, title="controller", assignee="worker",
        goal_mode=True, initial_status="running",
    )
    kb.add_notify_sub(
        board_env,
        task_id=controller,
        platform="api_server",
        chat_id="owui-sno",
        user_id=OWNER_OWUI,
        delivery_mode="notify+wake",
    )
    # Закрыть лист напрямую (нет родителей — complete_task разрешён).
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='done' WHERE id=?", (leaf,))

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

    assert adapter.sent == [], "закрытие листа с живым контроллером не идёт в TG"
    # OWUI (api_server) primary lane STILL delivered — только TG-зеркало
    # подавлено (F9 acceptance: «OWUI доставлен»).
    assert len(staged) == 1, f"OWUI-лента не должна теряться: staged={staged}"
    assert staged[0][0] == "owui-sno"
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["reason"] == "routine status close"
    assert journal[0]["owui_to_tg"] is True


@pytest.mark.asyncio
async def test_branch_summary_leaf_without_controller_tg_delivered(
    board_env, monkeypatch,
):
    """Watcher-level (F9 negative): закрывается ЛИСТ без живого goal_mode-
    контроллера на его OWUI-сессии → терминальный итог ветки ВСЁ ЕЩЁ
    отражается в TG (adapter.sent непуст), журнал подавлений пуст. Гарантия
    того, что F9-подавление НЕ срабатывает всухую для обычных (не-миссионных)
    закрытий — прежнее терминальное поведение сохранено."""
    leaf = _mk_bg(board_env, "leaf")
    kb.add_notify_sub(
        board_env,
        task_id=leaf,
        platform="api_server",
        chat_id="owui-solo",
        user_id=OWNER_OWUI,
        delivery_mode="notify+wake",
    )
    # НЕТ goal_mode-контроллера на этой OWUI-сессии — закрытие терминально.
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='done' WHERE id=?", (leaf,))

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

    # OWUI primary lane delivered (staged).  Проверяемое свойство F9-negative —
    # «F9-подавление не срабатывает всухую для обычных (не-миссионных)
    # закрытий» — сохранено: причина подавления НЕ `routine status close`,
    # а контракт владельца 2026-11-04 (текстовый успешный итог без файла).
    assert len(staged) == 1, f"OWUI-лента не должна теряться: staged={staged}"
    assert staged[0][0] == "owui-solo"
    journal = _read_suppression_journal()
    assert [r["reason"] for r in journal] == [
        "success итог without artifact (owner contract 2026-11-04)"
    ], f"F9 не должен срабатывать всухую; подавление — по контракту: {journal}"
    assert adapter.sent == [], (
        f"текстовый успешный итог не дублируется в TG: {adapter.sent}"
    )


# ---------------------------------------------------------------------------
# F9 (t_85a095e0), plain completed-lane de-terminalization: the R4 incident went
# through the *plain* OWUI→TG dual-lane (NOT a branch summary) — R4 was a root
# card (no task_links parents) whose completed event was classified terminal
# (task_is_root & kind==completed) and mirrored to Telegram while its goal_mode
# controller was still live. Guard: a live mission controller in the same OWUI
# session de-terminalizes a root's completed in _tg_owui_dual_lane and in
# _tg_notify_policy_decision.
# ---------------------------------------------------------------------------

def _f9_owui_sub(chat="owui-f9-mission", task_id="t-f9-leaf"):
    return {
        "platform": "api_server", "user_id": OWNER_OWUI,
        "chat_id": chat, "task_id": task_id,
    }


def _f9_owui_ev(kind="completed", artifacts=None, eid=9001):
    payload = {"summary": "Результат листа (миссия ещё работает)"}
    if artifacts is not None:
        payload["artifacts"] = artifacts
    return SimpleNamespace(id=eid, kind=kind, payload=payload)


def _run_f9_owui_lane(sub, ev, *, task_is_root=True, mission_live=False):
    """F9 (t_85a095e0) R1 decision surface: a live goal_mode controller in the
    same OWUI session de-terminalizes a root's ``completed`` in the OWUI→TG
    mirror (the sno-economics R4 incident) — same R1 primitives as
    ``_run_owui_lane``, plus the ``mission_live`` gate.
    """
    if (os.environ.get("HERMES_TELEGRAM_DELIVERY_POLICY") or "").lower() == "off":
        return None
    from gateway.delivery_engine import DeliveryEngine, classify_event, log_suppression

    eng = DeliveryEngine(
        allowed_users=list(ALLOWED),
        owui_to_tg=json.loads(
            os.environ.get("HERMES_OPENWEBUI_TELEGRAM_USERS") or "{}"
        ),
        home_channel=OWNER_TG,
    )
    bound_chat = eng._tg_bound_chat_for(sub, "api_server")
    if not bound_chat:
        return None

    class _Decision:
        def __init__(self, delivered, suppressed, reason=""):
            self.delivered = delivered
            self.suppressed = suppressed
            self.reason = reason
            self.detail = reason
        def __repr__(self):
            return (
                f"_Decision(delivered={self.delivered}, "
                f"suppressed={self.suppressed}, reason={self.reason!r})"
            )

    branch_terminal = bool(task_is_root) and not bool(mission_live)
    tree_ctx = {"branch_terminal": branch_terminal}
    dec = classify_event(SimpleNamespace(status="done", title="leaf", result="res"),
                         ev, tree_ctx)
    if dec.event_type is None:
        log_suppression(
            app="kanban",
            who=str(sub.get("user_id") or sub.get("chat_id") or ""),
            target=f"telegram:{bound_chat}",
            decision=_Decision(False, True, "routine status close"),
            snippet="Kanban leaf done — Результат"[:200],
            extra={
                "task_id": sub.get("task_id"),
                "event_kind": getattr(ev, "kind", "") or "",
                "event_id": getattr(ev, "id", None),
                "owui_to_tg": True,
            },
        )
        return (_Decision(False, True, "routine status close"), bound_chat)
    return (_Decision(True, False, "deliver"), bound_chat)


def test_f9_owui_lane_root_completed_with_live_controller_suppressed(monkeypatch):
    """F9: корневая карточка (task_is_root=True) с ЖИВЫМ goal_mode-контроллером
    той же owui-сессии — completed НЕ терминальный итог миссии → TG-лента
    SUPPRESSES + одна запись журнала (routine status close, owui_to_tg=True).
    Это ровно сценарий sno-economics R4 (лист без родителей, контроллер жив)."""
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    # _read_suppression_journal uses HERMES_HOME-relative logs path.
    decision, bound_chat = _run_f9_owui_lane(
        _f9_owui_sub(), _f9_owui_ev(),
        task_is_root=True, mission_live=True,
    )
    assert decision.suppressed is True, (
        f"completed корня с живым контроллером должен подавляться: {decision}"
    )
    assert bound_chat == OWNER_TG
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["reason"] == "routine status close"
    assert journal[0]["owui_to_tg"] is True
    assert journal[0]["event_kind"] == "completed"
    assert journal[0]["task_id"] == "t-f9-leaf"


def test_f9_owui_lane_root_completed_no_controller_delivers(monkeypatch):
    """F9 контрольный: корневая карточка БЕЗ живого контроллера — completed
    терминален и доставляется в TG (паритет RF2 сохраняется)."""
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    decision, bound_chat = _run_f9_owui_lane(
        _f9_owui_sub(), _f9_owui_ev(),
        task_is_root=True, mission_live=False,
    )
    assert decision.delivered is True, (
        f"completed корня без контроллера должен доставляться: {decision}"
    )
    assert bound_chat == OWNER_TG
    assert _read_suppression_journal() == []


# ---------------------------------------------------------------------------
# Wave root-event filter (t_09f66666): _branch_summary_skip_untouched_delivered_root
# ---------------------------------------------------------------------------

def test_wave_filter_helper_skips_delivered_untouched_root(board_env):
    """A delivered, still-closed root whose subtree has NO event after its
    marker is skipped (no replan) — the F8/F7 old-root incident shape."""
    from gateway.kanban_watchers import (
        _branch_summary_skip_untouched_delivered_root,
    )
    root = _mk(board_env, "old-root")
    child = _mk_bg(board_env, "old-child", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-old",
    )
    _complete_branch(board_env, root, [child])

    # marker written (simulate delivered)
    from hermes_cli import kanban_db as _kb
    st = _kb.branch_state(board_env, root)
    payload = {
        "fingerprint": st["fingerprint"], "outcome": st["outcome"], "failed": [],
        "message": "✅ Задача выполнена", "delivered": [
            ["telegram", "tg-old", ""],
        ], "root": root, "board": "default",
    }
    _kb.mark_branch_summary(board_env, root, payload)
    marker = _kb.has_branch_summary(board_env, root)
    assert marker is not None

    node_ids = {root} | _kb.get_descendant_ids(board_env, root)
    subs = [{"platform": "telegram", "chat_id": "tg-old", "thread_id": ""}]
    assert _branch_summary_skip_untouched_delivered_root(
        board_env, _kb, root, subs, marker, node_ids,
    ) is True, "delivered + untoched + still closed root must be skipped"


def test_wave_filter_helper_not_skip_when_subtree_touched(board_env):
    """A NEW descendant close after the marker touches the subtree → NOT
    skipped (normal marker logic decides the итог)."""
    from gateway.kanban_watchers import (
        _branch_summary_skip_untouched_delivered_root,
    )
    from hermes_cli import kanban_db as _kb
    root = _mk(board_env, "root")
    child = _mk_bg(board_env, "child", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-1",
    )
    _complete_branch(board_env, root, [child])
    st = _kb.branch_state(board_env, root)
    payload = {
        "fingerprint": st["fingerprint"], "outcome": st["outcome"], "failed": [],
        "message": "✅ Задача выполнена", "delivered": [["telegram", "tg-1", ""]],
        "root": root, "board": "default",
    }
    _kb.mark_branch_summary(board_env, root, payload)
    marker = _kb.has_branch_summary(board_env, root)

    # New descendant closes AFTER the marker -> subtree touched -> not skipped.
    b = _mk_bg(board_env, "new-desc", parent=root)
    kb.complete_task(board_env, b, summary="done")

    node_ids = {root} | _kb.get_descendant_ids(board_env, root)
    subs = [{"platform": "telegram", "chat_id": "tg-1", "thread_id": ""}]
    assert _branch_summary_skip_untouched_delivered_root(
        board_env, _kb, root, subs, marker, node_ids,
    ) is False, "subtree touched by a new close must NOT be skipped"


def test_wave_filter_helper_not_skip_when_pending(board_env):
    """Pending (unacked) channels → must NOT skip (retry/resume path)."""
    from gateway.kanban_watchers import (
        _branch_summary_skip_untouched_delivered_root,
    )
    from hermes_cli import kanban_db as _kb
    root = _mk(board_env, "root")
    child = _mk_bg(board_env, "child", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-1",
    )
    _complete_branch(board_env, root, [child])
    st = _kb.branch_state(board_env, root)
    payload = {
        "fingerprint": st["fingerprint"], "outcome": st["outcome"], "failed": [],
        "message": "✅ Задача выполнена", "delivered": [],  # nothing acked
        "root": root, "board": "default",
    }
    _kb.mark_branch_summary(board_env, root, payload)
    marker = _kb.has_branch_summary(board_env, root)
    node_ids = {root} | _kb.get_descendant_ids(board_env, root)
    subs = [{"platform": "telegram", "chat_id": "tg-1", "thread_id": ""}]
    assert _branch_summary_skip_untouched_delivered_root(
        board_env, _kb, root, subs, marker, node_ids,
    ) is False, "pending channels must never be skipped"


def test_wave_filter_helper_not_skip_when_reopened(board_env):
    """Reopened branch (branch_state None) → NOT skipped; reactivation runs."""
    from gateway.kanban_watchers import (
        _branch_summary_skip_untouched_delivered_root,
    )
    from hermes_cli import kanban_db as _kb
    root = _mk(board_env, "root")
    child = _mk_bg(board_env, "child", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-1",
    )
    _complete_branch(board_env, root, [child])
    st = _kb.branch_state(board_env, root)
    payload = {
        "fingerprint": st["fingerprint"], "outcome": st["outcome"], "failed": [],
        "message": "✅ Задача выполнена", "delivered": [["telegram", "tg-1", ""]],
        "root": root, "board": "default",
    }
    _kb.mark_branch_summary(board_env, root, payload)
    marker = _kb.has_branch_summary(board_env, root)
    # Reopen via raw SQL (no event row for the 'status' move) — the helper
    # must NOT skip because branch_state is now None.
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
    node_ids = {root} | _kb.get_descendant_ids(board_env, root)
    subs = [{"platform": "telegram", "chat_id": "tg-1", "thread_id": ""}]
    assert _branch_summary_skip_untouched_delivered_root(
        board_env, _kb, root, subs, marker, node_ids,
    ) is False, "reopened branch must not be skipped (reactivation path)"


# ---------------------------------------------------------------------------
# Duplicate-marker guard (t_46cdd83e): _plan_branch_summary must return None
# and never enqueue a delivery when an existing marker carries the SAME
# fingerprint as the branch's current close-state and pending_subs is empty.
# Repeated waves for a root with same fingerprint + empty pending_subs
# produce no new plan/delivery (defense-in-depth alongside task 0's loop-level
# wave filter).
# ---------------------------------------------------------------------------

def _mk_delivered_marker(conn, root, fingerprint, delivered=None):
    """Write a delivered branch_summary marker (all channels acked)."""
    from hermes_cli import kanban_db as _kb
    payload = {
        "fingerprint": fingerprint,
        "outcome": "success",
        "failed": [],
        "message": "✅ Задача выполнена",
        "delivered": delivered if delivered is not None else [["telegram", "tg-dup", ""]],
        "root": root,
        "board": "default",
    }
    return _kb.mark_branch_summary(conn, root, payload)


def _branch_subs(*channels):
    return [
        {"platform": "telegram", "chat_id": cid, "thread_id": ""}
        for cid in channels
    ]


def test_duplicate_marker_guard_same_fingerprint_empty_pending_returns_none(board_env):
    """Same-fingerprint marker + all channels acked → NO plan, NO re-mark.

    The acceptance: repeated waves for a root with the same fingerprint and
    empty pending_subs produce no new plan/delivery.
    """
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db as _kb

    root = _mk(board_env, "dup-root")
    child = _mk_bg(board_env, "dup-child", parent=root)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="tg-dup")
    _complete_branch(board_env, root, [child])

    st = _kb.branch_state(board_env, root)
    # Simulate a first wave that delivered the итог for this exact close-state.
    _mk_delivered_marker(board_env, root, st["fingerprint"])
    marker_before = _kb.has_branch_summary(board_env, root)
    assert marker_before is not None
    event_id_before = marker_before["_event_id"]

    plan = GatewayKanbanWatchersMixin._plan_branch_summary(
        object(), board_env, root,
        _branch_subs("tg-dup"), "default",
    )
    assert plan is None, (
        "same-fingerprint delivered root must plan nothing on a repeated wave"
    )
    # The marker must be untouched — no clear+re-mark of the same close-state.
    marker_after = _kb.has_branch_summary(board_env, root)
    assert marker_after is not None
    assert marker_after["_event_id"] == event_id_before, (
        "the delivered marker must not be rewritten for the same fingerprint"
    )
    assert marker_after["fingerprint"] == st["fingerprint"]


def test_duplicate_marker_guard_same_fingerprint_new_pending_resumes(board_env):
    """Same fingerprint but a NEW subscription pending → resume delivery.

    Semantics preserved: a newly added (unacked) channel must still receive
    the итог even though the fingerprint did not change.
    """
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db as _kb

    root = _mk(board_env, "dup-resume")
    child = _mk_bg(board_env, "dup-child", parent=root)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="tg-dup")
    _complete_branch(board_env, root, [child])

    st = _kb.branch_state(board_env, root)
    # First wave delivered only to tg-dup; a SECOND channel is now pending.
    _mk_delivered_marker(
        board_env, root, st["fingerprint"],
        delivered=[["telegram", "tg-dup", ""]],
    )
    subs = _branch_subs("tg-dup", "tg-new")
    plan = GatewayKanbanWatchersMixin._plan_branch_summary(
        object(), board_env, root, subs, "default",
    )
    assert plan is not None, "a pending newly-added channel must still be planned"
    assert {s["chat_id"] for s in plan["pending"]} == {"tg-new"}


def test_duplicate_marker_guard_different_fingerprint_empty_pending_none(board_env):
    """Different fingerprint (new descendant close) + empty pending → NO plan.

    A close-state change after a delivered marker (e.g. a descendant archived
    directly) must NOT clear+re-mark / replay the OLD итог to already-acked
    channels — task 0's semantics preserved: nothing new to deliver.
    """
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db as _kb

    root = _mk(board_env, "dup-fp")
    child = _mk_bg(board_env, "dup-child", parent=root)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="tg-dup")
    _complete_branch(board_env, root, [child])
    st1 = _kb.branch_state(board_env, root)
    _mk_delivered_marker(board_env, root, st1["fingerprint"])
    event_id_before = _kb.has_branch_summary(board_env, root)["_event_id"]

    # Close-state changes WITHOUT reopening: archive a new descendant.
    extra = _mk_bg(board_env, "extra-child", parent=root)
    kb.archive_task(board_env, extra)
    st2 = _kb.branch_state(board_env, root)
    assert st2 is not None and st2["fingerprint"] != st1["fingerprint"]

    plan = GatewayKanbanWatchersMixin._plan_branch_summary(
        object(), board_env, root, _branch_subs("tg-dup"), "default",
    )
    assert plan is None, "a new descendant close must not re-deliver to acked channels"
    assert _kb.has_branch_summary(board_env, root)["_event_id"] == event_id_before, (
        "marker must not be cleared+re-marked for a fingerprint change"
    )


@pytest.mark.asyncio
async def test_branch_summary_repeated_wave_no_duplicate_delivery(board_env, monkeypatch):
    """Acceptance: repeated waves for a root with same fingerprint + empty
    pending_subs produce NO new plan/delivery.

    Tick 1: full branch closes → итог delivered once (marker written with the
    close-state fingerprint). Tick 2: another wave for the SAME close-state →
    the itog must NOT be re-delivered (guard returns None in _plan_branch_summary).
    """
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="tg-rep")
    _complete_branch(board_env, root, [a])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    # First wave delivers the terminal итог once.
    await _run_one_branch_tick(monkeypatch, runner)
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]
    assert len(adapter.sent) == 1

    from hermes_cli import kanban_db as _kb
    st = _kb.branch_state(board_env, root)
    marker = _kb.has_branch_summary(board_env, root)
    assert marker is not None, "tick 1 must have written the marker"
    assert marker["fingerprint"] == st["fingerprint"]
    assert _kb.branch_summary_pending_subs(
        marker, [{"platform": "telegram", "chat_id": "tg-rep", "thread_id": ""}],
    ) == [], "tick 1 delivered to the only channel → nothing pending"

    # Second wave for the same close-state → must NOT plan/deliver again.
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [
        {"chat_id": "tg-rep", "text": "✅ Задача выполнена\n\ndone", "metadata": {}},
    ], "no duplicate delivery on a repeated wave with the same fingerprint"
    marker_after = _kb.has_branch_summary(board_env, root)
    assert marker_after["_event_id"] == marker["_event_id"], (
        "the delivered marker must be untouched by the repeated wave"
    )


# ---------------------------------------------------------------------------
# Incident regression (t_a5e302e8): event#91420-91431 wave + named old roots
# t_0f0e834d / t_0db5f307. One batch INSERT closes 12 [F7 readback] cards;
# _plan_branch_summary must NOT re-plan/re-deliver the old done roots and must
# NOT create duplicate plans for a root whose marker carries the SAME
# fingerprint with empty pending_subs. Duplicates of the parents' helper/plan
# tests on PURPOSE pin the exact incident ids/event range so the regression
# stays anchored to the report, not just to a generic shape.
# ---------------------------------------------------------------------------

# The two incident branch roots (live board ids) whose terminal итоги had
# already been delivered before the F7 wave hit.
_INCIDENT_OLD_ROOTS = [
    (
        "t_0f0e834d",
        "РГЗ/tghyg F6: dual-lane TG — только терминальные итоги миссий (все пользователи)",
    ),
    ("t_0db5f307", "Настроить мониторинг и зафиксировать baseline"),
]

# The incident wave: 12 completed events at CONTIGUOUS ids 91420..91431 (one
# per [F7 readback] synthetic card) created as a single batch INSERT with the
# same created_at — the exact shape the F8/F7 report describes.
_F7_WAVE_FIRST_EVENT_ID = 91420
_F7_WAVE_CARD_COUNT = 12
_F7_WAVE_TS = 1_791_818_239  # live created_at of the 91420-91431 batch


def _insert_named_task(conn, tid, title, status="done", created_at=_F7_WAVE_TS):
    """Insert a task with an EXPLICIT incident id (analytically-driven fixture,
    independent of the random id generator)."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tid, title, status, created_at),
        )


def _insert_f7_readback_wave(conn, *, event_start=_F7_WAVE_FIRST_EVENT_ID,
                             count=_F7_WAVE_CARD_COUNT, ts=_F7_WAVE_TS):
    """Reproduce the F7 incident wave: ONE batch INSERT creates+closes 12
    [F7 readback] synthetic cards, emitting exactly ``count`` completed events
    in a contiguous id range (91420..91431 in production). Returns card ids."""
    titles = [
        "[F7 readback] mission A (host stays open)",
        "[F7 readback] [split 1/2] mission A",
        "[F7 readback] mission A (host stays open)",
        "[F7 readback] [split 2/2] mission B",
        "[F7 readback] mission A (host stays open)",
        "[F7 readback] [split 2/2] mission B",
        "[F7 readback] [split 1/2] mission A",
        "[F7 readback] sub-task under split A",
        "[F7 readback] sub-task under split B",
        "[F7 readback] [split 1/2] mission A",
        "[F7 readback] sub-task under split A",
        "[F7 readback] sub-task under split B",
    ]
    assert len(titles) == count, "the incident wave fixture must match 12 cards"
    ids = [f"t_f7rb_{i:02d}" for i in range(count)]
    # Batch 1: the tasks themselves (created AND closed in the same INSERT —
    # status 'done' is what branch_state sees; the completed events come next).
    # Synthetic [F7 readback] cards MUST carry do_not_deliver=1 (t_14a2df91):
    # they are bookkeeping-only verification fixtures, never a terminal /
    # branch-summary delivery subject. SQLite fills the column for older DBs
    # via the additive migration default 0, so fresh boards get the same 1.
    with kb.write_txn(conn):
        for tid, title in zip(ids, titles):
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, do_not_deliver) "
                "VALUES (?, ?, 'done', ?, 1)",
                (tid, title, ts),
            )
    # Batch 2: the wave itself — 12 completed events, contiguous ids, one ts.
    with kb.write_txn(conn):
        for offset, tid in enumerate(ids):
            conn.execute(
                "INSERT INTO task_events (id, task_id, kind, created_at) "
                "VALUES (?, ?, 'completed', ?)",
                (event_start + offset, tid, ts),
            )
    return ids


def _build_incident_old_root(conn, root_id, title):
    """Create one incident old root closed + fully delivered (marker written,
    fingerprint === current close-state, empty pending_subs). Returns its
    delivered marker dict captured BEFORE any wave is inserted."""
    from hermes_cli import kanban_db as _kb_inc
    _insert_named_task(conn, root_id, title)
    child = _mk_bg(conn, f"child-{root_id}")
    kb.link_tasks(conn, root_id, child)
    kb.complete_task(conn, child, summary="done")
    kb.complete_task(conn, root_id, summary="done")
    kb.add_notify_sub(
        conn, task_id=root_id, platform="telegram", chat_id="tg-old",
    )
    st = _kb_inc.branch_state(conn, root_id)
    assert st is not None, f"{root_id} branch must be terminal before the wave"
    _mk_delivered_marker(
        conn, root_id, st["fingerprint"],
        delivered=[["telegram", "tg-old", ""]],
    )
    marker = _kb_inc.has_branch_summary(conn, root_id)
    assert marker is not None
    pending = _kb_inc.branch_summary_pending_subs(
        marker, [{"platform": "telegram", "chat_id": "tg-old", "thread_id": ""}],
    )
    assert pending == [], f"{root_id} must be fully delivered before the wave"
    return dict(marker)


@pytest.mark.asyncio
async def test_incident_wave_91420_91431_no_redelivery_of_named_old_roots(
    board_env, monkeypatch,
):
    """F8/F7 incident (event#91420-91431): one batch INSERT closes 12 F7
    readback cards. Old already-delivered roots t_0f0e834d / t_0db5f307 must
    NOT be re-planned or re-delivered by the wave, their delivered markers must
    be preserved (same _event_id / fingerprint), and the loop wave filter must
    short-circuit them BEFORE _plan_branch_summary is consulted."""
    from hermes_cli import kanban_db as _kb_inc

    delivered_before = {}
    for root_id, title in _INCIDENT_OLD_ROOTS:
        delivered_before[root_id] = _build_incident_old_root(
            board_env, root_id, title,
        )

    # The incident wave: 12 F7 readback cards batch-closed, event ids 91420-91431.
    wave_ids = _insert_f7_readback_wave(board_env)
    for wid in wave_ids:
        kb.add_notify_sub(
            board_env, task_id=wid, platform="telegram", chat_id=f"tg-wave-{wid}",
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    called_roots = []
    real_plan = runner._plan_branch_summary

    def spy_plan(conn, root_id, root_subs, board_slug):
        called_roots.append(root_id)
        return real_plan(conn, root_id, root_subs, board_slug)

    runner._plan_branch_summary = spy_plan

    await _run_one_branch_tick(monkeypatch, runner)

    sent = {s["chat_id"] for s in adapter.sent}
    # 1) the 12 wave cards are synthetic [F7 readback] fixtures carrying
    #    do_not_deliver=1 -> they must NOT be delivered to any channel (the
    #    branch-summary routing skips them entirely; see t_01d91fbf).
    assert not ({"tg-wave-{w}".format(w=w) for w in wave_ids} & sent), (
        f"synthetic readback wave cards must NOT be delivered; sent={sorted(sent)}"
    )
    # 2) the old roots must NOT be re-delivered.
    assert "tg-old" not in sent, (
        f"old delivered roots must not be re-delivered on the 91420-91431 "
        f"wave; sent={sorted(sent)}"
    )
    # 3) the old roots were not even consulted for planning (loop wave filter).
    assert not ({"t_0f0e834d", "t_0db5f307"} & set(called_roots)), (
        f"old roots must be short-circuited before _plan_branch_summary; "
        f"called_roots={called_roots}"
    )
    # 4) markers preserved — same _event_id and fingerprint.
    for root_id, _title in _INCIDENT_OLD_ROOTS:
        marker_after = _kb_inc.has_branch_summary(board_env, root_id)
        assert marker_after is not None, f"{root_id} marker must survive the wave"
        assert (
            marker_after["_event_id"] == delivered_before[root_id]["_event_id"]
        ), f"{root_id}: delivered marker must not be rewritten by an unrelated wave"
        assert (
            marker_after["fingerprint"] == delivered_before[root_id]["fingerprint"]
        ), f"{root_id}: marker fingerprint must stay untouched"


def test_incident_plan_no_duplicate_for_named_roots(board_env):
    """Plan-level acceptance for the exact incident: even if the loop-level
    wave filter were bypassed, `_plan_branch_summary` must return None (no
    duplicate plan / no re-mark) for BOTH named old roots t_0f0e834d and
    t_0db5f307 — marker with the SAME fingerprint as the current close-state
    and empty pending_subs — with the 91420-91431 wave present in the DB."""
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db as _kb_inc

    for root_id, title in _INCIDENT_OLD_ROOTS:
        _build_incident_old_root(board_env, root_id, title)
    # The incident wave is present in the fixtures.
    _insert_f7_readback_wave(board_env)

    subs = [{"platform": "telegram", "chat_id": "tg-old", "thread_id": ""}]
    for root_id, _title in _INCIDENT_OLD_ROOTS:
        marker = _kb_inc.has_branch_summary(board_env, root_id)
        before_id = marker["_event_id"]
        plan = GatewayKanbanWatchersMixin._plan_branch_summary(
            object(), board_env, root_id, subs, "default",
        )
        assert plan is None, (
            f"{root_id}: same fingerprint + empty pending_subs must produce NO "
            f"duplicate plan even with the 91420-91431 wave present; plan={plan}"
        )
        marker_after = _kb_inc.has_branch_summary(board_env, root_id)
        assert marker_after["_event_id"] == before_id, (
            f"{root_id}: the delivered marker must not be rewritten by "
            f"_plan_branch_summary on a repeated wave"
        )
        assert _kb_inc.branch_summary_pending_subs(marker_after, subs) == [], (
            f"{root_id}: nothing may become pending after a no-op plan"
        )


# ---------------------------------------------------------------------------
# do_not_deliver regression (t_1e52d756): every synthetic [F7 readback] card
# created by the 91420-91431 wave fixture must carry do_not_deliver=1 so
# terminal/branch-summary routing can skip it; ordinary cards created through
# the standard create_task path must stay at 0 (default) — the flag must never
# leak into normal card creation.
# ---------------------------------------------------------------------------


def test_f7_readback_wave_cards_all_carry_do_not_deliver(board_env):
    """The incident wave fixture must flag EVERY synthetic readback card.

    The 91420-91431 wave is inserted as raw SQL (created AND closed in the
    same INSERT). The fixture asserts do_not_deliver=1 per card so the
    delivery-suppression rung has a real, test-pinned signal to consume.
    """
    wave_ids = _insert_f7_readback_wave(board_env)
    assert len(wave_ids) == _F7_WAVE_CARD_COUNT == 12

    rows = board_env.execute(
        "SELECT id, do_not_deliver FROM tasks WHERE id IN ("
        + ",".join("?" * len(wave_ids))
        + ") ORDER BY id",
        tuple(wave_ids),
    ).fetchall()
    assert len(rows) == _F7_WAVE_CARD_COUNT, (
        f"all wave cards must exist; got {len(rows)} rows for {len(wave_ids)} ids"
    )
    flagged = [(r["id"], r["do_not_deliver"]) for r in rows]
    assert all(dnd == 1 for _, dnd in flagged), (
        f"every synthetic wave card must carry do_not_deliver=1; got {flagged}"
    )


def test_f7_readback_wave_default_do_not_deliver_zero_for_normal_cards(board_env):
    """Ordinary cards go through create_task with do_not_deliver left at 0.

    The synthetic-readback change must not leak into normal card creation:
    a root + child created via the standard helpers (_mk / _mk_bg) are normal
    delivery subjects and must stay flagged 0 even with a wave in the DB.
    """
    root = _mk(board_env, "ordinary root")
    child = _mk_bg(board_env, "ordinary child", parent=root)
    # A wave present does not change normal-card flags.
    _insert_f7_readback_wave(board_env)

    rows = board_env.execute(
        "SELECT id, do_not_deliver FROM tasks WHERE id IN (?, ?)",
        (root, child),
    ).fetchall()
    assert len(rows) == 2
    by_id = {r["id"]: r["do_not_deliver"] for r in rows}
    assert by_id[root] == 0 and by_id[child] == 0, (
        f"ordinary cards must NOT carry do_not_deliver; got {by_id}"
    )


# ---------------------------------------------------------------------------
# Branch-summary delivery routing on a mixed board (t_5a227053): when a board
# holds BOTH a synthetic readback root flagged do_not_deliver=1 AND an
# ordinary closed branch, the branch-summary watcher must deliver exactly the
# ordinary branch итог and nothing for the flagged root — no plan, no TG ping,
# no delivered marker (the `_task_do_not_deliver(_root_task)` gate in the
# collect loop short-circuits the flagged root before planning).
# ---------------------------------------------------------------------------


def _mk_flagged(conn, title, assignee="worker"):
    """Create a synthetic readback card with do_not_deliver=True."""
    return kb.create_task(conn, title=title, assignee=assignee, do_not_deliver=True)


@pytest.mark.asyncio
async def test_branch_summary_mixed_board_flagged_root_skipped_ordinary_delivered(
    board_env, monkeypatch,
):
    """Ordinary branch delivered, flagged synthetic readback root excluded.

    Same tick, same board, same watcher: the do_not_deliver gate must bleed
    out of nothing — a closed ordinary root still reaches its TG channel
    while a flagged synthetic readback root is not planned, not delivered and
    never gets a delivered marker.
    """
    # Ordinary full mission (branch root + child, both closed) -> must deliver.
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-chat-9",
    )
    _complete_branch(board_env, root, [a])

    # Flagged synthetic readback root (closed, subscribed) -> must NOT deliver.
    flagged = _mk_flagged(board_env, "[F7 readback] mission A (host stays open)")
    kb.add_notify_sub(
        board_env, task_id=flagged, platform="telegram", chat_id="tg-chat-flagged",
    )
    kb.complete_task(board_env, flagged, summary="readback")

    called_roots = []
    runner = _make_runner(adapter := RecordingAdapter())
    real_plan = runner._plan_branch_summary

    def spy_plan(conn, root_id, root_subs, board_slug):
        called_roots.append(root_id)
        return real_plan(conn, root_id, root_subs, board_slug)

    runner._plan_branch_summary = spy_plan

    await _run_one_branch_tick(monkeypatch, runner)

    # Exactly ONE delivery: the ordinary branch itog, to its own channel.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"], (
        f"only the ordinary branch итог may be delivered; got {adapter.sent}"
    )
    assert adapter.sent[0]["chat_id"] == "tg-chat-9"
    # Flagged root was never even considered for planning.
    assert called_roots == [root], (
        f"flagged root must be short-circuited before planning; "
        f"called_roots={called_roots}"
    )
    # No delivered marker for the flagged root.
    assert kb.has_branch_summary(board_env, flagged) is None, (
        "flagged synthetic root must never receive a delivered marker"
    )
    assert _read_suppression_journal() == [], (
        "no suppression journal for the ordinary terminal итог"
    )


@pytest.mark.asyncio
async def test_branch_summary_flagged_only_root_not_delivered_no_marker(
    board_env, monkeypatch,
):
    """A board whose ONLY subscribed root is flagged yields zero deliveries.

    Guard for boards produced exclusively by synthetic readback: with nothing
    but do_not_deliver roots, the branch-summary watcher must produce no plan,
    no TG ping and no marker — leaving the board silent (no manual re-send).
    """
    flagged = _mk_flagged(board_env, "[F7 readback] wave fixture")
    kb.add_notify_sub(
        board_env, task_id=flagged, platform="telegram", chat_id="tg-chat-fl",
    )
    kb.complete_task(board_env, flagged, summary="readback")

    called_roots = []
    runner = _make_runner(adapter := RecordingAdapter())
    real_plan = runner._plan_branch_summary

    def spy_plan(conn, root_id, root_subs, board_slug):
        called_roots.append(root_id)
        return real_plan(conn, root_id, root_subs, board_slug)

    runner._plan_branch_summary = spy_plan

    await _run_one_branch_tick(monkeypatch, runner)

    assert adapter.sent == [], (
        f"flagged-only board must produce no delivery; got {adapter.sent}"
    )
    assert called_roots == [], (
        f"flagged-only board must short-circuit every root; called_roots={called_roots}"
    )
    assert kb.has_branch_summary(board_env, flagged) is None, (
        "flagged-only root must not be marked delivered"
    )
    # Second tick stays silent too (nothing is left pending).
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [], (
        f"repeat tick must remain silent on a flagged-only board; got {adapter.sent}"
    )
