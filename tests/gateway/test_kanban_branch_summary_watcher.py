"""Integration tests for the kanban branch-summary watcher stage.

Covers the "короткий итог ветки" delivery: ONE short message per channel when
the whole branch closes, no duplication across repeated ticks, failure
summaries for needs-a-fix nodes, api_server (OWUI) delivery, reactivation
re-send, and the `kanban.branch_summary_replace` suppression flag.
"""

import asyncio
import os

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import (
    _kanban_branch_summary_replace,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


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


def _complete_branch(conn, root, children):
    for tid in [root] + list(children):
        assert kb.complete_task(conn, tid, summary="done")


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


# ---------------------------------------------------------------------------
# Single summary per channel, no duplicate ticks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_single_delivery_no_dup_telegram(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    b = _mk(board_env, "b")
    kb.link_tasks(board_env, root, a)
    kb.link_tasks(board_env, root, b)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    _complete_branch(board_env, root, [a, b])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена"]
    assert adapter.sent[0]["chat_id"] == "chat-1"
    marker = kb.has_branch_summary(kb.connect(board="default"), root)
    assert marker is not None
    assert len(kb.branch_summary_pending_subs(
        marker, [{"platform": "telegram", "chat_id": "chat-1", "thread_id": ""}])) == 0

    # repeat tick -> no second message
    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_summary_open_branch_no_delivery(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.complete_task(board_env, root, summary="done")  # a stays ready -> open

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []
    assert kb.has_branch_summary(kb.connect(board="default"), root) is None


@pytest.mark.asyncio
async def test_summary_open_no_delivery_after_this_test_spawns_no_marker(board_env, monkeypatch):
    # dependency-blocked descendant keeps the branch open -> no summary
    root = _mk(board_env, "root")
    a = _mk(board_env, "dep_child")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.complete_task(board_env, root, summary="done")
    kb.block_task(board_env, a, kind="dependency", reason="waiting")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []
    assert kb.has_branch_summary(kb.connect(board="default"), root) is None


@pytest.mark.asyncio
async def test_summary_failure_needs_input_telegram(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "child_needs_answer")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.complete_task(board_env, root, summary="done")
    kb.block_task(board_env, a, kind="needs_input", reason="respond please")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"].startswith("❌ Задача не выполнена (нужен фикс):")
    assert a in adapter.sent[0]["text"] or "child_needs_answer" in adapter.sent[0]["text"]


@pytest.mark.asyncio
async def test_summary_reactivation_resends_after_reclose(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    _complete_branch(board_env, root, [a])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена"]

    # reopen a node (simulate done -> ready reactivation)
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='ready' WHERE id=?", (a,))
    # while open: tick clears the marker, no new message
    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []
    conn2 = kb.connect(board="default")
    assert kb.has_branch_summary(conn2, root) is None
    conn2.close()

    # re-close -> fresh summary delivered again
    kb.complete_task(board_env, a, summary="done")
    await _run_one_branch_tick(monkeypatch, runner)
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена"]


@pytest.mark.asyncio
async def test_summary_api_server_delivery_and_ack(board_env, monkeypatch):
    """api_server (OWUI) sub -> staged via the delivery outbox and acked once."""
    from gateway import kanban_origin_delivery as _kod

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc123",
        user_id="sess-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    staged = []
    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    # note: no OWUI registry file needed — stage_terminal is patched.

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    # watcher requires an adapter entry for the platform to consider it live
    runner.adapters = {Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    assert len(staged) == 1
    assert staged[0][0] == "owui-abc123"
    assert staged[0][1] == "✅ Задача выполнена"
    conn2 = kb.connect(board="default")
    marker = kb.has_branch_summary(conn2, root)
    assert kb.branch_summary_pending_subs(
        marker, [{"platform": "api_server", "chat_id": "owui-abc123", "thread_id": ""}]) == []
    conn2.close()

    # repeated tick -> no re-stage
    staged.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert staged == []


# ---------------------------------------------------------------------------
# branch_summary_replace: notifier suppresses descendant pings
# ---------------------------------------------------------------------------

def test_replace_flag_reads_config_default_false(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    assert _kanban_branch_summary_replace() is False


@pytest.mark.asyncio
async def test_replace_flag_suppresses_descendant_notifier_pings(board_env, monkeypatch):
    """With replace enabled a terminal DESCENDANT event is silent in the notifier
    (its ping is claimed+advanced, not rendered) — the whole-branch итог is the
    single channel message, and it arrives only once the branch fully closes."""

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.add_notify_sub(board_env, task_id=a, platform="telegram", chat_id="chat-2")

    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    async def notifier_tick():
        real_sleep = asyncio.sleep
        runner._running = True

        async def fake_sleep(delay):
            if delay == 5:
                return None
            runner._running = False
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await runner._kanban_notifier_watcher(interval=1)

    # Close the root first (parent gating: a descendant only becomes `ready`
    # once its parent is done). Root's OWN completion ping is NOT suppressed
    # — replace mode silences descendants, not the root.
    assert kb.complete_task(board_env, root, summary="done")
    await notifier_tick()
    assert [s["chat_id"] for s in adapter.sent] == ["chat-1"]

    # Now the descendant reaches a terminal notifier event (blocked:
    # needs an answer) — branch closes in failure. Its individual ping must
    # be claimed+silent (the whole-branch итог is the single message).
    adapter.sent.clear()
    assert kb.block_task(board_env, a, reason="need an answer", kind="needs_input")
    assert kb.get_task(board_env, a).status == "blocked"
    await notifier_tick()
    assert adapter.sent == []

    # branch итог: exactly ONE short message per channel, no individual pings
    await _run_one_branch_tick(monkeypatch, runner, interval=1)
    texts = {s["chat_id"]: s["text"] for s in adapter.sent}
    assert set(texts) == {"chat-1", "chat-2"}
    assert texts["chat-1"].startswith("❌ Задача не выполнена (нужен фикс):")
    assert texts["chat-2"].startswith("❌ Задача не выполнена (нужен фикс):")

    # repeated tick -> no duplicate
    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner, interval=1)
    assert adapter.sent == []
