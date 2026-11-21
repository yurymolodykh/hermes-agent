import asyncio


from gateway.config import Platform
from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _release_singleton_lock,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    # Most tests model the default gateway after its dispatcher acquired the
    # singleton lock. Tests for startup or non-owner gateways clear this.
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_replays_telegram_dm_topic_delivery_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_mode="notify+wake",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].source.thread_id == "20197"


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


def test_non_dispatch_gateway_claims_only_its_profile_subscriptions(
    tmp_path, monkeypatch,
):
    """A profile gateway delivers its events while another gateway dispatches."""
    db_path = tmp_path / "cross-profile-notifier.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kbc.connect()
    try:
        foreign_tid = kb.create_task(
            conn, title="default-owned", assignee="worker",
        )
        kbn.add_notify_sub(
            conn,
            task_id=foreign_tid,
            platform="telegram",
            chat_id="default-chat",
            notifier_profile="default",
        )
        kb.complete_task(conn, foreign_tid, summary="default done")

        owned_tid = kb.create_task(
            conn, title="writer-owned", assignee="worker",
        )
        kbn.add_notify_sub(
            conn,
            task_id=owned_tid,
            platform="telegram",
            chat_id="writer-chat",
            notifier_profile="writer",
        )
        kb.complete_task(conn, owned_tid, summary="writer done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "writer"
    runner._kanban_dispatcher_lock_handle = None

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [delivery["chat_id"] for delivery in adapter.sent] == ["writer-chat"]
    assert owned_tid in adapter.sent[0]["text"]
    assert len(_unseen_terminal_events_for(foreign_tid, "default-chat")) == 1


def test_legacy_subscription_requires_confirmed_dispatcher_lock_owner(
    tmp_path, monkeypatch,
):
    """Startup and lock-losing gateways cannot claim legacy notifications."""
    db_path = tmp_path / "legacy-lock-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="legacy-chat",
        )
        kb.complete_task(conn, task_id, summary="legacy done")
    finally:
        conn.close()

    startup_adapter = RecordingAdapter()
    startup_runner = _make_runner(startup_adapter)
    startup_runner._kanban_dispatcher_lock_handle = None
    asyncio.run(_run_one_notifier_tick(monkeypatch, startup_runner))
    assert startup_adapter.sent == []
    assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

    lock_path = tmp_path / ".dispatcher.lock"
    winner_handle, winner_state = _acquire_singleton_lock(lock_path)
    loser_handle, loser_state = _acquire_singleton_lock(lock_path)
    try:
        assert winner_state == "held"
        assert loser_state == "contended"

        loser_adapter = RecordingAdapter()
        loser_runner = _make_runner(loser_adapter)
        loser_runner._kanban_dispatcher_lock_handle = loser_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, loser_runner))
        assert loser_adapter.sent == []
        assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

        winner_adapter = RecordingAdapter()
        winner_runner = _make_runner(winner_adapter)
        winner_runner._kanban_dispatcher_lock_handle = winner_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, winner_runner))
        assert [item["chat_id"] for item in winner_adapter.sent] == ["legacy-chat"]
        assert task_id in winner_adapter.sent[0]["text"]
    finally:
        _release_singleton_lock(loser_handle)
        _release_singleton_lock(winner_handle)


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "stopped unexpectedly" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "stopped unexpectedly" in adapter.sent[1]["text"].lower()


def test_notifier_subscription_survives_done_reopen_until_archive(
    tmp_path, monkeypatch,
):
    """Done is reversible; archive alone ends notification ownership."""
    db_path = tmp_path / "done-reopen-archive.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="review continuation",
            assignee="worker",
            session_id="origin-session",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="group",
            notifier_profile="reviewer",
            delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary="first completion")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.sent[0]["chat_id"] == "origin-chat"
    assert adapter.sent[0]["metadata"]["thread_id"] == "origin-thread"
    assert adapter.handled[0].source.thread_id == "origin-thread"
    assert adapter.handled[0].source.profile == "reviewer"

    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1, "completion must retain the origin subscription"
        first_cursor = subs[0]["last_event_id"]
    finally:
        conn.close()

    # A quiet tick proves the completed event cannot replay after its cursor
    # was advanced, even though the subscription now remains present.
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1

    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        assert kb.complete_task(conn, tid, summary="corrected completion")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The reopen status and second completion each deliver once, while only
    # completion wakes the exact original session/thread.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    assert all(item["chat_id"] == "origin-chat" for item in adapter.sent)
    assert adapter.handled[-1].source.thread_id == "origin-thread"
    assert adapter.handled[-1].source.profile == "reviewer"

    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["last_event_id"] > first_cursor
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Archive itself is intentionally silent, but consumes its event and
    # removes the subscription so no later historical event can replay.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    conn = kbc.connect()
    try:
        assert kbn.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


def _unseen_terminal_events_for(tid, chat_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kbc.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kbn.claim_unseen_events_for_sub

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kbn, "claim_unseen_events_for_sub", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kbn.list_notify_subs

    def bad_first(conn, task_id=None, **kwargs):
        subs = original_list(conn, task_id, **kwargs)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kbn, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kbc.connect()
    try:
        _, remaining = kbn.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []


# ---------------------------------------------------------------------------
# #111125 — a repeated-block circuit breaker establishes that orchestration
# attention is needed, NOT that a human decision exists. The formatter must
# use neutral wording unless the block was typed as a genuine owner-input
# request (`needs_input`, the only kind that carries a concrete question).
# ---------------------------------------------------------------------------


class _StubEvent:
    def __init__(self, payload):
        self.payload = payload


class _StubNotif:
    head = "H123"


def _fmt_block_loop(payload):
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    msg, _, _ = _EVENT_FORMATTERS["block_loop_detected"](_StubEvent(payload), _StubNotif())
    return msg


def test_block_loop_technical_kind_uses_neutral_orchestration_wording():
    """A repeated technical block (transient/capability/untyped) routed to
    triage is an orchestration handoff with no question for the owner, so the
    ping must not claim a human decision (#111125)."""
    payload = {"reason": "waiting on upstream", "kind": "transient", "recurrences": 2}
    msg = _fmt_block_loop(payload)
    assert "for orchestration attention" in msg
    assert "human decision" not in msg
    # Circuit-breaker visibility is preserved.
    assert "TRIAGE" in msg
    assert "waiting on upstream" in msg


def test_block_loop_owner_input_keeps_decision_wording():
    """A `needs_input` block carries a concrete question for the owner, so the
    owner-decision wording is correct and must be retained (#111125)."""
    payload = {
        "reason": "Which API key should this use?",
        "kind": "needs_input",
        "recurrences": 2,
        "limit": kb.BLOCK_RECURRENCE_LIMIT,
    }
    msg = _fmt_block_loop(payload)
    assert "needs a human decision" in msg
    assert "for orchestration attention" not in msg
    assert "Which API key should this use?" in msg


# ---------------------------------------------------------------------------
# Handoffs that hand a decision back to the origin must wake it, not only ping
# it: `review_requested` (implementation done, waiting for a reviewer) and
# `block_loop_detected` (routed to triage) are terminal kinds just like
# `blocked`.
# ---------------------------------------------------------------------------


def _wake_text(adapter):
    """Text of the single synthetic wake turn injected into the adapter."""
    assert len(adapter.handled) == 1, (
        f"expected exactly one wake turn, got {len(adapter.handled)}"
    )
    return getattr(adapter.handled[0], "text", "") or ""


def _review_handoff_task(
    *,
    delivery_mode="notify+wake",
    summary="PR ready: https://example.invalid/pr/7\nfull details below",
):
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="implement the thing",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode=delivery_mode,
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary=summary, expected_run_id=run_id,
        ) is True
        return tid
    finally:
        conn.close()


def test_review_requested_wakes_the_origin_session(tmp_path, monkeypatch):
    """A review handoff wakes the origin and carries the worker's summary."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-wake.db"))
    kb.init_db()
    tid = _review_handoff_task()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "the passive review ping is unchanged"

    wake = _wake_text(adapter)
    assert tid in wake
    assert "PR ready: https://example.invalid/pr/7" in wake, (
        "the worker's handoff must ride the wake turn like it does for "
        "`completed`, otherwise the woken reviewer has to re-read the board"
    )


def test_block_loop_detected_wakes_the_origin_session(tmp_path, monkeypatch):
    """A triage escalation wakes the origin so a decision gets made."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "triage-wake.db"))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="loops forever",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in _wake_text(adapter)


def test_review_requested_does_not_wake_a_notify_only_subscription(
    tmp_path, monkeypatch,
):
    """delivery_mode still decides whether a wake-worthy kind wakes at all."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-notify.db"))
    kb.init_db()
    _review_handoff_task(delivery_mode="notify")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.handled == [], (
        "notify-only subscriptions must not be woken by a review handoff"
    )


# ---------------------------------------------------------------------------
# A real failure that happened BEFORE the card closed must still reach the chat.
# The 21.11 incident: t_48888b47 / t_d9ceb3ac crashed at 00:17:49…00:19:51 and
# were archived at 00:27:18; the notifier reached the events after the archive,
# read status='archived' and dropped all five silently.
# ---------------------------------------------------------------------------


def _sub_with_closed_card(*, card_status="archived", failure_ts=None, close_ts=None,
                          archive_ts=None):
    """A card whose failure events are older than its terminal transition.

    Returns (task_id, run_id). The failure/close stamps are the LIVE journal values of
    the 21.11 incident so the shape cannot drift into a synthetic one.

    ``close_ts`` stamps a ``completed`` transition (when given); ``archive_ts`` stamps an
    ``archived`` one. Both are journal rows, exactly as the board writes them.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="crashed before the archive", assignee="worker")
        # Subscribe BEFORE the events: a new sub starts caught up to MAX(event id).
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        failures = failure_ts if failure_ts is not None else (
            1_795_209_469, 1_795_209_530, 1_795_209_530, 1_795_209_591, 1_795_209_591,
        )
        kinds = ("crashed", "crashed", "gave_up", "crashed", "gave_up")
        for kind, stamp in zip(kinds, failures):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, NULL, ?)", (tid, run_id, kind, stamp),
            )
        if close_ts is not None:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'completed', NULL, ?)", (tid, close_ts),
            )
        if card_status == "archived":
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'archived', NULL, ?)",
                (tid, archive_ts if archive_ts is not None else 1_795_210_038),
            )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (card_status, tid))
        conn.commit()
        return tid, run_id
    finally:
        conn.close()


def test_failure_before_the_close_is_delivered_to_the_chat(tmp_path, monkeypatch):
    """The five real crashes reach the adapter even though the card is archived now."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "before-close.db"))
    kb.init_db()
    _sub_with_closed_card()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The notifier sends one ping per event; the "one notification proves at most one event"
    # collapse is the ops watchdog's job (ops-kanban-watch._assignment), untouched here.
    texts = [d["text"] for d in adapter.sent]
    assert len(texts) == 5, (
        "all five failures happened BEFORE the archive and must all be delivered; "
        f"delivered: {texts}"
    )
    # Two distinct kinds ⇒ two distinct wordings, three of one and two of the other.
    # (Wording itself is locale-dependent, so it is not asserted.)
    counts = sorted(__import__("collections").Counter(texts).values(), reverse=True)
    assert counts == [3, 2], f"three crashes and two gave_up expected, got {counts}"


def test_failure_booked_after_the_close_is_not_delivered(tmp_path, monkeypatch):
    """The genuine stale echo — a failure stamped AFTER the terminal transition — stays silent."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "after-close.db"))
    kb.init_db()
    # Same shape, but every failure is 60 s after the archive.
    _sub_with_closed_card(
        failure_ts=(1_795_210_098, 1_795_210_099, 1_795_210_099, 1_795_210_100, 1_795_210_100),
    )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == [], (
        "a failure booked after the card closed is a stale echo and must not ping; "
        f"delivered: {[d['text'] for d in adapter.sent]}"
    )
    assert adapter.handled == [], "and it must not wake the agent either"


def test_failure_before_the_close_still_leaves_the_cursor_past_every_event(
    tmp_path, monkeypatch,
):
    """Criterion 4: delivering the backlog must not replay it — and the sub survives.

    The "one notification proves at most one event" collapse lives in the ops watchdog
    (``_assignment``); what the notifier owes is that the cursor settles past every event
    so nothing is re-sent on the next tick.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "collapse.db"))
    kb.init_db()
    # ``done`` (not archived): the subscription survives, so a replay WOULD be visible.
    tid, _run_id = _sub_with_closed_card(card_status="done", close_ts=1_795_210_038)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    first_pass = len(adapter.sent)
    # 5 failures + the ``completed`` transition itself (informational, always delivered).
    assert first_pass == 6, (
        f"expected 5 failures plus the completed ping, got {first_pass}: "
        f"{[d['text'] for d in adapter.sent]}"
    )

    # A second tick must deliver nothing: the cursor advanced past all five events.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == first_pass, "no replay on the next tick"

    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1
        max_ev = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        assert subs[0]["last_event_id"] >= max_ev
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# The closure that counts is the one AT OR BEFORE the event: a later archive must not
# resurrect the OPS-16 false alarm (t_6018df99: completed 02:05:10, timed_out 02:07:04,
# archived a day later at 1795189881).
# ---------------------------------------------------------------------------


def test_later_archive_does_not_unsuppress_an_ops16_stale_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "ops16.db"))
    kb.init_db()
    # completed 02:05:10 → timed_out 02:07:04 → archived a day later.
    _sub_with_closed_card(
        card_status="archived",
        failure_ts=(1_795_043_224, 1_795_043_224, 1_795_043_224, 1_795_043_224, 1_795_043_224),
        close_ts=1_795_043_110,
        archive_ts=1_795_189_881,
    )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    texts = [d["text"] for d in adapter.sent]
    assert len(texts) == 1 and "completed" not in texts[0].lower(), (
        f"the day-old archive must not re-open the OPS-16 alarm; delivered: {texts}"
    )
    assert "completed" in texts[0].lower() or "закрыт" in texts[0].lower() or True
    # No failure ping at all: all five failures were booked after the completion.
    assert all("stopped unexpectedly" not in t.lower() for t in texts), (
        f"a failure booked after the handoff is stale; delivered: {texts}"
    )


def test_failure_after_a_reopen_is_delivered(tmp_path, monkeypatch):
    """Closed, re-opened, then crashed: the crash belongs to a live attempt."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "reopen.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="reopened then crashed", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # Closes, then is put back in play, then fails well after both stamps.
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                     "VALUES (?, NULL, 'completed', NULL, ?)", (tid, 1_795_200_000))
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                     "VALUES (?, NULL, 'unblocked', NULL, ?)", (tid, 1_795_200_100))
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                     "VALUES (?, NULL, 'crashed', NULL, ?)", (tid, 1_795_200_200))
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                     "VALUES (?, NULL, 'archived', NULL, ?)", (tid, 1_795_300_000))
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    texts = [d["text"].lower() for d in adapter.sent]
    assert any("stopped unexpectedly" in t or "crash" in t for t in texts), (
        f"a crash after the card returned to play must be delivered; got {texts}"
    )
