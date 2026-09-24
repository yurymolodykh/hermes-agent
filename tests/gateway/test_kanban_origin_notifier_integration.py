"""Actual notifier cursor remains pinned until an exact OWUI persisted ACK."""
import asyncio
import json
import sqlite3

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.origin_delivery_worker import drain_openwebui
from gateway.origin_outbox import counts
from hermes_cli import kanban_db as kb

RAW = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"

class NoPush:
    supports_async_delivery = False
    def __init__(self): self.sends = 0
    async def send(self, *a, **kw): self.sends += 1; raise AssertionError("send forbidden")

async def tick(monkeypatch, runner):
    original = asyncio.sleep
    async def sleep(delay):
        if delay == 5: return
        runner._running = False
        await original(0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await runner._kanban_notifier_watcher(interval=1)

def test_registry_path_alone_does_not_enable_chat_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(tmp_path / "webui.db"))
    monkeypatch.delenv("HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    monkeypatch.setattr(GatewayRunner, "_active_profile_name", lambda self: "01-orchestrator")
    import gateway.origin_delivery_worker as worker
    calls = []
    monkeypatch.setattr(worker, "drain_openwebui", lambda *a: calls.append(a))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.API_SERVER: NoPush()}
    runner._kanban_dispatcher_lock_handle = object()
    asyncio.run(tick(monkeypatch, runner))
    assert calls == []


def test_notifier_stages_then_advances_only_after_owned_readback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = tmp_path / "webui.db"
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(registry))
    monkeypatch.setenv("HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED", "1")
    monkeypatch.setattr(GatewayRunner, "_active_profile_name", lambda self: "01-orchestrator")
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (RAW, "owner-A", json.dumps({"history": {
            "messages": {"u": {"id": "u", "role": "user", "parentId": None}}, "currentId": "u"}})))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="root", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="api_server", chat_id="owui-" + RAW,
                          notifier_profile="01-orchestrator", user_id="owner-A", delivery_mode="notify+wake")
        kb.complete_task(conn, task_id, summary="finish")
        initial = kb.list_notify_subs(conn, notifier_profiles=["01-orchestrator"])[0]["last_event_id"]
    finally: conn.close()
    adapter = NoPush()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.API_SERVER: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    asyncio.run(tick(monkeypatch, runner))
    conn = kb.connect()
    try:
        current = kb.list_notify_subs(conn, notifier_profiles=["01-orchestrator"])[0]["last_event_id"]
        assert current == initial
    finally: conn.close()
    outbox = tmp_path / "delivery-outbox.sqlite"
    assert counts(outbox)["pending"] == 1
    # The second gateway tick itself drains the outbox and advances the cursor.
    runner._running = True
    asyncio.run(tick(monkeypatch, runner))
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, notifier_profiles=["01-orchestrator"])[0]["last_event_id"] > initial
    finally: conn.close()
    assert adapter.sends == 0
    assert counts(outbox)["acknowledged"] == 1
