"""E2E acceptance: wake only on terminal branch итог + human-action events (t_0a121f42).

Under ``kanban.branch_summary_replace: true`` the wake mechanism must:

  * NEVER wake the agent on an intermediate ``completed`` of a child card,
    on ``[split N/M]`` closings, or on routine ``status`` /
    self-resolving ``blocked`` of a mission tree;
  * ALWAYS wake the agent on the terminal branch итог (``branch_summary``);
  * ALWAYS wake on genuine human-action events — ``blocked`` with
    ``provider_route``, ``block_loop_detected``, and a card routed to
    ``triage`` — even when they occur on a descendant.

The wake is measured as a synthetic agent turn injected into the creator's
session (push adapter ``handle_message``), NOT as the visible channel text
ping (which the existing acceptance suite already covers).

Red→green: these tests fail on 5dfc4857a7d (WHERE ``_wake_kinds`` was an
unfiltered ``ev.kind in _WAKE_KINDS`` gate — an intermediate child completed
woke the creator) and on 8849af16d93 (no branch-summary wake existed), and
pass once the predicate ``should_wake_agent_for_event`` is wired into the
notifier ``_wake_kinds`` computation and the branch-summary watcher wakes on
a terminal итог.
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class WakeRecordingAdapter:
    """Push-capable adapter recording both text pings and wake turns."""

    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_dispatcher_lock_handle = object()  # owns dispatcher -> include unowned subs
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    return runner


async def _run_one_notifier_tick(monkeypatch, runner, interval=1):
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=interval)


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


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace",
        lambda: True,
    )
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, parent=None, assignee="worker", initial_status="running"):
    tid = kb.create_task(
        conn, title=title, assignee=assignee,
        initial_status=initial_status,
    )
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _tg_sub(conn, tid, chat):
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="telegram",
        chat_id=chat,
        user_id="origin-user",
        delivery_mode="notify+wake",
    )


def _complete(conn, tid, *, summary="done", artifacts=None):
    metadata = {"artifacts": artifacts} if artifacts is not None else None
    return kb.complete_task(conn, tid, summary=summary, metadata=metadata)


# ---------------------------------------------------------------------------
# 1. Intermediate child completed does NOT wake.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intermediate_child_completed_does_not_wake(board_env, monkeypatch):
    root = _mk(board_env, "root")
    child = _mk(board_env, "child")
    grand = _mk(board_env, "grand")
    kb.link_tasks(board_env, root, child)
    kb.link_tasks(board_env, child, grand)
    _tg_sub(board_env, root, "chat-root")
    _tg_sub(board_env, child, "chat-child")

    # Child completes while root is still running -> intermediate.
    _complete(board_env, child, summary="child done")

    adapter = WakeRecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    # Under branch_summary_replace the child's intermediate completion must
    # NOT inject a wake turn. (Its text ping is descendant + silent too, but
    # the assertion here is about the WAKE specifically.)
    assert adapter.handled == [], (
        f"intermediate child completed must not wake; got "
        f"{len(adapter.handled)} wakes"
    )


@pytest.mark.asyncio
async def test_split_sub_branch_close_does_not_wake(board_env, monkeypatch):
    root = _mk(board_env, "root")
    sub = _mk(board_env, "split sub")
    leaf = _mk(board_env, "leaf")
    kb.link_tasks(board_env, root, sub)
    kb.link_tasks(board_env, sub, leaf)
    _tg_sub(board_env, root, "chat-root")
    _tg_sub(board_env, sub, "chat-sub")

    # Sub-branch closes (its own leaf done) while root mission still open.
    _complete(board_env, leaf, summary="leaf done")

    adapter = WakeRecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    assert adapter.handled == [], (
        f"[split N/M] sub-branch close must not wake; got "
        f"{len(adapter.handled)} wakes"
    )


@pytest.mark.asyncio
async def test_self_resolving_child_blocked_does_not_wake(board_env, monkeypatch):
    root = _mk(board_env, "root")
    child = _mk(board_env, "child")
    kb.link_tasks(board_env, root, child)
    _tg_sub(board_env, root, "chat-root")
    _tg_sub(board_env, child, "chat-child")

    # dependency/transient blocked: auto-resolving, no human action needed.
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='todo', block_kind=?, "
            "block_recurrences=0 WHERE id=? AND status IN ('running','ready')",
            ("dependency", child),
        )
        kb._append_event(
            board_env, child, "blocked",
            {"reason": "waiting", "kind": "dependency", "recurrences": 0},
        )
    adapter = WakeRecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    assert adapter.handled == [], (
        f"self-resolving child blocked must not wake; got "
        f"{len(adapter.handled)} wakes"
    )


# ---------------------------------------------------------------------------
# 2. Human-action events DO wake (even on descendants).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_blocked_provider_route_wakes(board_env, monkeypatch):
    root = _mk(board_env, "root")
    child = _mk(board_env, "child")
    kb.link_tasks(board_env, root, child)
    _tg_sub(board_env, root, "chat-root")
    _tg_sub(board_env, child, "chat-child")

    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input', "
            "block_recurrences=0 WHERE id=?", (child,),
        )
        kb._append_event(
            board_env, child, "blocked",
            {"reason": "provider down", "kind": "needs_input",
             "provider_route": "openrouter/xyz"},
        )
    adapter = WakeRecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    assert adapter.handled, (
        "child blocked with provider_route must wake the creator; "
        f"got {len(adapter.handled)} wakes"
    )


@pytest.mark.asyncio
async def test_child_block_loop_detected_wakes(board_env, monkeypatch):
    root = _mk(board_env, "root")
    child = _mk(board_env, "child")
    kb.link_tasks(board_env, root, child)
    _tg_sub(board_env, root, "chat-root")
    _tg_sub(board_env, child, "chat-child")

    with kb.write_txn(board_env):
        kb._append_event(board_env, child, "block_loop_detected",
                         {"reason": "loop"})
    adapter = WakeRecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    assert adapter.handled, (
        "child block_loop_detected must wake the creator; "
        f"got {len(adapter.handled)} wakes"
    )


# ---------------------------------------------------------------------------
# 3. Terminal branch итог (branch_summary) wakes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_branch_summary_wakes(board_env, monkeypatch):
    root = _mk(board_env, "root")
    child = _mk(board_env, "child")
    kb.link_tasks(board_env, root, child)
    _tg_sub(board_env, root, "chat-root")
    _tg_sub(board_env, child, "chat-child")

    # Close the whole branch: root first (parent gating), then child.
    _complete(board_env, root, summary="root done")
    _complete(board_env, child, summary="child done")

    adapter = WakeRecordingAdapter()
    runner = _make_runner(adapter)

    # Notifier tick: root's own completed is intermediate under replace mode
    # (branch_summary owns the single wake) -> no wakes yet.
    await _run_one_notifier_tick(monkeypatch, runner)
    assert adapter.handled == [], (
        "root intermediate completed must not wake; "
        f"got {len(adapter.handled)} wakes"
    )
    adapter.sent.clear()

    # Branch watcher: the terminal итог lands and wakes the creator.
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.handled, (
        "terminal branch итог must wake the creator; "
        f"got {len(adapter.handled)} wakes"
    )
    assert len(adapter.sent) >= 1, "terminal итог text ping must be delivered"
