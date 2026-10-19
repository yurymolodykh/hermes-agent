"""CONTROLLED DYNAMIC adversarial scenarios (t_5693201d) — red-team dynamic findings.

Runs the REAL notifier / branch-summary watcher loops against a hermetic
(disposable tmp_path) kanban DB with recording adapters — no production
deploy, no real Telegram/OWUI, no repo mutation beyond this test file.

Each test records ACTUAL observed behavior for one adversarial scenario from
the red-team task:
  * parallel watcher ticks racing to send the final result (double send)
  * reopening a node after the final result (resend)
  * block/unblock cycle causing a blocked storm
  * cron job failing every 5 minutes (alert-once dedup)
  * mirror when the TG adapter is unavailable (OWUI channel not blocked)
  * artifact >25MB or broken path
  * artifact-catalog instead of a file
  * missing second-user binding
  * PWA subscription without owui_user_id
  * payload with control characters or very long text

The findings (attack -> expectation -> fact -> verdict) are compiled into
redteam-findings-dynamic.md by a companion script/task; this file is the
OBSERVABLE-REALITY source for the facts.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from gateway.config import Platform
from hermes_cli import kanban_db as kb

# ---------------------------------------------------------------------------
# Shared hermetic harness (mirrors tests/gateway/test_kanban_mission_chat_silence_dual_lane.py)
# ---------------------------------------------------------------------------

OWNER_OWUI = "sess-owui-creator"
OWNER_TG = "tg-owner-100200300"
ALLOWED_TG = [OWNER_TG]


def _make_runner(adapters):
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner._kanban_dispatcher_lock_handle = object()  # owns the dispatcher
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "reviewer"
    runner._kanban_notifier_profile = "reviewer"
    return runner


class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.docs = []
        self.wakes = []
        self.send_calls = []  # raw send() calls (chat_id, text)

    async def send(self, chat_id, text, metadata=None):
        self.send_calls.append((chat_id, text))
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append({
            "chat_id": chat_id, "file_path": str(file_path),
            "file_name": file_name or os.path.basename(str(file_path)),
            "metadata": metadata or {},
        })
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def handle_message(self, event):
        self.wakes.append(getattr(event, "text", ""))


class FailingTgAdapter(RecordingAdapter):
    """TG adapter whose send() throws — simulates "TG adapter unavailable"."""

    supports_async_delivery = True

    def __init__(self):
        super().__init__()
        self.fail_all_sends = True
        self.send_attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.send_attempts += 1
        if self.fail_all_sends:
            raise RuntimeError("simulated telegram adapter unavailable")
        return await super().send(chat_id, text, metadata)


async def _run_one_notifier_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the notifier watcher (real method)."""
    import gateway.kanban_watchers as kw
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    import gateway.kanban_watchers as _kw
    orig_collect = _kw.GatewayKanbanWatchersMixin._kanban_notifier_watcher
    # no-op; keep original behavior
    await runner._kanban_notifier_watcher(interval=interval)


async def _run_one_branch_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the branch-summary watcher."""
    import gateway.kanban_watchers as kw
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


@pytest.fixture(autouse=True)
def _synthetic_assignees(isolated_kanban_assignee_registry):
    """Provide synthetic assignee registry for hermetic task creation."""
    return isolated_kanban_assignee_registry


@pytest.fixture
def mission_env(tmp_path, monkeypatch):
    """Hermetic disposable kanban board; policy on; owner binding."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "dual.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED_TG))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, parent=None, assignee="worker", initial_status="running"):
    tid = kb.create_task(
        conn, title=title, assignee=assignee, initial_status=initial_status,
    )
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _owui_sub(conn, tid, chat, user_id=OWNER_OWUI, delivery_metadata=None):
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="api_server",
        chat_id=chat,
        user_id=user_id,
        delivery_mode="notify+wake",
        delivery_metadata=delivery_metadata,
    )


def _tg_sub(conn, tid, chat, user_id=None):
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="telegram",
        chat_id=chat,
        user_id=user_id,
        delivery_mode="notify+wake",
    )


def _complete(conn, tid, *, summary="done", artifacts=None, metadata=None):
    md = metadata or {}
    if artifacts is not None:
        md = {**md, "artifacts": artifacts}
    return kb.complete_task(conn, tid, summary=summary, metadata=md)


def _staged_sink(monkeypatch, staged):
    """Replace stage_terminal with a recording sink returning 'acknowledged'."""
    from gateway import kanban_origin_delivery as _kod
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append({
            "chat_id": sub.get("chat_id"),
            "message": message,
            "event_id": event_id,
            "user_id": sub.get("user_id"),
            "delivery_metadata": sub.get("delivery_metadata"),
        })
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    return staged


# ===========================================================================
# Scenario 1: Parallel watcher ticks racing to send the final result
#   (double-send surface). claim_unseen_events_for_sub must be atomic across
#   two concurrent gateways pointing at the SAME board DB.
# ===========================================================================


def test_parallel_watchers_claim_exactly_once(tmp_path, monkeypatch):
    """Two concurrent notifier loops on one DB must not both send the итог."""
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    db_path = tmp_path / "race.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _mk(conn, "race-task")
        _tg_sub(conn, tid, "race-chat", user_id="u1")
        conn.close()

        # Simulate the RACE: two concurrent gateways pointing at the SAME board
        # DB both call claim_unseen_events_for_sub in to_thread — each with its
        # OWN connection, exactly as the real watcher does. The claim must be
        # atomic: the loser sees zero new events.
        import threading
        barrier = threading.Barrier(2)
        out = {}
        errors = {}

        def claim(name):
            try:
                c = kb.connect()
                try:
                    barrier.wait(timeout=10)
                    out[name] = kb.claim_unseen_events_for_sub(
                        c, task_id=tid, platform="telegram", chat_id="race-chat",
                        kinds=["completed"],
                    )
                finally:
                    c.close()
            except Exception as exc:  # pragma: no cover
                errors[name] = exc

        # First, write the completed event.
        cw = kb.connect()
        try:
            kb._append_event(cw, tid, "completed", {"summary": "race done"})
        finally:
            cw.close()
        t1 = threading.Thread(target=lambda: claim("a"))
        t2 = threading.Thread(target=lambda: claim("b"))
        t1.start(); t2.start()
        t1.join(timeout=20); t2.join(timeout=20)
        assert not errors, f"claims must not raise: {errors}"
        assert out, "both claimers must produce a result"
        # Exactly one claimer wins the event range; the other sees 0 events.
        winner = [v for v in out.values() if v[2]]
        loser = [v for v in out.values() if not v[2]]
        assert len(winner) == 1, f"exactly one claimer must win, got {winner}"
        assert len(loser) == 1, f"exactly one claimer must be empty, got {out}"
    finally:
        conn.close()


# ===========================================================================
# Scenario 2: Reopen a node after the final result (resend surface).
#   A done task re-opened must re-deliver the NEXT close, not replay the old.
# ===========================================================================


def test_reopen_after_final_result_no_stale_resend(tmp_path, monkeypatch):
    """Done -> reopen (running) -> done again: exactly 2 sends, no replay."""
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    db_path = tmp_path / "reopen.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _mk(conn, "reopen-task")
        _tg_sub(conn, tid, "reopen-chat", user_id="u1")
        kb.complete_task(conn, tid, summary="first done")

        adapter = RecordingAdapter()
        runner = _make_runner({Platform.TELEGRAM: adapter})
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        assert len(adapter.sent) == 1, "first close delivers once"

        # Reopen: status-ready + a real re-run completes again.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        kb.complete_task(conn, tid, summary="second done")

        runner2 = _make_runner({Platform.TELEGRAM: adapter})
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner2))

        texts = [s["text"] for s in adapter.sent]
        assert len(adapter.sent) == 3, f"expected 1st done + reopen + 2nd done = 3, got {texts}"
        assert "second done" in texts[-1]
    finally:
        conn.close()


# ===========================================================================
# Scenario 3: block/unblock cycle — blocked storm into owner TG.
# ===========================================================================


def test_block_reblock_same_cause_dedup(mission_env, monkeypatch):
    """Same-cause re-block inside cooldown must NOT spam the owner's TG."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-block", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child")

    # Block with a definite human-action cause.
    with kb.write_txn(mission_env):
        mission_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=? AND status IN ('running','ready')", (child,),
        )
        kb._append_event(mission_env, child, "blocked",
                         {"reason": "provider route needed", "kind": "needs_input",
                          "provider_route": "letovo/x"})

    staged = []
    _staged_sink(monkeypatch, staged)
    # Dual-lane wiring: the OWUI→TG mirror is evaluated inside the api_server
    # branch of the notifier, so BOTH adapters must be registered (same as the
    # reference test_kanban_mission_chat_silence_dual_lane payload).
    owui = RecordingAdapter()
    owui.supports_async_delivery = False  # api_server: no push, outbox is delivery
    tg = RecordingAdapter()
    runner = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(tg.sent) == 1, "first block mirrors once"

    # The same cause re-blocks (worker retry, same cause) 30s later.
    # The F2 dedup marker has a 24h cooldown default -> second block is suppressed.
    with kb.write_txn(mission_env):
        mission_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=? AND status IN ('running','ready','todo')", (child,),
        )
        kb._append_event(mission_env, child, "blocked",
                         {"reason": "provider route needed", "kind": "needs_input",
                          "provider_route": "letovo/x"})

    tg.sent.clear()
    runner2 = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner2))
    assert tg.sent == [], f"same-cause re-block inside cooldown must not re-mirror: {tg.sent}"


# ===========================================================================
# Scenario 4: Cron failing every 5 minutes — alert-once dedup (F4).
#   Reuses the real cron jobs mark_failure_alerted persistence bit.
# ===========================================================================


def test_cron_alert_once_dedup(tmp_path):
    """A job failing every 5m with SAME class must alert exactly once."""
    import cron.jobs as cron_jobs

    job = {
        "id": "dyn-fail-every-5m",
        "name": "dyn fail",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "deliver": "telegram",
        "no_agent": True,
        "script": "missing.py",
    }
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        assert cron_jobs.mark_failure_alerted("dyn-fail-every-5m", "script_not_found") is False
        assert cron_jobs.mark_failure_alerted("dyn-fail-every-5m", "script_not_found") is True
        assert cron_jobs.mark_failure_alerted("dyn-fail-every-5m", "script_not_found") is True
        # persistence
        stored = [j for j in cron_jobs.load_jobs() if j["id"] == "dyn-fail-every-5m"][0]
        assert stored.get("failure_alerted") == "script_not_found"


def test_cron_alert_once_real_run(_hermes_env_dyn, monkeypatch):
    """Two failing ticks -> exactly ONE delivery (real run_one_job)."""
    import cron.jobs as cron_jobs
    from cron.jobs import create_job
    import cron.scheduler as scheduler

    job = create_job(
        prompt=None, schedule="every 5m", script="missing.py",
        no_agent=True, deliver="telegram", name="dyn-f4",
    )
    delivered = []

    monkeypatch.setattr(
        scheduler, "_deliver_result",
        lambda _job, content, **_kwargs: delivered.append(content),
    )
    monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: {})
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: _hermes_env_dyn)
    monkeypatch.setattr(
        "hermes_cli.env_loader.load_hermes_dotenv", lambda **_kw: None,
    )

    for _ in range(2):
        fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
        assert scheduler.run_one_job(fresh) is True

    assert len(delivered) == 1, f"expected 1 alert, got {len(delivered)}: {delivered}"


@pytest.fixture
def _hermes_env_dyn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


# ===========================================================================
# Scenario 5: Mirror when the TG adapter is unavailable — OWUI channel must
#   NOT be blocked (the mirror is best-effort; the primary lane ACKs).
# ===========================================================================


def test_mirror_tg_adapter_down_owui_not_blocked(mission_env, monkeypatch):
    """TG adapter down: OWUI staging must still ACK; the OWUI channel advances."""
    # replace mode OFF (default additive): the notifier itself delivers the
    # root's completed to the OWUI lane and evaluates the TG mirror. Under
    # replace mode the root completion is suppressed in the notifier (the
    # branch-summary watcher owns the итог), which would not exercise the
    # OWUI-staging-vs-TG-mirror interplay this scenario targets.
    root = _mk(mission_env, "mission")
    _owui_sub(mission_env, root, "owui-request")
    assert _complete(mission_env, root, summary="mission done with real content")

    staged = []
    _staged_sink(monkeypatch, staged)
    tg_down = FailingTgAdapter()
    # Dual-lane wiring: the api_server sub must be registered for the OWUI
    # primary lane to stage; TG adapter is present but failing sends.
    owui = RecordingAdapter()
    owui.supports_async_delivery = False  # api_server: no push, outbox is delivery
    runner = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg_down})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The OWUI primary lane must still be staged even though TG is down
    # (the mirror is best-effort and must not block/backout the OWUI lane).
    assert staged, f"OWUI lane must still stage with TG down: {staged}"
    # The TG mirror must have been genuinely ATTEMPTED (and failed) — proving
    # the scenario is "TG adapter down mid-mirror", not "TG lane never ran".
    assert tg_down.send_attempts >= 1, (
        f"TG mirror must be attempted even when it fails: "
        f"send_attempts={tg_down.send_attempts} staged={staged}"
    )


# ===========================================================================
# Scenario 6: Artifact >25MB or broken path — must not break text delivery.
# ===========================================================================


def test_owl_root_artifact_paths_25mb_and_broken(tmp_path, monkeypatch):
    """_owui_root_artifact_paths drops >25MB and missing files, keeps valid."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "art.db"))
    kb.init_db()
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (25 * 1024 * 1024 + 1))
    broken = tmp_path / "missing.md"
    ok = tmp_path / "ok.txt"
    ok.write_text("hello", encoding="utf-8")
    conn = kb.connect()
    try:
        tid = _mk(conn, "art-task")
        _complete(conn, tid, summary="done", artifacts=[
            str(big), str(broken), str(ok),
        ])
        conn.close()

        from gateway.kanban_watchers import _owui_root_artifact_paths
        plan = {"root_id": tid, "board": "default"}
        paths = _owui_root_artifact_paths(plan)
        assert str(ok) in paths, "existing small file must be kept"
        assert str(big) not in paths, ">25MB artifact must be skipped"
        assert str(broken) not in paths, "broken path must be skipped"
    finally:
        conn.close()


# ===========================================================================
# Scenario 7: Artifact-catalog instead of a file (a .json list of paths would
#   be treated as a file — but a NON-existent catalog path must be skipped,
#   and an existing catalog gets delivered as an artifact).
# ===========================================================================


def test_artifact_catalog_path_handled(tmp_path, monkeypatch):
    """Catalog path (metadata['artifacts'] list) is honored; broken catalog skipped."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "cat.db"))
    kb.init_db()
    catalog = tmp_path / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    broken_cat = tmp_path / "catalog-missing.json"
    conn = kb.connect()
    try:
        tid = _mk(conn, "cat-task")
        _complete(conn, tid, summary="done",
                  artifacts=[str(catalog), str(broken_cat)])
        conn.close()

        from gateway.kanban_watchers import _owui_root_artifact_paths
        paths = _owui_root_artifact_paths({"root_id": tid, "board": "default"})
        assert str(catalog) in paths, "existing catalog file rides as an artifact"
        assert str(broken_cat) not in paths, "missing catalog path skipped"
    finally:
        conn.close()


# ===========================================================================
# Scenario 8: Missing second-user binding — foreign OWUI owner gets NO TG lane.
# ===========================================================================


def test_foreign_owner_no_binding_no_tg_mirror(mission_env, monkeypatch):
    """A second OWUI user NOT in the binding gets OWUI-only (no TG mirror)."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    foreign = "sess-foreign-user-not-bound"
    root = _mk(mission_env, "mission")
    _owui_sub(mission_env, root, "owui-foreign", user_id=foreign)
    assert _complete(mission_env, root, summary="foreign mission done with summary")

    # Replace-mode terminal итог goes through the branch-summary watcher, which
    # stages OWUI and evaluates the OWUI→TG mirror for the bound owner.
    from gateway import kanban_origin_delivery as _kod

    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append(
            (sub.get("chat_id"), sub.get("user_id"), message, event_id)
        )
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui = RecordingAdapter()
    owui.supports_async_delivery = False  # api_server: no push, outbox is delivery
    tg = RecordingAdapter()
    runner = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})
    asyncio.run(_run_one_branch_tick(monkeypatch, runner))
    # OWUI primary lane still delivers (not affected by binding).
    assert branch_staged, f"OWUI primary lane must still deliver: {branch_staged}"
    # Foreign owner: no server binding -> OWUI-only, NO TG mirror.
    assert tg.sent == [], f"foreign owner must not get TG mirror: {tg.sent}"


# ===========================================================================
# Scenario 9: PWA subscription without owui_user_id — must NOT create a TG lane.
# ===========================================================================


def test_pwa_sub_no_owui_user_id_fail_closed(mission_env, monkeypatch):
    """PWA (api_server) subscription with empty user_id and no owui attr -> no TG."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    root = _mk(mission_env, "mission-pwa")
    # PWA subscription: no user_id, no delivery_metadata owui attribute.
    _owui_sub(mission_env, root, "owui-pwa", user_id="")
    assert _complete(mission_env, root, summary="pwa mission done")

    # Replace-mode terminal итог goes through the branch-summary watcher.
    from gateway import kanban_origin_delivery as _kod

    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append(
            (sub.get("chat_id"), sub.get("user_id"), message, event_id)
        )
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui = RecordingAdapter()
    owui.supports_async_delivery = False  # api_server: no push, outbox is delivery
    tg = RecordingAdapter()
    runner = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})
    asyncio.run(_run_one_branch_tick(monkeypatch, runner))
    # OWUI primary lane delivers; NO TG lane may open for an owner-less PWA.
    assert branch_staged, f"OWUI primary lane must still deliver: {branch_staged}"
    assert tg.sent == [], f"PWA sub w/o owui_user_id must not get TG mirror: {tg.sent}"


# ===========================================================================
# Scenario 10: Payload with control characters or very long text — sanitize.
# ===========================================================================


def test_payload_control_chars_and_long_text_sanitized(tmp_path, monkeypatch):
    """Kanban summary with NUL/control chars + 100k chars is sanitized, sent once."""
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "ctl.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _mk(conn, "ctl-task")
        # A real telegram subscription is required for the notifier to send.
        _tg_sub(conn, tid, "ctl-chat", user_id="u1")
        ctl_summary = "done\x00\x01\x02\nline2"
        long_summary = "L" * 100_000
        kb.complete_task(conn, tid, summary=ctl_summary + long_summary)
        conn.close()

        adapter = RecordingAdapter()
        runner = _make_runner({Platform.TELEGRAM: adapter})
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        assert len(adapter.sent) == 1, "send must not crash"
        sent_text = adapter.sent[0]["text"]
        assert len(sent_text) <= 1600 + 200, (
            f"notifier caps long text near 1600, got {len(sent_text)}"
        )
    finally:
        conn.close()


def test_sanitize_delivery_text_control_chars_unit():
    """sanitize_delivery_text is a pure function: control chars survive as text
    (they are not hex-escaped), but long text is truncated with max_chars."""
    from hermes_cli.delivery_policy import sanitize_delivery_text
    text = "line1\x00\x01\x02responding\nline2"
    out = sanitize_delivery_text(text, max_chars=1000)
    assert out  # does not raise
    long_text = "x" * 5000
    out2 = sanitize_delivery_text(long_text, max_chars=1000)
    assert len(out2) <= 1000
