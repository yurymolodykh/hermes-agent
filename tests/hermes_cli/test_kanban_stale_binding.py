"""Tests for dead-session binding release / re-bind (t_44017da2).

When a gateway dies (crash, kill -9, host reboot) its worker children die
with it, but the ``task_runs`` rows they held stay ``status='running'`` with
``worker_session_id`` still pointed at the now-dead session. A fresh worker
spawned for that SAME run is then hard-rejected by ``bind_worker_session``'s
strict compare-and-set — ``KANBAN_BINDING: rejected active run/session
binding`` — and dies before doing any work; the card wedges until some other
TTL/crash path requeues it.

Two mechanisms fix this (both conservative — never disturb a live worker):

* ``release_dead_worker_bindings`` — a boot-time sweep wired into every
  ``dispatch_once`` tick that clears ``worker_session_id`` ONLY on runs whose
  bound worker is provably dead (host-local claim + non-alive pid + launch
  grace elapsed). The first tick after a gateway restart IS the boot reset.
* ``bind_worker_session`` — when the strict CAS refuses because the run is
  bound to a DIFFERENT session whose worker is provably dead, the stale
  binding is released and the fresh worker takes the slot (re-bind instead of
  hard reject).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated board copy in a TEMP dir — never the real kanban.db.

    Pins ``HERMES_KANBAN_HOME`` at the temp copy so ``kanban_db_path``
    resolves to ``<tmp>/.hermes/kanban.db`` directly (the task's required
    isolation vector), and keeps the ``HERMES_HOME`` redirect the rest of
    the suite relies on as a second layer. A fail-closed assertion then
    proves the resolved DB path lives under ``tmp_path`` and is not the
    operator's real board ``C:/Users/hermes/AppData/Local/hermes/kanban.db``
    — a regression that silently points the board back at production fails
    this fixture instead of writing to the live DB.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", "")  # derived from KANBAN_HOME
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    resolved = kb.kanban_db_path(board=None).expanduser().resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"test board resolved OUTSIDE the temp copy: {resolved}"
    )
    assert resolved != Path(
        "C:/Users/hermes/AppData/Local/hermes/kanban.db"
    ).resolve(), "test board resolved to the LIVE kanban.db!"
    return home


def _running_card_with_stale_binding(
    conn,
    *,
    session_id: str,
    run_pid: int,
    task_pid=None,
) -> tuple[str, int]:
    """Create+claim a task and leave exactly the residue a DEAD gateway's
    worker would: a ``running`` run whose ``worker_session_id`` points at the
    dead session and whose recorded worker pid is ``run_pid``.

    ``task_pid`` stays NULL by default so ``detect_crashed_workers`` (which
    requires ``tasks.worker_pid IS NOT NULL``) skips the card — isolation:
    only ``release_dead_worker_bindings`` may act on it. The claim is stamped
    with the CURRENT generation and a future ``claim_expires`` so the
    generation- / TTL- / orphan-reclaim passes all leave it alone.

    Returns ``(task_id, run_id)``.
    """
    tid = kb.create_task(conn, title="x", assignee="alice")
    kb.claim_task(conn, tid)
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["current_run_id"]
    # Started well before the (simulated) gateway death — outside any crash
    # grace window, so liveness is judged purely on pid aliveness.
    old_start = int(time.time()) - 3600
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (task_pid, tid),
    )
    conn.execute(
        """
        UPDATE task_runs
           SET worker_session_id = ?, worker_pid = ?, started_at = ?
         WHERE id = ?
        """,
        (session_id, run_pid, old_start, run_id),
    )
    return tid, int(run_id)


def _session(conn, run_id):
    return conn.execute(
        "SELECT worker_session_id FROM task_runs WHERE id = ?", (run_id,),
    ).fetchone()["worker_session_id"]


# ---------------------------------------------------------------------------
# Boot-time sweep
# ---------------------------------------------------------------------------


def test_dispatch_boot_releases_dead_session_binding(
    kanban_home, monkeypatch,
):
    """A run left bound to a dead gateway session is un-wedged on the first
    dispatch tick: the stale ``worker_session_id`` is cleared, and a fresh
    dispatcher-spawned worker can then bind WITHOUT the "rejected active
    run/session binding" rejection."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)  # 424242 dead
    with kb.connect() as conn:
        tid, run_id = _running_card_with_stale_binding(
            conn, session_id="dead-session-1", run_pid=424242,
        )
        byte_assert = _session(conn, run_id)
        assert byte_assert == "dead-session-1"

        # Gateway boot: first dispatch tick.
        res = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None, max_spawn=0,
        )

        # The sweep released the dead binding …
        assert tid in res.bindings_released
        assert _session(conn, run_id) is None
        # … and the card is still running (the sweep does NOT requeue it).
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()["status"] == "running"

        # A dispatcher-spawned worker for that same run now binds fine.
        assert kb.bind_worker_session(
            conn, tid, run_id, "alice", "fresh-session-2",
        ) is True
        assert kb.has_authenticated_worker_binding(
            conn, tid, run_id, "alice", "fresh-session-2",
        )


def test_live_worker_binding_never_released(kanban_home, monkeypatch):
    """Negative: a run bound to a LIVE worker session is never released by the
    boot sweep — the binding stays put and a different session is still
    rejected (no stealing from a live worker)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)  # pid alive
    with kb.connect() as conn:
        tid, run_id = _running_card_with_stale_binding(
            conn, session_id="live-session-1", run_pid=os.getpid(),
        )
        res = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None, max_spawn=0,
        )

        assert tid not in res.bindings_released
        assert _session(conn, run_id) == "live-session-1"
        # A different (e.g. duplicate-spawn) session must still be rejected.
        assert kb.bind_worker_session(
            conn, tid, run_id, "alice", "other-session",
        ) is False


def test_foreign_claim_binding_never_released(kanban_home, monkeypatch):
    """A run claimed by a FOREIGN host is never judged on this host — its
    worker may be alive over there; clearing its binding here would be wrong."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid, run_id = _running_card_with_stale_binding(
            conn, session_id="foreign-session", run_pid=424242,
        )
        conn.execute(
            "UPDATE task_runs SET claim_lock = 'otherhost:99' WHERE id = ?",
            (run_id,),
        )
        res = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None, max_spawn=0,
        )

        assert tid not in res.bindings_released
        assert _session(conn, run_id) == "foreign-session"


def test_no_pid_no_release(kanban_home, monkeypatch):
    """A bound run with NO recorded worker pid cannot be positively proven dead
    — leave it alone rather than risk clearing a live worker's binding."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid, run_id = _running_card_with_stale_binding(
            conn, session_id="pidless-session", run_pid=None,
        )
        res = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None, max_spawn=0,
        )

        assert tid not in res.bindings_released
        assert _session(conn, run_id) == "pidless-session"


# ---------------------------------------------------------------------------
# Re-bind on attach (bind_worker_session takeover)
# ---------------------------------------------------------------------------


def test_bind_takes_over_dead_binding(kanban_home, monkeypatch):
    """Even WITHOUT a prior boot sweep, a fresh worker binding to a run whose
    existing session is provably dead takes the slot instead of rejecting."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid, run_id = _running_card_with_stale_binding(
            conn, session_id="dead-session", run_pid=424242,
        )
        assert kb.bind_worker_session(
            conn, tid, run_id, "alice", "fresh-session",
        ) is True
        assert _session(conn, run_id) == "fresh-session"


def test_bind_rejects_live_different_session(kanban_home, monkeypatch):
    """Negative: a live worker's binding is never stolen — a different session
    is still hard-rejected by the strict CAS."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    with kb.connect() as conn:
        tid, run_id = _running_card_with_stale_binding(
            conn, session_id="live-session", run_pid=os.getpid(),
        )
        assert kb.bind_worker_session(
            conn, tid, run_id, "alice", "intruder-session",
        ) is False
        assert _session(conn, run_id) == "live-session"


def test_bind_idempotent_same_session(kanban_home):
    """Binding the SAME session twice (worker restart re-attach) still succeeds
    — the CAS treats it as a no-op success."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
        ).fetchone()["current_run_id"]
        assert kb.bind_worker_session(
            conn, tid, run_id, "alice", "my-session",
        ) is True
        assert kb.bind_worker_session(
            conn, tid, run_id, "alice", "my-session",
        ) is True
        assert _session(conn, run_id) == "my-session"
