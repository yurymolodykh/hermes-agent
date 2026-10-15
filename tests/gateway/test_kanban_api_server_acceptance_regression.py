"""Acceptance regression suite: api_server lane — NO per-child noise (t_60aa4496).

Builds on the wiring tests in ``test_kanban_api_server_lane_wiring.py``
(t_b9f41135) and the pure-predicate unit tests in
``test_kanban_api_server_suppress_predicate.py`` (t_ec550192) with an
end-to-end acceptance matrix for the READ-PRODUCER contract:

For a mission of N cards in the ``api_server`` (OWUI) lane under
``kanban.branch_summary_replace: true``:

  * NO per-child routine ``completed`` / ``status`` / ``blocked`` message is
    emitted — the user's channel must not be flooded by intermediate closures
    of a mission tree;
  * the user's REQUEST (the originating OWUI session subscription) survives —
    the exact-owner OWUI chat stays subscribed and the terminal итог reaches
    it, never dropped;
  * the TERMINAL branch итог with report ARTIFACTS survives — the root report
    file rides inline in the branch_summary message (``_owui_branch_result_text``),
    and is NOT lost just because the individual pings were suppressed;
  * genuine human-attention blocks stay visible at the WIRING level, not only
    in the pure-predicate unit tests:
      - ``blocked`` with ``provider_route`` (provider outage → operator must
        pick a route) stays staged/loud;
      - ``block_loop_detected`` (task routed to TRIAGE) stays loud;
      - a card routed to ``triage`` stays visible;
  * ``[split N/M]`` / non-terminal closures are suppressed — a mid-tree close
    (one sub-branch of a still-open mission) is NOT a terminal итог and must
    not emit a per-child message.

Red→green proof (acceptance): the loud-on-descendant cases (provider_route,
block_loop_detected, triage) FAIL on the pre-wiring parent commit
4094e06153b — there the legacy ``silent`` blanket (``d.get("silent") and kind
in TERMINAL_KINDS``) swallowed every TERMINAL_KIND on an api_server descendant
BEFORE the shim/predicate could keep loud kinds alive — and PASS on the wired
commit 5dfc4857a7d, where the canonical
``should_suppress_api_server_event(ev, card=task, ...)`` predicate is the sole
suppression authority for the api_server lane. Measured for this file on the
parent branch (4094e06153b): 3 failed, 4 passed — exactly the three
'stays-loud' cases are red; on the wired commit: 7 passed.
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
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner


async def _run_one_notifier_tick(monkeypatch, runner, interval=1):
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None  # startup + cadence; keep this tick alive
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=interval)


async def _run_one_branch_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the branch-summary watcher."""
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None  # startup + cadence; keep running this tick
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.wakes = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "accept.db"))
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
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    return staged


def _complete(conn, tid, *, summary="done", artifacts=None):
    metadata = {"artifacts": artifacts} if artifacts is not None else None
    return kb.complete_task(conn, tid, summary=summary, metadata=metadata)


def _emit_self_resolving_blocked(conn, tid, kind="dependency", reason="waiting"):
    """Route a card to ``todo`` with a routine ``blocked`` event, mirroring the
    auto-resolving dependency/transient path (not a human-action block)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='todo', block_kind=?, block_recurrences=0 "
            "WHERE id=? AND status IN ('running','ready')",
            (kind, tid),
        )
        kb._append_event(
            conn, tid, "blocked",
            {"reason": reason, "kind": kind, "recurrences": 0},
        )


# ---------------------------------------------------------------------------
# 1. N-child mission: NO per-child routine completed/status/blocked.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_n_child_mission_no_per_child_routine_pings(board_env, monkeypatch):
    """N=3 mission under replace mode: per-child routine ``completed``,
    ``status`` and self-resolving ``blocked`` (dependency) must produce ZERO
    visible api_server messages — the branch итог is the single channel
    message (acceptance: 'для миссии из N карточек не появляется per-child
    completed/status/blocked')."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(board_env, "mission-N3")
    children = [_mk(board_env, f"child-{i}", parent=root) for i in range(3)]
    _api_sub(board_env, root, "owui-request")
    for i, cid in enumerate(children):
        _api_sub(board_env, cid, f"owui-child-{i}")

    # Root terminal FIRST (parent-gating), then children.
    assert _complete(board_env, root, summary="mission done")
    for cid in children[:2]:
        assert _complete(board_env, cid, summary="child done")
    # Third child: routine status event + self-resolving dependency block
    # (both intermediate) then a plain completion.
    with kb.write_txn(board_env):
        kb._append_event(board_env, children[2], "status", {"status": "ready"})
    _emit_self_resolving_blocked(board_env, children[2])
    # Child now sits in todo; force it back to ready-completable state.
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (children[2],),
        )
    assert _complete(board_env, children[2], summary="child done")

    staged = []
    _staged_sink(monkeypatch, staged)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    # No per-child ping of ANY routine kind reaches the OWUI lane.
    assert adapter.sent == [], f"must not send per-child pings: {adapter.sent}"
    assert staged == [], f"must not stage per-child pings: {staged}"


# ---------------------------------------------------------------------------
# 2. Terminal итог with ARTIFACTS survives the branch_summary path.
# ---------------------------------------------------------------------------


def test_branch_summary_itog_keeps_root_report_artifact(board_env):
    """The api_server branch итог appends the root ``completed`` event's report
    artifact inline (``_owui_branch_result_text``) — the artifact survives even
    though the individual per-child pings were suppressed (acceptance:
    'остаются … терминальный итог с артефактами')."""
    from gateway.kanban_watchers import _owui_branch_result_text

    plan = {"root_id": "t_root", "board": "default"}
    msg = "✅ Задача выполнена"

    # No artifact -> unchanged short итог.
    assert _owui_branch_result_text(plan, msg) == msg

    # With a readable .md report artifact -> appended inline.
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "itog.md")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("Итоговая сводка по миссии.\n")
        root = _mk(board_env, "mission")
        with kb.write_txn(board_env):
            kb._append_event(
                board_env, root, "completed",
                {"summary": "done", "artifacts": [report]},
            )
        plan2 = {"root_id": root, "board": "default"}
        out = _owui_branch_result_text(plan2, msg)
        assert "itog.md" in out, f"artifact filename missing: {out}"
        assert "Итоговая сводка" in out


# ---------------------------------------------------------------------------
# 3. blocked WITH provider_route stays loud (wiring level).
#    — RED on 4094 (pre-wiring silent blanket swallows it), GREEN on 5dfc.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_blocked_provider_route_stays_loud(board_env, monkeypatch):
    """A strict descendant blocked by a provider outage (payload
    ``provider_route``) MUST reach the OWUI session even under replace mode —
    the legacy ``silent`` blanket must not swallow it (acceptance: 'blocked с
    provider_route … остаются видимыми')."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(board_env, "mission")
    child = _mk(board_env, "child-provider", parent=root)
    _api_sub(board_env, root, "owui-root")
    _api_sub(board_env, child, "owui-child-provider")

    assert _complete(board_env, root, summary="mission done")
    # Emulate the provider-outage blocker: blocked + needs_input +
    # provider_route in the payload (same shape as kanban_db's 4th-outage path).
    with kb.write_txn(board_env):
        payload = {
            "reason": "Провайдер/модель letovo/deepseek-v4-flash недоступен; выберите маршрут",
            "kind": "needs_input",
            "provider_route": "letovo/deepseek-v4-flash",
        }
        board_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=? AND status IN ('running','ready')",
            (child,),
        )
        kb._append_event(board_env, child, "blocked", payload)

    staged = []
    _staged_sink(monkeypatch, staged)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    assert len(staged) == 1, f"provider_route block must stage exactly once: {staged}"
    assert staged[0][0] == "owui-child-provider"
    assert "blocked" in staged[0][1]


# ---------------------------------------------------------------------------
# 4. block_loop_detected / triage stays loud (wiring level).
#    — RED on 4094 (pre-wiring silent blanket swallows it), GREEN on 5dfc.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_block_loop_detected_stays_loud(board_env, monkeypatch):
    """A descendant routed to TRIAGE via ``block_loop_detected`` (re-blocked
    for the same cause past the recurrence limit) MUST stay visible — it is
    the one transition that exists to force human attention (acceptance:
    'block_loop_detected/triage остаются видимыми')."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(board_env, "mission")
    child = _mk(board_env, "child-loop", parent=root)
    _api_sub(board_env, root, "owui-root")
    _api_sub(board_env, child, "owui-child-loop")

    assert _complete(board_env, root, summary="mission done")
    # Emulate the recurrence-limit path: route the child to triage and emit
    # the block_loop_detected event (same shape as block_task when
    # recurrences >= BLOCK_RECURRENCE_LIMIT).
    with kb.write_txn(board_env):
        payload = {
            "reason": "re-blocked for the same cause",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        }
        board_env.execute(
            "UPDATE tasks SET status='triage', block_kind='needs_input', "
            "block_recurrences=2 WHERE id=? AND status IN ('running','ready')",
            (child,),
        )
        kb._append_event(board_env, child, "block_loop_detected", payload)

    staged = []
    _staged_sink(monkeypatch, staged)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    assert len(staged) == 1, f"block_loop_detected must stage: {staged}"
    assert staged[0][0] == "owui-child-loop"
    assert "TRIAGE" in staged[0][1].upper()


@pytest.mark.asyncio
async def test_child_triage_card_route_stays_loud(board_env, monkeypatch):
    """A card routed to ``triage`` (status) stays visible even when only a
    routine ``completed`` event remains on it — the predicate reads the card
    status and never drops a triage card (acceptance: 'triage остаются
    видимыми')."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(board_env, "mission")
    child = _mk(board_env, "child-triage", parent=root)
    _api_sub(board_env, root, "owui-root")
    _api_sub(board_env, child, "owui-child-triage")

    assert _complete(board_env, root, summary="mission done")
    with kb.write_txn(board_env):
        # Card routed to triage but the LAST claimed event is a routine
        # completed — the predicate must still keep it loud via card.status.
        board_env.execute(
            "UPDATE tasks SET status='triage' WHERE id=?", (child,),
        )
        kb._append_event(board_env, child, "completed", {"summary": "done"})

    staged = []
    _staged_sink(monkeypatch, staged)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    assert len(staged) == 1, f"triage card must stay loud: {staged}"
    assert staged[0][0] == "owui-child-triage"


# ---------------------------------------------------------------------------
# 5. [split N/M] mid-tree close is suppressed (not a terminal итог).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_sub_branch_close_suppressed(board_env, monkeypatch):
    """A ``[split N/M]`` style mid-tree close — one sub-branch (a child of a
    still-OPEN mission root) closes while the mission continues — must NOT
    emit a visible per-child message (acceptance: 'split N/M … подавляются')."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    mission = _mk(board_env, "mission")
    # The [split N/M] card is itself a child chain under the open mission.
    split = _mk(board_env, "[split 1/2] sub-branch", parent=mission)
    sub = _mk(board_env, "sub-task", parent=split)
    _api_sub(board_env, mission, "owui-mission")
    _api_sub(board_env, split, "owui-split")
    _api_sub(board_env, sub, "owui-sub")

    # Mission is still RUNNING (not terminal). The [split N/M] sub-branch
    # closes (its child + the split card both hit terminal). Parent-gating
    # would require the whole mission to close first in a real transition, so
    # here we inject the closure events directly — the shape the notifier must
    # treat as intermediate: a strict-descendant mid-tree close while the
    # mission root remains open.
    with kb.write_txn(board_env):
        # sub (grandchild) closure is intermediate on the mission tree.
        board_env.execute(
            "UPDATE tasks SET status='done' WHERE id=?", (sub,),
        )
        kb._append_event(board_env, sub, "completed", {"summary": "sub done"})
        # split card closure is a [split N/M] descendant close.
        board_env.execute(
            "UPDATE tasks SET status='done' WHERE id=?", (split,),
        )
        kb._append_event(board_env, split, "completed", {"summary": "split done"})

    staged = []
    _staged_sink(monkeypatch, staged)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_notifier_tick(monkeypatch, runner)

    # Mission root still open -> NO terminal итог, NO per-child noise.
    assert adapter.sent == [], f"split closure must be silent: {adapter.sent}"
    assert staged == [], f"split closure must not stage: {staged}"


# ---------------------------------------------------------------------------
# 6. The user's REQUEST survives: terminal итог reaches the OWUI session.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_itog_reaches_request_session(board_env, monkeypatch):
    """When the whole mission closes, the terminal branch итог IS delivered to
    the exact OWUI request session (previously-suppressed pings collapse into
    ONE итог with artifacts) — the user's request is not lost (acceptance:
    'остаются запрос, терминальный итог')."""
    import os
    import tempfile
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "mission-report.md")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("Миссия завершена: все 3 карточки закрыты.\n")

        root = _mk(board_env, "mission")
        children = [_mk(board_env, f"child-{i}", parent=root) for i in range(2)]
        _api_sub(board_env, root, "owui-request")
        for i, cid in enumerate(children):
            _api_sub(board_env, cid, f"owui-child-{i}")

        assert _complete(board_env, root, summary="mission done",
                         artifacts=[report])
        for cid in children:
            assert _complete(board_env, cid, summary="child done")

        # Notifier tick: per-child pings suppressed.
        staged = []
        _staged_sink(monkeypatch, staged)
        adapter = RecordingAdapter()
        runner = _make_runner(adapter)
        await _run_one_notifier_tick(monkeypatch, runner)
        assert adapter.sent == []
        assert staged == []

        # Branch-summary tick: ONE terminal итог WITH the root artifact.
        from gateway import kanban_origin_delivery as _kod
        branch_staged = []

        def fake_stage(outbox, registry, sub, profile, board, event_id, message):
            branch_staged.append((sub.get("chat_id"), message, event_id))
            return ("acknowledged", 1)

        monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
        await _run_one_branch_tick(monkeypatch, runner, interval=1)

        # Every subscribed channel of the closed tree receives exactly ONE
        # terminal итог (replace mode: the single channel message per channel),
        # and the originating request session is among them, with the artifact
        # riding inline.
        assert branch_staged, f"no итог staged: {branch_staged}"
        by_chat = {}
        for chat_id, message, _eid in branch_staged:
            assert chat_id not in by_chat, f"duplicate итог to {chat_id}"
            by_chat[chat_id] = message
        assert set(by_chat) == {"owui-request", "owui-child-0", "owui-child-1"}, \
            f"unexpected итог receivers: {sorted(by_chat)}"
        itog = by_chat["owui-request"]
        assert "Задача выполнена" in itog
        assert "mission-report.md" in itog
        assert "Миссия завершена" in itog
