"""Acceptance wiring test: api_server lane emits NO per-child noise.

Covers the wired decision in the kanban notifier loop (t_b9f41135): under
``kanban.branch_summary_replace: true`` the api_server (OWUI) lane must NOT
emit a visible per-event message for every intermediate child closure of a
mission tree — only the terminal branch итог (branch_summary) stays, plus
anything that genuinely needs a human decision (blocked with provider_route,
block_loop_detected / triage).

The pure predicate itself (``should_suppress_api_server_event``) is unit-tested
in test_kanban_api_server_suppress_predicate.py.  This file exercises the
WIRING: the notifier loop routes the api_server-lane decision through the
predicate and the legacy strict-descendant ``silent`` blanket no longer
swallows loud human-action kinds on api_server descendants.
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.API_SERVER: adapter}
    runner._kanban_dispatcher_lock_handle = object()  # owns dispatcher -> include unowned subs
    runner._kanban_sub_fail_counts = {}
    runner._profile_adapters = {}
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay == 5:
            return None  # keep the loop body alive this tick
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.wakes = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "owui.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
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


def _api_sub(conn, tid, chat):
    # Mirror what the auto-subscribe path writes for a server-owned OWUI chat.
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="api_server",
        chat_id=chat,
        user_id="sess-owui-1",
        delivery_mode="notify+wake",
    )


def _staged_sink(monkeypatch, staged):
    from gateway import kanban_origin_delivery as _kod
    import os
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    return staged


# ---------------------------------------------------------------------------
# Acceptance core: N-child mission on api_server lane — no per-child noise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_server_mission_suppresses_per_child_routine_pings(
    board_env, monkeypatch,
):
    """Routine per-child completed/status closures on api_server are claimed +
    silent; only the branch итог (branch_summary) surfaces.  The root's own
    routine completion ping is also NOT a visible message (replace mode: the
    branch_summary stage owns the single channel message)."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(board_env, "mission")
    children = [_mk(board_env, f"child-{i}", parent=root) for i in range(3)]
    # Root + every child carry an api_server OWUI subscription (auto-subscribe).
    _api_sub(board_env, root, "owui-root")
    for i, cid in enumerate(children):
        _api_sub(board_env, cid, f"owui-child-{i}")

    # Close the root first (parent satisfaction), then the children — routine
    # terminal closures for every card in the mission tree.
    assert kb.complete_task(board_env, root, summary="mission done")
    for cid in children:
        assert kb.complete_task(board_env, cid, summary="child done")

    staged = []
    _staged_sink(monkeypatch, staged)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    # No per-child completion text ping reaches the OWUI lane (routine closes
    # are intermediate noise; the root's completion is also not the итог).
    assert adapter.sent == [], f"api_server must not send per-child pings: {adapter.sent}"
    # branch_summary is NOT delivered through this notifier loop (it is a
    # separate stage); the assertion here is that NOTHING routine was staged.
    assert staged == [], f"routine closures must not stage OWUI messages: {staged}"


@pytest.mark.asyncio
async def test_api_server_child_blocked_needs_human_fix_stays_loud(
    board_env, monkeypatch,
):
    """A child blocked needing a human fix (needs_input) MUST stay visible even
    on a strict descendant of a subscribed root — the legacy ``silent`` blanket
    must not swallow it (t_b9f41135 acceptance: 'блокировки при наличии')."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(board_env, "mission")
    child = _mk(board_env, "child-needs-input", parent=root)
    _api_sub(board_env, root, "owui-root")
    _api_sub(board_env, child, "owui-child-needs-input")

    # Root must be terminal before the child is blockable (parent gating).
    assert kb.complete_task(board_env, root, summary="mission done")
    # Child needs a human decision -> blocked with block_kind=needs_input
    # (a genuine human-action block; the predicate keeps it loud).
    assert kb.block_task(
        board_env, child,
        reason="need an explicit answer before release",
        kind="needs_input",
    )

    staged = []
    _staged_sink(monkeypatch, staged)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    # The child's human-action block must reach the OWUI session.
    assert len(staged) == 1, f"needs_input block must stay loud: {staged}"
    assert staged[0][0] == "owui-child-needs-input"
