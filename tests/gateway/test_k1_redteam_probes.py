"""Targeted deterministic probes — K1 red-team reproduction.

These reach into the REAL functions with real DB state and assert on the
exact invariants the mission Spect declares.  They are NOT part of the
mission's own test suite (which validates the same code along happy paths);
they attack the once-only claims, the F4 alert-once contract, and the
sanitize path with adversarial sequencing.

Run from the repo dir:
  uv run pytest -q tests/gateway/test_k1_redteam_probes.py -p no:cacheprovider
"""
import asyncio
import json
import os

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


# --------------------------------------------------------------------------
# Fixtures / recording adapter (same shape as the mission's own tests)
# --------------------------------------------------------------------------
@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "probe.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.docs = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append({"chat_id": chat_id, "file_path": file_path})


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_dispatcher_lock_handle = object()  # owns dispatcher -> includes unowned
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


def _mk(conn, title, assignee="worker"):
    return kb.create_task(conn, title=title, assignee=assignee)


def _complete_branch(conn, root, children):
    for tid in [root] + list(children):
        assert kb.complete_task(conn, tid, summary="done")


# --------------------------------------------------------------------------
# P1: THE MAIN CLAIM — the once-only branch итог marker is an UNCONDITIONAL
# INSERT.  Under two concurrent watchers (two gateway processes for the same
# profile — the exact deployment the per-event lane already serializes via
# claim_unseen_events_for_sub's BEGIN IMMEDIATE), both planners read
# "no marker", both mark, both deliver.  The marker is not a real claim.
# --------------------------------------------------------------------------
def test_p1_marker_claim_is_unconditional_insert(board_env):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    _complete_branch(board_env, root, [a])

    connA = kb.connect(board="default")
    connB = kb.connect(board="default")
    st = kb.branch_state(connA, root)
    assert st is not None and st["outcome"] == "success"
    # Both planners observe "no marker" (interleaving: neither has called
    # mark_branch_summary yet).
    assert kb.has_branch_summary(connA, root) is None
    assert kb.has_branch_summary(connB, root) is None

    # Both proceed to mark — the marker claim is an unconditional append,
    # there is NO conditional INSERT WHERE NOT EXISTS guard.
    idA = kb.mark_branch_summary(connA, root, {
        "fingerprint": st["fingerprint"], "outcome": "success", "failed": [],
        "message": "✅ Задача выполнена", "delivered": [], "root": root, "board": "default",
    })
    idB = kb.mark_branch_summary(connB, root, {
        "fingerprint": st["fingerprint"], "outcome": "success", "failed": [],
        "message": "✅ Задача выполнена", "delivered": [], "root": root, "board": "default",
    })
    connA.close(); connB.close()

    # TWO marker rows exist: the "once-only claim" did not serialize.
    conn3 = kb.connect(board="default")
    rows = conn3.execute(
        "SELECT COUNT(*) c FROM task_events WHERE task_id=? AND kind='branch_summary'",
        (root,),
    ).fetchone()
    conn3.close()
    assert rows["c"] == 2  # <-- TOCTOU double-claim proven

    # And both planners would deliver to the same channel -> double message.
    ackedA = kb.ack_branch_summary_channel(kb.connect(board="default"), root,
                                           platform="telegram", chat_id="chat-1", thread_id=None)
    delivered = kb.has_branch_summary(kb.connect(board="default"), root)
    assert delivered is not None
    # Latest marker is the one every ack writes to; the OTHER marker is
    # orphaned but its writer already delivered -> the channel saw 2 pings.
    assert idA != idB


# --------------------------------------------------------------------------
# P2: a routine intermediate flip (a child re-queued to `ready` by
# dependency-gating / review-reopen) makes branch_state None and CLEARS the
# delivered marker; the next full close re-sends a fresh итог.  That is the
# DOCUMENTED "reopen → re-send" contract (branch_state: "a reopen → re-close
# clears the marker while the branch is open, so a fresh итог is sent again"):
# the branch genuinely went terminal -> open -> terminal again, which IS a
# user-visible state change, so one fresh итог per close-cycle is correct —
# NOT a duplicate.  The once-only claim is per-close, not per-branch-life.
# (Note: a re-queue to `ready`, not `todo`, is the realistic dispatcher
# shape; `complete_task` only accepts running/ready/blocked/review -> done, so
# a `todo` flip would never re-close and would make this probe vacuous.)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_routine_flip_clears_delivered_marker_and_resends(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    _complete_branch(board_env, root, [a])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(adapter.sent) == 1  # user has the итог
    marker = kb.has_branch_summary(kb.connect(board="default"), root)
    assert marker is not None
    assert kb.branch_summary_pending_subs(
        marker, [{"platform": "telegram", "chat_id": "chat-1", "thread_id": ""}]) == []
    adapter.sent.clear()

    # A routine internal flip — the dispatcher re-queues the already-done
    # child back to `ready` (dependency-gating / review-reopen of a KID that
    # the user never sees directly).  NOT a user-driven reopen of the branch.
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='ready' WHERE id=?", (a,))

    # The tick sees the branch OPEN (branch_state None) and clears the marker;
    # nothing is delivered while the branch is open.
    assert kb.branch_state(kb.connect(board="default"), root) is None
    await _run_one_branch_tick(monkeypatch, runner)
    assert kb.has_branch_summary(kb.connect(board="default"), root) is None
    assert adapter.sent == []

    # The child completes again (worker finishes) -> branch terminal again.
    # Because the branch passed through OPEN, this is a genuine reopen ->
    # re-close, so exactly ONE fresh итог is re-sent (documented contract).
    kb.complete_task(board_env, a, summary="done")
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(adapter.sent) == 1  # fresh итог after the reopen -> re-close
    assert kb.has_branch_summary(kb.connect(board="default"), root) is not None


# --------------------------------------------------------------------------
# P3: F4 cron alert-once is keyed by error CLASS.  When one outage oscillates
# between two classes (provider timeout <-> rate limit) the persistent bit
# flips each tick and the owner is re-alerted on EVERY flip — the alert-once
# contract ("ровно один раз до устранения") degrades to per-flip alerts.
# --------------------------------------------------------------------------
def test_p3_f4_alert_once_class_oscillation(tmp_path, monkeypatch):
    from cron import jobs as cjobs
    from cron.scheduler import _cron_failure_alert_class

    # t0: script times out -> class A, first alert (isolated store)
    clsA = _cron_failure_alert_class("script timed out after 30s")
    assert clsA == "script_timed_out"
    # NOTE: the documented cross-process seam is use_cron_store().
    with cjobs.use_cron_store(tmp_path):
        cjobs.save_jobs([{
            "id": "job-x", "name": "x", "schedule": "5m",
            "prompt": "do", "enabled": True, "runner": "agent",
        }])
        first = cjobs.mark_failure_alerted("job-x", clsA)
        assert first is False  # <-- must deliver

        # t1: same class again -> silent
        again = cjobs.mark_failure_alerted("job-x", clsA)
        assert again is True  # <-- silent

        # t2: the SAME outage, but the error text now reads provider timeout
        # (a transient provider blip on the same broken script via a different
        # runner path) -> class CHANGES -> re-alert fires.
        clsB = _cron_failure_alert_class("provider timed out")
        assert clsB not in (None, clsA)  # a second DISTINCT class
        flip = cjobs.mark_failure_alerted("job-x", clsB)
        assert flip is False  # <-- a second alert for the SAME failure episode

        # t3: flips back
        flipback = cjobs.mark_failure_alerted("job-x", clsA)
        assert flipback is False  # <-- third alert

    # Conclusion: 3 alerts for one continuous failure episode.  The dedup is
    # per-class, not per-incident — a flapping error string re-alerts each
    # time the class toggles.


# --------------------------------------------------------------------------
# P4: sanitize_delivery_text strips reasoning/interim but passes C0 control
# characters (NUL, 0x01, 0x07) through into the message text that is staged
# to OWUI and sent to Telegram (both of which reject NUL).
# --------------------------------------------------------------------------
def test_p4_control_chars_pass_through_sanitizer():
    from hermes_cli.delivery_policy import sanitize_delivery_text
    evil = "итог\u0000done\u0001\n\u0007"
    cleaned = sanitize_delivery_text(evil)
    assert "\u0000" in cleaned  # NOT stripped
    assert "\u0001" in cleaned
    assert "\u0007" in cleaned


# --------------------------------------------------------------------------
# P5: F3 — PWA subscription without owui_user_id and without the server
# marker stays OWUI-only (fail-closed).  Sanity that the seam is closed.
# --------------------------------------------------------------------------
def test_p5_pwa_sub_without_owner_is_owui_only(board_env):
    from gateway.kanban_watchers import _sub_owui_owner_id
    tid = _mk(board_env, "pwa")
    kb.add_notify_sub(board_env, task_id=tid, platform="api_server",
                      chat_id="pwa-123", user_id="", delivery_mode="notify+wake")
    sub = kb.list_notify_subs(board_env, task_id=tid)[0]
    assert _sub_owui_owner_id(sub) == ""


# --------------------------------------------------------------------------
# P6: F3 — the verified-origin seam sets the server marker; a subscription
# created by an unverified client path must not pick up a foreign owner.
# Sanity: set_notify_sub_owui_owner fail-closes on empty.
# --------------------------------------------------------------------------
def test_p6_set_owui_owner_fail_closed_on_empty(board_env):
    tid = _mk(board_env, "root")
    kb.add_notify_sub(board_env, task_id=tid, platform="api_server",
                      chat_id="owui-1", user_id="sess", delivery_mode="notify+wake")
    # Server marks a verified owner.
    conn = kb.connect(board="default")
    kb.set_notify_sub_owui_owner(conn, task_id=tid, platform="api_server",
                                 chat_id="owui-1", owui_user_id="owui-42")
    conn.close()
    sub = kb.list_notify_subs(board_env, task_id=tid)[0]
    assert sub["delivery_metadata"]["owui_user_id_attr"] == "owui-42"
    # Empty clears (fail-closed revert to OWUI-only).
    conn2 = kb.connect(board="default")
    kb.set_notify_sub_owui_owner(conn2, task_id=tid, platform="api_server",
                                 chat_id="owui-1", owui_user_id="")
    conn2.close()
    sub2 = kb.list_notify_subs(board_env, task_id=tid)[0]
    assert "owui_user_id_attr" not in (sub2.get("delivery_metadata") or {})
