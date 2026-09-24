"""Kanban notifier behavior on stateless (api_server) subscriptions.

Covers the wrong-session-wake / silent-loss fixes:
* a SendResult(success=False) return (the API server's send() stub) rewinds
  the cursor instead of advancing past a never-delivered event;
* api_server subscriptions wake their ``chat_id`` delivery destinations via
  the /v1/chat/completions self-post, never task ``session_id`` provenance or
  handle_message (which would derive a different session key).
"""

import asyncio
import json
import subprocess
import sys

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class SoftFailAdapter:
    """Push-capable adapter whose send() returns SendResult(success=False)
    WITHOUT raising — previously treated as delivered (event lost)."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        return SendResult(success=False, error="soft failure")


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(self):
        self._host = "127.0.0.1"
        self._port = 8642
        self._api_key = "k"
        self._model_name = "hermes"
        self.handle_message_calls = []
        self.send_calls = 0

    async def send(self, chat_id, text, metadata=None):
        self.send_calls += 1
        return SendResult(
            success=False,
            error="API server uses HTTP request/response, not send()",
        )

    async def handle_message(self, event):
        self.handle_message_calls.append(event)


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapters):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _create_completed_subscription(platform, chat_id, session_id=None):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="notify once", assignee="worker", session_id=session_id,
        )
        kb.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        kb.complete_task(conn, tid, summary="done once")
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid, platform, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform=platform,
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


@pytest.mark.windows_only
def test_provider_outage_wakes_origin_with_full_operator_question(tmp_path, monkeypatch):
    """Real 74 exit -> durable block -> api_server wake in the same origin."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "outage.db"))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="specific route", assignee="worker",
                             model_override="gemma4:26b", provider_override="local",
                             session_id="worker-session")
        kb.add_notify_sub(conn, task_id=tid, platform="api_server",
                          chat_id="owner-session", delivery_mode="notify+wake")
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:test")
        proc = subprocess.Popen([sys.executable, "-c", "import sys;sys.exit(74)"],
                                creationflags=subprocess.CREATE_NO_WINDOW)
        kb._windows_worker_processes[proc.pid] = proc
        assert proc.wait(timeout=10) == 74
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (proc.pid, tid))
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        kb.reap_worker_zombies()
        assert kb.detect_crashed_workers(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"

    posts = []
    async def fake_self_post(adapter, *, text, session_id):
        posts.append((text, session_id))
    import gateway.wake as wake_mod
    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(posts) == 1
    text, session_id = posts[0]
    assert session_id == "owner-session"
    assert "local/gemma4:26b" in text
    assert "letovo/deepseek-v4-flash" in text
    assert "доступность и качество альтернативы не подтверждены" in text
    assert "Без вашего явного ответа переключения не будет" in text
    assert _unseen_terminal_events(tid, "api_server", "owner-session"), (
        "a separate self-post response is not visible in the owner's browser"
    )
    with kb.connect() as conn:
        pending = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='delivery_pending'",
            (tid,),
        ).fetchall()
        assert len(pending) == 1
        marker = json.loads(pending[0]["payload"])
        assert "non-push" in marker["reason"]
        assert marker["destination"] == "owner-session"
        assert isinstance(marker["source_event_id"], int)
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(posts) == 1, "pending question must not spawn repeated invisible agent turns"


def test_apiserver_sub_wakes_subscription_destination_via_self_post(tmp_path, monkeypatch):
    """An api_server subscription wakes its chat_id destination, not the
    task's worker-session provenance or a build_session_key()-derived session."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver.db"))
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server", "origin-session", session_id="worker-session",
    )

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    adapter = ApiServerLikeAdapter()
    runner = _make_runner({Platform.API_SERVER: adapter})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.handle_message_calls == [], (
        "api_server wake must not go through handle_message (wrong-session bug)"
    )
    assert len(posts) == 1
    assert posts[0]["session_id"] == "origin-session"
    assert all(post["session_id"] != "worker-session" for post in posts)
    wake_text = posts[0]["text"]
    assert tid in wake_text
    # Graph-safe wake turn (#70752): the synthetic turn must carry the
    # worker's completion handoff and the don't-recreate guidance so a
    # woken orchestrator doesn't re-decompose existing work.
    assert "done once" in wake_text, "creator wake must carry the worker handoff"
    assert "not a request to decompose" in wake_text.lower()
    assert "do not recreate" in wake_text.lower()
    # The wake self-post IS the delivery on this path (no separate text-ping
    # fallback is attempted for stateless api_server subs) — cursor advances
    # once the wake succeeds.
    assert _unseen_terminal_events(tid, "api_server", "origin-session") == []


def test_apiserver_failed_wake_retains_subscription_after_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "failed-wake.db"))
    kb.init_db()
    tid = _create_completed_subscription("api_server", "origin-session")
    import gateway.wake as wake_mod
    calls = []

    async def failed_post(adapter, *, text, session_id):
        calls.append(session_id)
        raise ConnectionError("self-post unavailable")

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", failed_post)
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})
    for _ in range(13):
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(calls) == 13
    with kb.connect() as conn:
        sub = conn.execute("SELECT last_event_id FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchone()
        assert sub is not None
        assert _unseen_terminal_events(tid, "api_server", "origin-session")


def test_apiserver_subscriptions_have_independent_wake_destinations(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver-multi.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="notify both",
            assignee="worker",
            session_id="worker-session",
        )
        for chat_id in ("origin-a", "origin-b"):
            kb.add_notify_sub(
                conn,
                task_id=tid,
                platform="api_server",
                chat_id=chat_id,
            )
        kb.complete_task(conn, tid, summary="done once")
    finally:
        conn.close()

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert sorted(post["session_id"] for post in posts) == ["origin-a", "origin-b"]
    assert all(post["session_id"] != "worker-session" for post in posts)
    assert _unseen_terminal_events(tid, "api_server", "origin-a") == []
    assert _unseen_terminal_events(tid, "api_server", "origin-b") == []


def test_apiserver_wake_failure_rewinds_then_retries_destination(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver-retry.db"))
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server", "origin-session", session_id="worker-session",
    )
    attempted_sessions = []

    async def fail_once_then_succeed(adapter, *, text, session_id):
        attempted_sessions.append(session_id)
        if len(attempted_sessions) == 1:
            raise RuntimeError("simulated wake failure")

    import gateway.wake as wake_mod

    monkeypatch.setattr(
        wake_mod,
        "_self_post_chat_completion",
        fail_once_then_succeed,
    )
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert _unseen_terminal_events(tid, "api_server", "origin-session")

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert attempted_sessions == ["origin-session", "origin-session"]
    assert "worker-session" not in attempted_sessions
    assert _unseen_terminal_events(tid, "api_server", "origin-session") == []

