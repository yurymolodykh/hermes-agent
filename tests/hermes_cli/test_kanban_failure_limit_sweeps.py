"""``kanban.failure_limit`` must reach the TIMEOUT and CRASH sweeps, not only spawn.

Acceptance defect Д-1 of the auto-continue review: ``enforce_max_runtime`` and
``_account_crashes`` called ``_record_task_failure`` with no ``failure_limit``, so
the ``DEFAULT_FAILURE_LIMIT`` (2) was booked whatever the operator configured. A
board with ``kanban.failure_limit: 3`` therefore ate one retry in silence and
pushed the card into a split / block a whole attempt early.

The journal proved it: ``auto_continued`` events carried
``effective_limit=2, limit_source=dispatcher`` while the config said 3.

Both sweeps are exercised through their public entry points, and the timeout
case runs through ``dispatch_once`` so the whole chain
(``_run_reclaim_phase`` -> sweep -> ``_record_task_failure``) is covered. The
test is RED on the pre-fix code: the second timeout already emits
``auto_continued`` with ``effective_limit=2``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_auto_continue as kac
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

CONFIGURED_LIMIT = 3


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return root


@pytest.fixture
def conn(home: Path):
    connection = kbc.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def auto_continue_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kac, "resolve_settings",
        lambda: kac.AutoContinueSettings(enabled=True, max_splits=2),
    )


@pytest.fixture
def no_live_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never probe or signal a real process: every reclaim path is host-local."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)


def _continued_events(conn, task_id: str) -> list[dict]:
    return [
        e.payload or {} for e in kb.list_events(conn, task_id) if e.kind == "auto_continued"
    ]


def _new_card(conn) -> str:
    tid = kb.create_task(conn, title="overrunning job", assignee="worker", max_runtime_seconds=1)
    conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (f"/tmp/ws/{tid}", tid))
    conn.commit()
    return tid


def _backdate_active_run(conn, task_id: str) -> None:
    """Make the card's CURRENT run look longer than its ``max_runtime_seconds``."""
    old = int(time.time()) - 30
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, task_id))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, task_id),
        )


def test_timeout_sweep_honours_the_configured_failure_limit(
    conn, auto_continue_on, no_live_pids, monkeypatch: pytest.MonkeyPatch,
):
    """Three timeouts are needed to trip when the limit is 3 — not two.

    The worker is pinned "alive" so the crash sweep leaves the card to the
    timeout sweep (that is what distinguishes the two paths), and every signal
    is a stub: nothing here can touch a real process.
    """
    tid = _new_card(conn)
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: True)
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started_at=None: True)
    signals: list[int] = []

    def _signal_fn(pid, sig):
        signals.append(pid)

    def _one_timeout(attempt: int) -> list[str]:
        if attempt > 1:
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            conn.commit()
        claimed = kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':')[0]}:w{attempt}")
        assert claimed is not None, f"attempt {attempt}: the card must be claimable again"
        kbd._set_worker_pid(conn, tid, 700_000 + attempt, run_id=claimed.current_run_id)
        _backdate_active_run(conn, tid)
        return kbd.enforce_max_runtime(
            conn, signal_fn=_signal_fn, failure_limit=CONFIGURED_LIMIT,
        )

    for attempt in (1, 2):
        assert _one_timeout(attempt) == [tid], f"attempt {attempt} must be timed out"
        assert _continued_events(conn, tid) == [], (
            f"{attempt} timeout(s) are below the configured limit of 3; the card must not "
            f"have been continued yet (limit was booked at {kb.DEFAULT_FAILURE_LIMIT}?)"
        )

    assert _one_timeout(3) == [tid]
    assert signals, "the stub signal_fn must have been called, never os.kill"

    events = _continued_events(conn, tid)
    assert len(events) == 1, f"the third timeout must trip exactly once, got {events}"
    assert events[0]["effective_limit"] == CONFIGURED_LIMIT, (
        "kanban.failure_limit=3 must be booked on the timeout path; "
        f"got effective_limit={events[0]['effective_limit']!r} "
        f"source={events[0].get('limit_source')!r}"
    )
    assert events[0]["limit_source"] == "dispatcher"


def test_crash_sweep_honours_the_configured_failure_limit(
    conn, auto_continue_on, no_live_pids,
):
    """Same threshold on the crash path: three dead workers, not two."""
    tid = _new_card(conn)
    conn.execute("UPDATE tasks SET max_runtime_seconds = NULL WHERE id = ?", (tid,))
    conn.commit()

    for attempt in (1, 2, 3):
        if attempt > 1:
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            conn.commit()
        assert kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':')[0]}:w{attempt}")
        kbd._set_worker_pid(conn, tid, 800_000 + attempt)
        crashed = kbd.detect_crashed_workers(conn, failure_limit=CONFIGURED_LIMIT)
        assert tid in crashed, f"attempt {attempt}: a dead worker must be reclaimed"
        if attempt < 3:
            assert _continued_events(conn, tid) == [], (
                f"attempt {attempt} is below the configured limit of 3 — the crash path "
                "must not trip yet"
            )

    events = _continued_events(conn, tid)
    assert len(events) == 1, f"the third crash must trip exactly once, got {events}"
    assert events[0]["effective_limit"] == CONFIGURED_LIMIT, (
        "kanban.failure_limit=3 must be booked on the crash path; "
        f"got effective_limit={events[0]['effective_limit']!r}"
    )
