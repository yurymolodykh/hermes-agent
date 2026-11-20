"""A wake refused because the destination store holds no receipt must not retry forever.

``local_receipt`` raised a bare ``RuntimeStoreError('storage_unavailable')`` for two very different
facts: the receipt is absent from this profile's store (the subscription points at a session that
lives in another profile, or the task was archived and its session removed), or its stored JSON is
unreadable. ``deliver`` treated every ``WakeNotAccepted`` as transient and rewound the claim, so the
kanban notifier re-claimed the same event every 5s and re-logged the refusal indefinitely
(13 412 journal lines in 24h for one archived task, subscription ``t_3168b24f``).

The refusal now names its check (``field``), that check distinguishes "absent" from "corrupt", and
a field that proves the destination can never serve the wake takes the bounded failure path: the
subscription is dropped after ``MAX_SEND_FAILURES`` attempts instead of retried forever.
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner  # module level: a test-time import re-runs hermes_bootstrap
from gateway.wake import WakeNotAccepted
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state_runtime import RuntimeStoreError


def _session_db(tmp_path):
    from hermes_state import SessionDB
    return SessionDB(tmp_path / "state.db")


def test_missing_receipt_names_the_absent_key_not_a_bare_code(tmp_path):
    """``field=receipt_missing`` with the key and the store path, instead of ``storage_unavailable``."""
    from hermes_state_local import POLICY_PREFIX, local_receipt

    db = _session_db(tmp_path)
    try:
        with pytest.raises(RuntimeStoreError) as caught:
            local_receipt(db, "local-absent")
        exc = caught.value
        assert exc.reason == "storage_unavailable"  # the bounded wire code is unchanged
        assert exc.field == "receipt_missing"
        assert str(db.db_path) in exc.detail
        assert POLICY_PREFIX not in exc.detail, "the session id is already in the log line"
    finally:
        db.close()


def test_corrupt_receipt_names_the_key_and_the_parse_error(tmp_path):
    """An unreadable receipt is ``receipt_corrupt``, with the key and what ``json`` rejected."""
    from hermes_state_local import POLICY_PREFIX, local_receipt

    db = _session_db(tmp_path)
    try:
        db.set_meta(POLICY_PREFIX + "local-corrupt", "{not json")
        with pytest.raises(RuntimeStoreError) as caught:
            local_receipt(db, "local-corrupt")
        exc = caught.value
        assert exc.reason == "storage_unavailable"
        assert exc.field == "receipt_corrupt"
        assert str(db.db_path) in exc.detail
        assert "JSONDecodeError" in exc.detail
    finally:
        db.close()


class _RefusingAdapter:
    """Push adapter whose wake injection refuses with a store proof."""

    def __init__(self, refusal):
        self.sent, self.handled, self.refusal = [], [], refusal

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        self.handled.append(event)
        raise self.refusal


def _notifier_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))


def _subscribed_task(tmp_path, monkeypatch, *, old_code=False):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "perm-wake.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="permanent refusal", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                           chat_type="dm", delivery_mode="notify+wake")
        kb.complete_task(conn, tid, summary="done")
        return tid
    finally:
        conn.close()


def _sub_count(tid):
    conn = kbc.connect()
    try:
        return len(kbn.list_notify_subs(conn, tid))
    finally:
        conn.close()


def test_permanent_refusal_is_counted_not_silently_rewound(tmp_path, monkeypatch):
    """A ``receipt_missing`` refusal bumps the failure counter instead of rewinding the claim.

    On the old code this assertion failed: every ``WakeNotAccepted`` took the ``rewind()`` branch, so
    the counter stayed empty and the event was re-claimed on every tick forever.
    """
    tid = _subscribed_task(tmp_path, monkeypatch)
    refusal = WakeNotAccepted("storage_unavailable", reason="storage_unavailable",
                              field=None, detail=None)
    refusal.field = "receipt_missing"  # the store's own proof, as ``local_receipt`` now stamps it
    refusal.detail = "gateway.local_policy.v1:x absent"
    adapter = _RefusingAdapter(refusal)
    runner = _notifier_runner(adapter)

    _one_tick(monkeypatch, runner)

    assert len(adapter.handled) == 1, "the wake is still attempted once"
    assert list(runner._kanban_sub_fail_counts.values()) == [1], (
        "a store-proven refusal must take the bounded failure path; the old code rewound instead"
    )
    assert _sub_count(tid) == 1, "one refusal must not drop the subscription"


def test_permanent_refusal_drops_the_subscription_after_the_cap(tmp_path, monkeypatch):
    """Repeated store-proven refusals drop the sub instead of retrying forever."""
    from gateway.kanban_watchers_notifier import MAX_SEND_FAILURES

    tid = _subscribed_task(tmp_path, monkeypatch)
    refusal = WakeNotAccepted("storage_unavailable", reason="storage_unavailable",
                              field="receipt_missing", detail="absent")
    adapter = _RefusingAdapter(refusal)
    runner = _notifier_runner(adapter)

    for _ in range(MAX_SEND_FAILURES):
        if not _sub_count(tid):
            break
        runner._running = True
        _one_tick(monkeypatch, runner)

    assert _sub_count(tid) == 0, (
        "a refusal the destination store can never clear must drop the subscription after the cap"
    )


def test_transient_refusal_still_rewinds_and_retries(tmp_path, monkeypatch):
    """A draining/queue-full refusal keeps the durable subscription: it does clear on its own."""
    tid = _subscribed_task(tmp_path, monkeypatch)
    refusal = WakeNotAccepted("gateway is draining", reason="runtime_draining")
    refusal.permanent = True  # ``run_notifications`` may prove it permanent for the DRAIN, not here
    adapter = _RefusingAdapter(refusal)
    runner = _notifier_runner(adapter)

    _one_tick(monkeypatch, runner)

    assert runner._kanban_sub_fail_counts == {}, "a transient refusal must not count toward the cap"
    assert _sub_count(tid) == 1, "the subscription must survive a refusal that clears on its own"