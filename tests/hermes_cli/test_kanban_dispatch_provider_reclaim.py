"""Acceptance tests for kanban dispatch hardening (parent t_e1b78631).

Covers the two dispatch symptoms this task fixes:

Symptom 1 — a card pinned to an unresolvable provider route (e.g. the bare
``custom`` billing class written by a route plugin with no endpoint/key of
its own) used to be spawned blindly and die instantly with ``"No API key
found for provider 'custom'"``. Now ``_provider_route_blocker`` validates
the pinned route BEFORE spawn and blocks the task deterministically with a
visible ``capability`` error, instead of crash-looping a doomed worker.

Symptom 2 — a reclaim believed a PID was still "alive" purely because the
process object/table entry lingered, so termination "failed" and the task
got deferred forever, hanging in ``running`` well past its TTL. Now
``_terminate_reclaimed_worker`` treats a gone PID (``ProcessLookupError``)
as successfully terminated, ``_pid_alive`` detects dead/zombie processes,
and the bounded-reclaim cap crash-reclaims a no-heartbeat worker that
survives ``MAX_RECLAIM_DEFER_CYCLES`` attempts instead of deferring forever.
"""
from __future__ import annotations

import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import profiles


pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _host_claim(claimer: str) -> str:
    return f"{kb._claimer_id().split(':', 1)[0]}:{claimer}"


# ---------------------------------------------------------------------------
# Symptom 1 — provider pre-flight blocks unresolvable routes before spawn
# ---------------------------------------------------------------------------


def test_bare_custom_provider_pinned_task_is_blocked_before_spawn(
    kanban_home, monkeypatch,
):
    """A card pinned to the bare ``custom`` class (no named endpoint/key) must
    be blocked with a visible capability error, never spawned."""
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda _name: True)
    spawn_calls = []

    def spy_spawn(task, workspace, board=None, **_kwargs):
        spawn_calls.append(task)
        return 4242

    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        # Legacy rows can predate create-time route validation; dispatch must
        # still block them rather than spawning an unusable worker.
        with monkeypatch.context() as create_patch:
            create_patch.setattr(kb, "_provider_route_blocker", lambda _p, _m: None)
            tid = kb.create_task(
                conn, title="doomed custom", assignee="default",
                model_override="deepseek-v4-flash", provider_override="custom",
            )
        res = kb.dispatch_once(conn, spawn_fn=spy_spawn, dry_run=False)

    assert tid in res.auto_blocked
    assert tid not in res.spawned
    assert spawn_calls == []

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        blocked = next(
            e for e in kb.list_events(conn, tid) if e.kind == "blocked"
        )
        reason = blocked.payload["reason"]
        assert reason.startswith("provider pre-flight blocked before spawn")
        assert "does not name a configured provider entry" in reason
        assert "bare 'custom'" in reason
        assert blocked.payload["kind"] == "capability"


def test_unresolvable_named_provider_pinned_task_is_blocked(
    kanban_home, monkeypatch,
):
    """A pin to a provider that does not resolve on this host is blocked."""
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda _name: True)
    spawn_calls = []

    def spy_spawn(task, workspace, board=None, **_kwargs):
        spawn_calls.append(task)
        return 4242

    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        with monkeypatch.context() as create_patch:
            create_patch.setattr(kb, "_provider_route_blocker", lambda _p, _m: None)
            tid = kb.create_task(
                conn, title="missing prov", assignee="default",
                model_override="some-model", provider_override="definitely-missing-prov",
            )
        res = kb.dispatch_once(conn, spawn_fn=spy_spawn, dry_run=False)

    assert tid in res.auto_blocked
    assert spawn_calls == []
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        blocked = next(
            e for e in kb.list_events(conn, tid) if e.kind == "blocked"
        )
        assert blocked.payload["kind"] == "capability"


def test_provider_blocker_skipped_when_no_pinned_route(kanban_home):
    """No pinned provider/model -> profile default route, not provider-blocked.

    ``_provider_route_blocker`` is the spawn-time pre-flight guard: with no
    override pair there is no pinned route to validate, so it must return
    ``None`` (allow the profile default). (A separate MODEL_LOCK root-route
    concern may still block a task that resolves to no route — that is out of
    scope for this pre-flight guard.)
    """
    assert kb._provider_route_blocker(None, None) is None
    assert kb._provider_route_blocker("", "") is None
    assert kb._provider_route_blocker("custom", None) is None


def test_provider_blocker_not_invoked_in_dry_run(kanban_home, monkeypatch):
    """dry_run must not invoke the spawn-time pre-flight provider blocker."""
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda _name: True)
    calls = []
    orig = kb._provider_route_blocker

    def spy(provider, model):
        calls.append((provider, model))
        return orig(provider, model)

    monkeypatch.setattr(kb, "_provider_route_blocker", spy)
    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        with monkeypatch.context() as create_patch:
            create_patch.setattr(kb, "_provider_route_blocker", lambda _p, _m: None)
            kb.create_task(
                conn, title="dry", assignee="default",
                model_override="m", provider_override="custom",
            )
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 5, dry_run=True,
                         max_in_progress=10)
    assert calls == []


# ---------------------------------------------------------------------------
# Symptom 2 — reclaim no longer hangs on a false-alive worker
# ---------------------------------------------------------------------------


def test_dead_pid_worker_is_reclaimed_not_deferred_forever(
    kanban_home, monkeypatch,
):
    """A stale claim whose PID is dead is reclaimed to retry, not deferred."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        tid = kb.create_task(conn, title="dead pid", assignee="a")
        kb.claim_task(conn, tid, claimer=_host_claim("w"))
        kb._set_worker_pid(conn, tid, 999999)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, tid),
        )
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert not any(
            e.kind == "reclaim_deferred" for e in kb.list_events(conn, tid)
        )
        assert any(
            e.kind == "reclaimed" for e in kb.list_events(conn, tid)
        )


def test_process_lookup_error_deems_worker_terminated(monkeypatch):
    """A PID that is already gone (ProcessLookupError on kill) is treated as
    successfully terminated, so reclaim proceeds instead of deferring."""
    host = kb._claimer_id().split(":", 1)[0]
    term = kb._terminate_reclaimed_worker(
        123456, f"{host}:w",
        signal_fn=lambda _p, _s: (_ for _ in ()).throw(
            ProcessLookupError("gone")
        ),
    )
    assert term["termination_attempted"] is True
    assert term["host_local"] is True
    assert term["terminated"] is True
    # And the reclaim-safety predicate sees no surviving worker.
    assert not kb._worker_survived_termination(term)


def test_nonlocal_or_noop_termination_is_not_survival(monkeypatch):
    """A non-local claim lock or a no-op kill attempt is NOT treated as a
    worker-survival, so reclaim takes the normal release path."""
    # Non-local claim lock -> host_local False -> not a survival
    t = kb._terminate_reclaimed_worker(1, "otherhost:worker",
                                       signal_fn=lambda _p, _s: None)
    assert not kb._worker_survived_termination(t)


def test_heartbeating_worker_is_deferred_not_capped(kanban_home, monkeypatch):
    """A worker that HAS heartbeated and survives termination is deferred
    (keep the claim, avoid duplicate spawn) and never crash-reclaimed."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        tid = kb.create_task(conn, title="hb", assignee="a")
        kb.claim_task(conn, tid, claimer=_host_claim("w"))
        kb._set_worker_pid(conn, tid, 424242)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 3600, int(time.time()) - 5, tid),
        )
        # Plant enough prior deferrals to hit the cap — but a heartbeat means
        # the worker is verifiably alive, so it must STILL defer.
        for _ in range(kb.MAX_RECLAIM_DEFER_CYCLES):
            kb._append_event(conn, tid, "reclaim_deferred", {"reason": "x"})

        term = {"termination_attempted": True, "host_local": True,
                "terminated": False}
        deferred = kb._defer_reclaim_for_live_worker(
            conn, tid, _host_claim("w"), int(time.time()), term,
            reason="ttl_expired_worker_alive",
        )
        assert deferred is True
        task = kb.get_task(conn, tid)
        assert task.status == "running"


def test_no_heartbeat_survived_worker_crash_reclaimed_after_defer_cap(
    kanban_home, monkeypatch,
):
    """A false-alive worker with NO heartbeat that survives MAX_RECLAIM_DEFER_CYCLES
    termination attempts is crash-reclaimed (bounded safety net) instead of
    deferring forever and hanging in ``running``."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        tid = kb.create_task(conn, title="false-alive", assignee="a")
        kb.claim_task(conn, tid, claimer=_host_claim("w"))
        kb._set_worker_pid(conn, tid, 424244)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, tid),
        )
        # MAX_RECLAIM_DEFER_CYCLES prior deferrals, NO heartbeat -> cap hit.
        for _ in range(kb.MAX_RECLAIM_DEFER_CYCLES):
            kb._append_event(conn, tid, "reclaim_deferred", {"reason": "x"})

        term = {"termination_attempted": True, "host_local": True,
                "terminated": False, "prev_pid": 424244}
        deferred = kb._defer_reclaim_for_live_worker(
            conn, tid, _host_claim("w"), int(time.time()), term,
            reason="ttl_expired_worker_alive",
        )
        assert deferred is False
        committed = kb._reclaim_deferred_worker_as_crash(
            conn, tid, _host_claim("w"), term, reason="ttl_expired_worker_alive"
        )
        assert committed is True
        task = kb.get_task(conn, tid)
        # Not stuck in running — returned to retry phase with a crash record.
        assert task.status == "ready"
        crashed = [e for e in kb.list_events(conn, tid) if e.kind == "crashed"]
        assert crashed
        assert crashed[0].payload.get("bounded_defer_cap") == kb.MAX_RECLAIM_DEFER_CYCLES
        run = next(
            r for r in kb.list_runs(conn, tid) if r.outcome == "crashed"
        )
        assert run.status == "crashed"
        assert "survived" in (run.error or "")
