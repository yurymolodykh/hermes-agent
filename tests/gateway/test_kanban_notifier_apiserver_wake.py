"""A 200 self-post cannot count as delivery for stateless API subscribers."""
import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from gateway.platforms.base import SendResult

class ApiServerLikeAdapter:
    supports_async_delivery = False
    def __init__(self): self.send_calls = 0
    async def send(self, chat_id, text, metadata=None):
        self.send_calls += 1
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

async def tick(monkeypatch, runner):
    real_sleep = asyncio.sleep
    async def fake_sleep(delay):
        if delay == 5: return
        runner._running = False
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)

def test_missing_server_owned_origin_retains_cursor_and_never_self_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_OPENWEBUI_DB_PATH", raising=False)
    kb.init_db()
    conn = kb.connect()
    try:
        task = kb.create_task(conn, title="notify once", assignee="worker", session_id="worker-session")
        kb.add_notify_sub(conn, task_id=task, platform="api_server", chat_id="unbound-session", delivery_mode="notify+wake")
        kb.complete_task(conn, task, summary="done once")
        original = kb.list_notify_subs(conn)[0]["last_event_id"]
    finally: conn.close()
    import gateway.wake as wake
    async def reject_selfpost(*args, **kwargs): raise AssertionError("self-post is not delivery")
    monkeypatch.setattr(wake, "_self_post_chat_completion", reject_selfpost)
    adapter = ApiServerLikeAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.API_SERVER: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    asyncio.run(tick(monkeypatch, runner))
    conn = kb.connect()
    try: assert kb.list_notify_subs(conn)[0]["last_event_id"] == original
    finally: conn.close()
    assert adapter.send_calls == 0
    assert not (tmp_path / "delivery-outbox.sqlite").exists()
