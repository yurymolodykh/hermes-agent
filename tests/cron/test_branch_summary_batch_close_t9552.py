"""Regression (t_9552f6d9): a batch close of N branch-root cards must NOT
re-plan / re-deliver an OLD already-delivered root that shares the SAME
subscription channel.

Incident (F8, РГЗ/tghyg): a wave of completed events made the kanban
branch-summary watcher re-plan branch итог for ALL closed roots of a
subscription channel, not just the roots completed by that wave — old done /
synthetic cards were re-sent to Telegram as a duplicate "OWUI→TG mirror
terminal итог".

This suite pins the shared-channel shape:
  1. one subscription channel (tg-same) carries an OLD already-delivered root;
  2. a single completed wave batch-closes N NEW root cards on the SAME channel;
  3. in that wave only the N NEW roots produce a terminal delivery; the OLD
     delivered root must NOT be re-planned nor re-delivered, and its delivered
     marker must survive untouched (same _event_id / fingerprint).

Also pinned (task item 5): a root whose marker carries the SAME fingerprint as
the current close-state and whose pending_subs is EMPTY — with the wave present
in the DB — must produce NO plan (_plan_branch_summary returns None) and must
not rewrite the marker.

Self-contained: does not import the shared test_branch_summary_t2 module so the
concurrently-committed F8/F9 test suite stays untouched.
"""

import asyncio

import pytest

from gateway.config import Platform
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


def _mk_bg(conn, title, parent=None, status="running", assignee="worker"):
    tid = kb.create_task(conn, title=title, assignee=assignee, initial_status=status)
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _complete_branch(conn, root, children):
    for tid in [root] + list(children):
        assert kb.complete_task(conn, tid, summary="done")


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
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


def _branch_subs(*channels):
    return [
        {"platform": "telegram", "chat_id": cid, "thread_id": ""}
        for cid in channels
    ]


def _mk_delivered_marker(conn, root, fingerprint, delivered=None):
    payload = {
        "fingerprint": fingerprint,
        "outcome": "success",
        "failed": [],
        "message": "✅ Задача выполнена",
        "delivered": delivered if delivered is not None else [["telegram", "tg-same", ""]],
        "root": root,
        "board": "default",
    }
    return kb.mark_branch_summary(conn, root, payload)


_SHARED = "tg-same"
_TS = 1_791_818_239


def _insert_closed_root(conn, rid, title, event_id):
    """Create a standalone (tree-less) branch-root card already in done state,
    with its own completed event and a subscription to the shared channel."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (rid, title, "done", _TS),
        )
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at) "
            "VALUES (?, ?, 'completed', ?)",
            (event_id, rid, _TS),
        )
    kb.add_notify_sub(conn, task_id=rid, platform="telegram", chat_id=_SHARED)


@pytest.mark.asyncio
async def test_shared_channel_batch_close_n_roots_does_not_redeliver_old_root(
    board_env, monkeypatch,
):
    """One completed wave batch-closes N new roots on the SAME channel as an
    already-delivered old root. Only the N new roots deliver; the old root is
    not re-planned / re-delivered and its marker is preserved."""
    from hermes_cli import kanban_db as _kbi

    # --- 1) old root closed + delivered to the shared channel ---
    old = _mk(board_env, "old-shared-root")
    old_ch = _mk_bg(board_env, "old-shared-child", parent=old)
    kb.add_notify_sub(board_env, task_id=old, platform="telegram", chat_id=_SHARED)
    _complete_branch(board_env, old, [old_ch])
    st_old = _kbi.branch_state(board_env, old)
    assert st_old is not None
    _mk_delivered_marker(board_env, old, st_old["fingerprint"])
    marker_old_before = _kbi.has_branch_summary(board_env, old)
    assert marker_old_before is not None

    # --- 2) batch-close N NEW roots on the same shared channel ---
    N = 5
    for i in range(N):
        _insert_closed_root(board_env, f"t_shared_new_{i:02d}", f"new root {i}", 91420 + i)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    called_roots = []
    real_plan = runner._plan_branch_summary

    def spy_plan(conn, root_id, root_subs, board_slug):
        called_roots.append(root_id)
        return real_plan(conn, root_id, root_subs, board_slug)

    runner._plan_branch_summary = spy_plan

    await _run_one_branch_tick(monkeypatch, runner)

    sent = [s["chat_id"] for s in adapter.sent]
    # 1) the N new roots are delivered to the shared channel.
    assert len(adapter.sent) == N, (
        f"exactly N={N} new-root deliveries expected to {_SHARED}, "
        f"got {len(adapter.sent)}: {sent}"
    )
    # 2) the OLD root must not be re-planned after the wave. With the shared
    #    channel a re-delivery would be indistinguishable by chat_id, so the
    #    sound assertion is that it is never consulted for planning.
    assert called_roots.count(old) == 0, (
        f"old delivered root must NOT be consulted for planning after the wave; "
        f"called_roots={called_roots}"
    )
    # 3) the old root's marker is preserved (same _event_id + fingerprint).
    marker_old_after = _kbi.has_branch_summary(board_env, old)
    assert marker_old_after is not None
    assert marker_old_after["_event_id"] == marker_old_before["_event_id"], (
        "delivered old-root marker must not be rewritten by the shared-wave"
    )
    assert marker_old_after["fingerprint"] == st_old["fingerprint"]


def test_plan_none_when_same_fingerprint_empty_pending_with_wave(board_env):
    """A tree-less closed root with a delivered marker whose fingerprint equals
    the current close-state and whose pending_subs is empty — with N new roots
    batch-closed in the DB — must produce NO plan (None) and must not rewrite
    the marker."""
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db as _kbi

    old = _mk(board_env, "old-tree-less-root")
    child = _mk_bg(board_env, "older-child", parent=old)
    kb.add_notify_sub(board_env, task_id=old, platform="telegram", chat_id=_SHARED)
    _complete_branch(board_env, old, [child])
    st_old = _kbi.branch_state(board_env, old)
    _mk_delivered_marker(board_env, old, st_old["fingerprint"])
    marker_before = _kbi.has_branch_summary(board_env, old)
    event_id_before = marker_before["_event_id"]

    # The wave of N new roots exists in the DB (same shared channel).
    for i in range(5):
        _insert_closed_root(board_env, f"t_wn_{i:02d}", f"wave new {i}", 91440 + i)

    plan = GatewayKanbanWatchersMixin._plan_branch_summary(
        object(), board_env, old, _branch_subs(_SHARED), "default",
    )
    assert plan is None, (
        f"same fingerprint + empty pending_subs must produce NO plan even with "
        f"the shared-channel wave present; plan={plan}"
    )
    marker_after = _kbi.has_branch_summary(board_env, old)
    assert marker_after["_event_id"] == event_id_before, (
        "the delivered marker must not be rewritten on a no-op plan"
    )
    assert marker_after["fingerprint"] == st_old["fingerprint"]
