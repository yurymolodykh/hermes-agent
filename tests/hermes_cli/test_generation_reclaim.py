"""Tests for generation-scoped kanban claims (t_ae461ad6).

When a gateway process dies (crash, kill -9, host reboot), its worker
children die with it but the cards they claimed stay in ``status='running'``
until ``release_stale_claims``' TTL (~4h, DEFAULT_CLAIM_TTL_SECONDS) expires —
their heartbeats simply stop and nothing notices.

The gateway-generation marker fixes this: every claim is stamped with the
claiming process's generation (pid + process start time + boot id), and a
dispatch tick immediately requeues ``running`` cards whose stamp differs from
the CURRENT generation AND whose worker PID is no longer alive. Cards whose
worker is still alive are never touched (a duplicate spawn would be wrong).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (mirrors test_kanban_db).

    ``HERMES_HOME`` is pointed at a tmp dir so the generation marker file
    (``<root>/kanban/.gateway_generation``) is written there, never at the
    real install.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_card(
    conn,
    generation,
    pid,
    worker_start=None,
):
    """Create+claim a task, then force its claim to look like it belongs to
    ``generation`` (the DISPATCHER stamp) with worker pid ``pid`` and (F3)
    the worker's own recorded create-time ``worker_start`` — as a previous
    gateway would have left it.
    """
    tid = kb.create_task(conn, title="x", assignee="alice")
    kb.claim_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET gateway_generation = ?, worker_pid = ?, "
        "worker_start = ? WHERE id = ?",
        (generation, pid, worker_start, tid),
    )
    return tid


def _status(conn, tid):
    return conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["status"]


# ---------------------------------------------------------------------------
# Marker file plumbing
# ---------------------------------------------------------------------------


def test_marker_file_written_and_readable(kanban_home):
    """``write_gateway_generation`` persists the process stamp and the reader
    round-trips it; the stamp carries pid + start + boot segments."""
    kb.write_gateway_generation()
    path = kb.gateway_generation_marker_path()
    assert path.exists()
    stamp = path.read_text(encoding="utf-8").strip()
    assert stamp == kb.current_gateway_generation()
    assert kb.read_gateway_generation() == stamp
    assert "pid:" in stamp
    assert "start:" in stamp
    assert "boot:" in stamp


def test_read_gateway_generation_absent_returns_none(kanban_home):
    assert kb.read_gateway_generation() is None


# ---------------------------------------------------------------------------
# Claim stamping
# ---------------------------------------------------------------------------


def test_claim_stamps_current_generation(kanban_home):
    """A normal claim must be stamped with the claiming process's generation,
    so a later restart can tell this card belongs to the old boot."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT gateway_generation FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["gateway_generation"] == kb.current_gateway_generation()


# ---------------------------------------------------------------------------
# Generation reclaim
# ---------------------------------------------------------------------------


def test_dead_generation_card_reclaimed_ready_on_first_tick(
    kanban_home, monkeypatch,
):
    """A running card stamped by a previous (dead) generation whose worker pid
    is not alive must be requeued to ``ready`` on the FIRST dispatch tick —
    immediately, not after the release_stale_claims TTL."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid = _running_card(conn, "pid:1:start:1:boot:old", pid=424242)
        kb.write_gateway_generation()  # current generation differs from card's
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid in res.generation_reclaimed
        assert _status(conn, tid) == "ready"
        row = conn.execute(
            "SELECT worker_pid, claim_lock, claim_expires FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["worker_pid"] is None
        assert row["claim_lock"] is None
        assert row["claim_expires"] is None


def test_live_pid_foreign_generation_card_untouched(
    kanban_home, monkeypatch,
):
    """Even a card claimed by a previous generation must NOT be reclaimed while
    its worker PID is still alive — the old gateway may be gone but its worker
    is doing real work, and spawning a duplicate would be wrong."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    with kb.connect() as conn:
        tid = _running_card(conn, "pid:1:start:1:boot:old", pid=123)
        kb.write_gateway_generation()
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid not in res.generation_reclaimed
        assert _status(conn, tid) == "running"


def test_current_generation_card_untouched(kanban_home, monkeypatch):
    """A card stamped with the CURRENT generation is not foreign — it is never
    swept by the generation path (the normal TTL/heartbeat paths own it)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid = _running_card(
            conn, kb.current_gateway_generation(), pid=424242,
        )
        kb.write_gateway_generation()
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid not in res.generation_reclaimed
        assert _status(conn, tid) == "running"


def test_null_generation_card_untouched(kanban_home, monkeypatch):
    """Legacy / non-gateway claims (NULL gateway_generation) are outside the
    generation scope — they stay on the normal TTL/crash recovery paths."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        kb.claim_task(conn, tid)
        conn.execute(
            "UPDATE tasks SET gateway_generation = NULL, worker_pid = 999 "
            "WHERE id = ?",
            (tid,),
        )
        kb.write_gateway_generation()
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid not in res.generation_reclaimed
        assert _status(conn, tid) == "running"


def test_dead_generation_reclaim_records_diagnostic_event(
    kanban_home, monkeypatch,
):
    """The direct reclaim helper must close the run, requeue to ready, and emit
    a ``reclaimed`` event carrying the reason + both generations + worker pid
    so operators can diagnose why a card was swept."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid = _running_card(conn, "gen-old", pid=424242)
        kb.write_gateway_generation()
        reclaimed = kb.reclaim_dead_generation_running(conn)

        assert reclaimed == [tid]
        assert _status(conn, tid) == "ready"
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (tid,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["reason"] == "dead_gateway_generation"
        assert payload["gateway_generation"] == "gen-old"
        assert payload["worker_pid"] == 424242
        # The current generation recorded in the event matches the marker.
        assert payload["current_generation"] == kb.read_gateway_generation()


def test_dead_generation_card_reclaimed_without_marker(kanban_home, monkeypatch):
    """No marker file present → the sweep falls back to THIS process's
    generation as the 'current' one, so a foreign-stamped card is still
    reclaimed (covers non-gateway dispatchers that never write a marker)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid = _running_card(conn, "pid:1:start:1:boot:old", pid=424242)
        assert kb.read_gateway_generation() is None
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid in res.generation_reclaimed
        assert _status(conn, tid) == "ready"


def test_dead_generation_reclaim_does_not_touch_cards_after_reclaim(
    kanban_home, monkeypatch,
):
    """Reclaiming is idempotent: a second tick must not double-touch anything
    (reclaimed cards are no longer running), and the result is stable."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid = _running_card(conn, "pid:1:start:1:boot:old", pid=424242)
        kb.write_gateway_generation()
        res1 = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)
        assert tid in res1.generation_reclaimed
        res2 = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)
        assert tid not in res2.generation_reclaimed
        assert _status(conn, tid) == "ready"


# ---------------------------------------------------------------------------
# PID-reuse scenario (F3 follow-up on fast-gateway-restart)
# ---------------------------------------------------------------------------


def test_pid_reuse_foreign_generation_reclaimed(kanban_home, monkeypatch):
    """When the OS reuses the dead worker's PID for a NEW process (a fresh shim
    or any unrelated process), ``_pid_alive(pid)`` alone would keep the stale
    card ``running`` forever despite its generation being dead.  The reclaim
    must compare the LIVE create-time of that PID against the worker's OWN
    recorded create-time (``worker_start``): mismatch ⇒ the pid was reused by a
    new process ⇒ requeue.

    Note the semantic split that the original draft got wrong: ``worker_start``
    is recorded per-spawn from the WORKER process, while ``gateway_generation``
    identifies the DISPATCHER (a different process).  A worker's create time
    must never be compared against the stamp's ``start:`` segment.
    """
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)  # PID exists (reused)
    monkeypatch.setattr(kb, "_pid_create_time_value", lambda pid: 999_000.0)
    with kb.connect() as conn:
        tid = _running_card(
            conn,
            "pid:4242:start:111_000:boot:old-boot",  # dispatcher stamp (foreign)
            pid=4242,
            worker_start=111_000.0,  # worker's own start; live 999000 differs
        )
        kb.write_gateway_generation()
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid in res.generation_reclaimed
        assert _status(conn, tid) == "ready"


def test_pid_reuse_same_worker_start_untouched(kanban_home, monkeypatch):
    """A foreign-generation card whose worker's recorded create-time STILL
    matches the live PID (the old worker really is still running) must NOT be
    reclaimed — the generation-number difference is purely a marker refresh
    artifact and the worker is doing real work."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        kb, "_pid_create_time_value", lambda pid: 111_000.0, raising=False
    )
    with kb.connect() as conn:
        tid = _running_card(
            conn,
            "pid:4242:start:111_000:boot:old-boot",
            pid=4242,
            worker_start=111_000.0,
        )
        kb.write_gateway_generation()
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid not in res.generation_reclaimed
        assert _status(conn, tid) == "running"


def test_pid_reuse_unknown_worker_start_untouched(kanban_home, monkeypatch):
    """Legacy / unreadable ``worker_start`` (NULL) is CONSERVATIVE: a live pid
    with no recorded start is never reclaimed — false-keeping is always safer
    than spawning a duplicate beside real work."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        kb, "_pid_create_time_value", lambda pid: 999_000.0, raising=False
    )
    with kb.connect() as conn:
        tid = _running_card(
            conn,
            "pid:4242:start:111_000:boot:old-boot",
            pid=4242,
            worker_start=None,
        )
        kb.write_gateway_generation()
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)

        assert tid not in res.generation_reclaimed
        assert _status(conn, tid) == "running"


def test_pid_reuse_guard_helper(kanban_home, monkeypatch):
    """The F3 guard ``pid_looks_reused``: a ((pid, recorded_start), live_start)
    mismatch means reuse; anything unprovable returns False (not reused)."""
    monkeypatch.setattr(kb, "_pid_create_time_value", lambda pid: 200.0)
    assert kb.pid_looks_reused(9, 200.0) is False   # same process
    assert kb.pid_looks_reused(9, 200.01) is False  # within 10 ms tolerance
    assert kb.pid_looks_reused(9, 999.0) is True    # reused (seconds apart)
    assert kb.pid_looks_reused(None, 200.0) is False
    assert kb.pid_looks_reused(9, None) is False    # legacy: not provable
    monkeypatch.setattr(kb, "_pid_create_time_value", lambda pid: None)
    assert kb.pid_looks_reused(9, 200.0) is False   # live unreadable: not provable
