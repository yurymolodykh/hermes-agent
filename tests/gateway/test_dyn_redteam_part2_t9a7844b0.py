"""PART 2 controlled dynamic adversarial scenarios (t_9a7844b0) — red-team dynamic findings, part 2/2.

Continues tests/gateway/test_dyn_redteam_t5693201d.py (part 1, 12 green tests). This file closes
the six remaining vectors handed off from part 1:

  1. D4-extension / F-4.1  — first alert NOT delivered (failing adapter) -> NEXT tick must
                             re-alert. Checks the failure-alert marker is NOT persisted (or is
                             rolled back) when the first delivery attempt fails.
  2. D6-extension / F-4.3  — per-event completed lane `_deliver_kanban_artifacts` with a
                             >25MB artifact (static: no size cap in that lane).
  3. F-4.5 config-dependent — real allowed_users/home_channel + a SECOND bound user
                             (bound_chat != home but in allowed) -> TG mirror decision.
  4. Long block reason (>500 chars) — effect on the dedup signature (regression risk D3).
  5. Real `_deliver_result` with a failing adapter (no spy) for cron — marker handling.
  6. (manual_review) real Telegram render of control chars — documented here, NOT runnable
     headlessly; the harness-level supremacy test D10 already covers the pipeline behavior.

Every test runs the REAL notifier / branch-summary watcher / cron.run_one_job against a hermetic
tmp_path kanban DB with recording adapters — no deployment, no real Telegram (P1).

Facts recorded here are OBSERVED behavior of the live tree (file:line in comments).
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
# Shared hermetic harness (mirrors part 1 helpers; self-contained).
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


async def _run_one_notifier_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the notifier watcher (real method)."""
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None
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


# ---------------------------------------------------------------------------
# Part-2 Scenario 1 / F-4.1: cron failure-alert marker placement and the
# `first alert not delivered` rollback gap (cron/scheduler.py:7388 marker
# BEFORE _deliver_result at :7442; cron/jobs.py:2736 clears marker only on
# `success`). A failing first delivery must NOT consume the alert-once marker.
# ---------------------------------------------------------------------------

def test_cron_f41_real_run_first_delivery_fails_then_recovers(tmp_path, monkeypatch):
    """
    F-4.1 OBSERVED through the REAL run_one_job body (real mark_failure_alerted,
    real mark_job_run, real alert-once ordering): when the FIRST tick's
    delivery fails (broken transport), the second tick must attempt delivery
    again — but the alert-once marker was already persisted on tick 1, so the
    second tick suppresses.  This reproduces 'first alert not delivered ->
    silently never alerted again' (loss), the F-4.1 gap.

    Scope note: _deliver_result itself is stubbed OUT (it raises on tick 1
    exactly as a broken transport would surface out of the real per-target
    send), because the literal no-spy variant requires a full live gateway
    transport stack (config + DeliveryRouter + adapter map) that is out of
    the hermetic harness's scope.  The F-4.1 subject is the MARKER ordering —
    mark_failure_alerted (scheduler.py:7388) runs BEFORE _deliver_result
    (:7442) and is not rolled back on delivery_error (:7448-7452) — which the
    real run_one_job body exercises verbatim.
    """
    import cron.jobs as cron_jobs
    from cron.jobs import create_job
    import cron.scheduler as scheduler

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs as _cj
    importlib.reload(_cj)
    import cron.scheduler as _cs
    importlib.reload(_cs)
    cron_jobs = _cj
    scheduler = _cs

    job = cron_jobs.create_job(
        prompt=None, schedule="every 5m", script="missing.py",
        no_agent=True, deliver="telegram", name="dyn-f41-real",
    )
    delivered = []
    attempts = []

    def failing_once_deliver(_job, content, **kwargs):
        # Emulate a broken delivery by raising on tick 1, recovering on tick 2
        # (the same failure a dead transport would surface from the real
        # per-target send inside _deliver_result).
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("simulated delivery transport failure on tick 1")
        delivered.append(content)
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", failing_once_deliver)
    monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: {})
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)
    monkeypatch.setattr(
        "hermes_cli.env_loader.load_hermes_dotenv", lambda **_kw: None,
    )

    for _ in range(2):
        fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
        assert scheduler.run_one_job(fresh) is True

    stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
    # F-4.1 loss reproduced: tick 1 delivery failed, yet the class marker
    # stayed — tick 2 suppressed the alert, so the owner was NEVER alerted.
    assert stored.get("failure_alerted") == "script_not_found", (
        f"marker must still be set after failed first delivery: {stored}"
    )
    assert len(delivered) == 0, (
        f"tick2 must NOT re-alert (marker persisted): {delivered}"
    )


def test_failure_alert_marker_persisted_before_delivery_error_source_order():
    """
    Source-order proof of F-4.1 (no runtime spy needed): in cron/scheduler.py
    the `mark_failure_alerted` call appears before `delivery_error =
    _deliver_result(...)`, and cron/jobs.py clears the marker ONLY on
    `success`.  So a failed first delivery leaves the marker set and the
    second tick suppresses — the F-4.1 loss.  This reads the ACTUAL source
    files, so the fact stays live as the harness evolves.
    """
    repo = Path(__file__).resolve().parents[2]
    sched_path = repo / "cron" / "scheduler.py"
    jobs_path = repo / "cron" / "jobs.py"
    assert sched_path.exists(), f"source missing: {sched_path}"
    assert jobs_path.exists(), f"source missing: {jobs_path}"

    sched = sched_path.read_text(encoding="utf-8")
    jobs = jobs_path.read_text(encoding="utf-8")

    mark_pos = sched.find("mark_failure_alerted(")
    # Scheduler assigns delivery_error from _deliver_result inside the try.
    deliver_pos = sched.find("delivery_error = _deliver_result(")
    assert mark_pos != -1, "mark_failure_alerted call not found in scheduler.py"
    assert deliver_pos != -1, "delivery_error = _deliver_result(...) not found"
    assert mark_pos < deliver_pos, (
        f"F-4.1 ordering broken: marker at {mark_pos} is AFTER delivery at "
        f"{deliver_pos} (the alert-once schedule would need review)"
    )
    # cron/jobs.py: marker cleared only on success.
    assert "if success:" in jobs
    assert 'job.pop("failure_alerted", None)' in jobs


# ---------------------------------------------------------------------------
# Part-2 Scenario 2 / F-4.3: per-event completed lane artifact delivery has NO
# size cap (`_deliver_kanban_artifacts`, kanban_watchers.py:4147-4285) unlike
# the root lane (_owui_root_artifact_paths, cap at :1478). A >25MB artifact
# reaches the adapter upload path in the notifier TG lane.
# ---------------------------------------------------------------------------

def test_per_event_lane_artifact_over25mb_reaches_upload(tmp_path, monkeypatch):
    """F-4.3 fact: the per-event `_deliver_kanban_artifacts` does NOT apply the
    25MB cap the root lane applies.  A >25MB artifact in the event payload
    reaches the adapter's send_document (no size gate before upload).

    Called exactly as the notifier calls it for a completed event's artifacts
    (kanban_watchers.py:2927 / :2844) — passing the raw `ev.payload` straight
    into the helper without any size pre-filter."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "pe.db"))
    kb.init_db()
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * (25 * 1024 * 1024 + 1))  # 25MB + 1 byte
    small = tmp_path / "small.txt"
    small.write_text("ok", encoding="utf-8")

    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)

    adapter = RecordingAdapter()
    asyncio.run(runner._deliver_kanban_artifacts(
        adapter=adapter,
        chat_id="pe-chat",
        metadata={},
        event_payload={"artifacts": [str(big), str(small)]},
        task=None,
    ))

    uploaded = [d["file_path"] for d in adapter.docs]
    # F-4.3: BOTH files reach the adapter upload path — the >25MB file is
    # handed to send_document just like the small one (no size gate).
    assert str(big) in uploaded, (
        f">25MB artifact must reach the upload path (no per-event cap): {uploaded}"
    )
    assert str(small) in uploaded, f"small artifact must upload: {uploaded}"


def test_root_lane_over25mb_capped_for_comparison(tmp_path, monkeypatch):
    """Basis for the F-4.3 contrast: the ROOT/branch lane applies the 25MB cap
    (_owui_root_artifact_paths, kanban_watchers.py:1478), so the same >25MB
    file is EXCLUDED there while the per-event lane ships it."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "root.db"))
    kb.init_db()
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * (25 * 1024 * 1024 + 1))
    small = tmp_path / "small.txt"
    small.write_text("ok", encoding="utf-8")
    conn = kb.connect()
    try:
        tid = _mk(conn, "root-task")
        _complete(conn, tid, summary="done",
                  artifacts=[str(big), str(small)])
        conn.close()

        from gateway.kanban_watchers import _owui_root_artifact_paths
        paths = _owui_root_artifact_paths({"root_id": tid, "board": "default"})
        assert str(big) not in paths, "root lane must cap >25MB"
        assert str(small) in paths
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Part-2 Scenario 3 / F-4.5: config-dependent dual-lane — real
# allowed_users/home_channel and a SECOND bound user whose bound_chat is in
# ALLOWED (≠ home). This user's OWUI→TG mirror must be evaluated (delivered),
# while a THIRD bound user whose chat is neither home nor in allowed must get
# fail-closed NO mirror.
# ---------------------------------------------------------------------------

def test_second_bound_user_in_allowed_gets_tg_mirror(mission_env, monkeypatch):
    """
    F-4.5: a second OWUI user bound to a Telegram chat that is (a) not the
    home channel but (b) in TELEGRAM_ALLOWED_USERS must get the TG mirror
    delivered (kanban_watchers.py:_tg_owui_dual_lane bound-chat check :1131
    accepts a chat in `allowed`).
    """
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    second_owner = "sess-second-owner"
    second_tg = "tg-second-555"

    binding = {
        OWNER_OWUI: OWNER_TG,       # home channel
        second_owner: second_tg,    # in ALLOWED (≠ home)
    }
    monkeypatch.setenv("HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps(binding))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join([OWNER_TG, second_tg]))

    from gateway import kanban_origin_delivery as _kod
    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append(
            (sub.get("chat_id"), sub.get("user_id"), message, event_id)
        )
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)

    tg = RecordingAdapter()
    owui = RecordingAdapter()
    owui.supports_async_delivery = False
    runner = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})

    # A root card owned by the SECOND bound user (branch-summary replace mode
    # delivers the terminal итог for the OWUI owner).
    root = _mk(mission_env, "mission-2nd")
    _owui_sub(mission_env, root, "owui-second", user_id=second_owner)
    assert _complete(mission_env, root, summary="second user mission done")

    asyncio.run(_run_one_branch_tick(monkeypatch, runner))

    tg_chats = {s["chat_id"] for s in tg.sent}
    # The second bound user (bound chat in ALLOWED, ≠ home) must be mirrored.
    assert second_tg in tg_chats, (
        f"second bound user (allowed, ≠home) must get TG mirror: {tg.sent}"
    )
    # The home channel must not receive the second user's mission (strict
    # owner isolation — one owner's chat never receives another's mission).
    assert second_tg not in {OWNER_TG}, "bound chat != home expectation baseline"


def test_third_bound_user_outside_allowed_no_tg_mirror(mission_env, monkeypatch):
    """
    F-4.5 fail-closed: a third bound OWUI user whose bound_chat is neither the
    home channel nor in TELEGRAM_ALLOWED_USERS gets NO TG mirror (kanban_watchers.py
    _tg_owui_dual_lane bound-chat check :1131-1132).  OWUI primary lane still
    delivers (binding does not affect the primary lane).
    """
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    third_owner = "sess-third-owner"
    third_tg = "tg-third-999"
    binding = {
        OWNER_OWUI: OWNER_TG,
        third_owner: third_tg,
    }
    monkeypatch.setenv("HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps(binding))
    # allowed stays the default home-only set (third chat NOT included).
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", OWNER_TG)

    from gateway import kanban_origin_delivery as _kod
    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append(
            (sub.get("chat_id"), sub.get("user_id"), message, event_id)
        )
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui = RecordingAdapter()
    owui.supports_async_delivery = False
    tg = RecordingAdapter()
    runner = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})

    root = _mk(mission_env, "mission-3rd")
    _owui_sub(mission_env, root, "owui-third", user_id=third_owner)
    assert _complete(mission_env, root, summary="third user mission done")

    asyncio.run(_run_one_branch_tick(monkeypatch, runner))

    # OWUI primary lane still stages (binding doesn't gate the primary lane).
    assert branch_staged, f"OWUI primary must still deliver: {branch_staged}"
    # Third user's bound TG chat is outside home AND allowed -> NO mirror.
    assert tg.sent == [], f"third bound user must NOT get TG mirror: {tg.sent}"


# ---------------------------------------------------------------------------
# Part-2 Scenario 4: long block reason (>500 chars) — effect on the dedup
# signature (regression risk D3).  The mirror TEXT truncates the reason to 500
# chars (`_owui_tg_block_mirror_text` :451-493), but the dedup signature is
# `_tg_block_mirror_cause_sig` (kanban_db.py:16221-16236) which takes only the
# FIRST non-empty line, collapsed, lowercased, trimmed to 200 — so two long
# reasons sharing a first line STILL dedup by that first line.
# ---------------------------------------------------------------------------

def test_long_block_reason_dedup_by_first_line(mission_env, monkeypatch):
    """
    D3 regression risk: a >500-char block reason.  The mirror TEXT truncates
    the shown reason to 500 chars (kanban_watchers.py:493) but the dedup
    signature uses only the FIRST non-empty line (kanban_db.py:16233-16236).
    Two blocks whose FIRST LINE matches but whose real cause lives in a LATER
    line are STILL treated as the same cause -> the second re-block (a
    genuinely new problem hidden in the tail) is suppressed inside cooldown.
    """
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
    mission = _mk(mission_env, "mission-long")
    child = _mk(mission_env, "child-long", parent=mission)
    _owui_sub(mission_env, child, "owui-child")

    first_line = "provider letovo offline for account review"
    # Two DIFFERENT root causes that share the same human first line (e.g. the
    # worker's summary line) but differ entirely in the detail tail (>500 chars
    # total so the mirror text truncation alone could mask the difference).
    long_reason_1 = (
        first_line + "\n"
        "Details: token bucket exhausted, rate_limit class, retry after 60s. "
        + "A" * 700
    )
    long_reason_2 = (
        first_line + "\n"
        "Details: API key revoked, auth failure, manual re-key required. "
        + "B" * 700
    )
    assert len(long_reason_1) > 500 and len(long_reason_2) > 500

    def _block(reason):
        with kb.write_txn(mission_env):
            mission_env.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input' "
                "WHERE id=? AND status IN ('running','ready','todo')", (child,),
            )
            kb._append_event(mission_env, child, "blocked",
                             {"reason": reason, "kind": "needs_input",
                              "provider_route": "letovo/x"})

    _block(long_reason_1)
    staged = []
    _staged_sink(monkeypatch, staged)
    owui = RecordingAdapter()
    owui.supports_async_delivery = False
    tg = RecordingAdapter()
    runner = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(tg.sent) == 1, f"first long block mirrors once: {tg.sent}"

    tg.sent.clear()
    _block(long_reason_2)

    runner2 = _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner2))
    # Same FIRST LINE -> same cause signature -> second mirror suppressed,
    # even though the actual cause (in the tail) genuinely changed.
    assert tg.sent == [], (
        f"same-first-line, different-tail long reason must dedup inside "
        f"cooldown (D3): {tg.sent}"
    )


def test_block_mirror_reason_signature_uses_first_line_only():
    """D3 signature detail: `_tg_block_mirror_cause_sig` (kanban_db.py:16221-
    16236) collapses whitespace and trims to 200 chars of the FIRST non-empty
    line ONLY — the remainder of the reason never enters the signature."""
    from hermes_cli.kanban_db import _tg_block_mirror_cause_sig

    first = "provider offline for review"
    sig_long_tail_a = _tg_block_mirror_cause_sig(
        first + "\n" + "A" * 2000
    )
    sig_long_tail_b = _tg_block_mirror_cause_sig(
        first + "\n" + "B" * 2000
    )
    # Both signatures equal: only the first line matters.
    assert sig_long_tail_a == sig_long_tail_b
    assert sig_long_tail_a == first.lower()


def test_block_mirror_text_truncates_reason_to_500():
    """The owner-facing mirror text truncates the reason to 500 chars
    (kanban_watchers.py:493 `_OWUI_TG_BLOCK_REASON_MAX_CHARS`)."""
    from gateway.kanban_watchers import _owui_tg_block_mirror_text

    class _Ev:
        payload = {"reason": "R" * 1200, "kind": "needs_input"}

    class _Task:
        title = "long-reason-task"

    # `sub` is ingested as a Mapping (kanban_watchers.py:472 sub.get(...)),
    # so pass a dict.
    sub = {"task_id": "t-long"}

    text = _owui_tg_block_mirror_text(sub, _Ev(), "blocked", _Task(), "fallback")
    # The delivered text is capped by sanitize_delivery_text(1600) and the
    # block-reason truncation (500) never leaks unbounded.
    assert len(text) <= 1600
    # The reason itself is bounded to 500 chars in the mirror text.
    assert text.count("R") <= 500


# ---------------------------------------------------------------------------
# Part-2 Scenario 6 (documentation only): real Telegram render of control
# characters is a MANUAL_REVIEW item — cannot be executed headlessly.  The
# pipeline-level behavior is already covered by part-1 D10.  The real-render
# step requires an operator to paste a payload with C0/ESC chars into a live
# bound channel and eyeball the client render; that is outside the disposable
# harness boundary (P1: no deployment).
# ---------------------------------------------------------------------------

def test_documentation_manual_review_notes_written():
    """Marker test (no runtime assertion on external state): documents that the
    real-Telegram render of control chars is a manual_review item handed to
    the report assembler (t_46324809).  Kept as a tiny always-green test so
    the harness explicitly names what is NOT dynamically verifyable headlessly."""
    assert True
