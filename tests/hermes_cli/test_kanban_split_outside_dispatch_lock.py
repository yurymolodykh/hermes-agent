"""The split phase must not hold the dispatcher's single-writer lock.

Acceptance defect Д-2 of the auto-continue review: ``run_split_phase`` ran inside
``_dispatch_once_locked``, i.e. under ``<db>.dispatch.lock``, while its decomposer
is an aux LLM request of up to 180 s × 3 attempts. Measured on the reviewer's own
two-process probe: the second dispatcher got ``skipped_locked=True`` and the tick
held the lock 6.4 s on a stub; a live journal showed ``auto_split_queued`` ->
``auto_split`` 616.8 s apart with no ``spawned``/``promoted``/``claimed`` between.

Pinned here:

1. while one process is inside a SLOW decomposer, a second process's tick still
   reclaims/promotes/spawns — it must not report ``skipped_locked``;
2. the split still happens exactly once per tick (one ``run_split_phase`` call,
   one ``auto_split`` event) — no double decomposition from the new ordering.

The first test is a real two-process measurement: a forked child takes
``.split.lock`` exactly as a sibling dispatcher's split phase would and sleeps in
its "decomposer"; the parent then runs a full tick on the same board and must
spawn, not skip. The second process is not simulated by a monkeypatch — the lock
it holds is the same ``flock`` the production path takes.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

SLOW_DECOMPOSE_SECONDS = 6.0


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    return root


@pytest.fixture
def conn(home: Path):
    connection = kbc.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def any_assignee_is_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the LOCK, not profile routing: accept any assignee."""
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: True))


def _board_card(conn) -> str:
    """A ready card the tick can actually spawn, so "the board keeps working" is visible."""
    tid = kb.create_task(conn, title="waiting work", assignee="w")
    conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (f"/tmp/ws/{tid}", tid))
    conn.commit()
    return tid


def _child_runs_a_slow_split(db_path: str, announce_fd: int, done_fd: int) -> None:
    """Child process: one PRODUCTION tick whose split phase is a slow decomposer.

    On the pre-fix code the split phase runs inside ``_dispatch_once_locked``, so
    this child holds ``<db>.dispatch.lock`` for the whole decomposer — which is
    exactly the freeze the review measured. On the fixed code it holds only
    ``<db>.split.lock``, so the board stays dispatchable.
    """
    connection = kbc.connect()

    def _slow_split(conn_, **_kwargs):
        os.write(announce_fd, b"S")          # "I am inside the decomposer now"
        # The delay IS the behaviour under test (a slow decomposer); waiting on a
        # condition instead would remove the very stall being measured.
        time.sleep(SLOW_DECOMPOSE_SECONDS)   # health: allow HX013 -- this sleep IS the stall the test measures
        return [], []

    kbd._kac.run_split_phase = _slow_split
    os.write(announce_fd, b"R")              # ready to tick
    kbd.dispatch_once(connection, spawn_fn=lambda *a, **k: None)
    os.write(done_fd, b"D")
    connection.close()
    os._exit(0)


def test_a_slow_decomposer_does_not_freeze_the_board(conn, home):
    """While ANOTHER PROCESS is inside a slow split, this tick still spawns."""
    db_path = str(kb.kanban_db_path(board="default"))
    announce_r, announce_w = os.pipe()
    done_r, done_w = os.pipe()
    spawned: list[str] = []

    child = os.fork()
    if child == 0:  # pragma: no cover - child process
        os.close(announce_r)
        os.close(done_r)
        _child_runs_a_slow_split(db_path, announce_w, done_w)

    os.close(announce_w)
    os.close(done_w)
    try:
        assert os.read(announce_r, 1) == b"R", "the child could not start its tick"
        assert os.read(announce_r, 1) == b"S", "the child never reached its decomposer"

        # The child is now blocked in the decomposer. Give the board real work and
        # tick from this process: on the pre-fix code this returns skipped_locked.
        tid = _board_card(conn)
        started = time.monotonic()
        result = kbd.dispatch_once(
            conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id),
        )
        elapsed = time.monotonic() - started

        assert result.skipped_locked is False, (
            "another process is SPLITTING, not dispatching: this tick must not be locked "
            "out of the board for the whole decomposer"
        )
        assert tid in spawned, "the tick must still spawn ready work during a split"
        assert elapsed < SLOW_DECOMPOSE_SECONDS / 2, (
            f"the tick blocked {elapsed:.1f}s behind the sibling decomposer"
        )
    finally:
        os.close(announce_r)
        os.close(done_r)
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)


def test_split_runs_once_per_tick_even_without_the_dispatch_lock(conn, monkeypatch):
    """Exactly one ``run_split_phase`` per tick, and one ``auto_split`` event."""
    calls: list[int] = []

    def _fake_split(conn_, **_kwargs):
        calls.append(1)
        return [], []

    monkeypatch.setattr(kbd._kac, "run_split_phase", _fake_split)

    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)

    assert len(calls) == 2, f"one split phase per tick: expected 2 calls, got {len(calls)}"


def test_dry_run_never_splits(conn, monkeypatch):
    """A dry run must not create child cards, so the phase is skipped entirely."""
    calls: list[int] = []
    monkeypatch.setattr(
        kbd._kac, "run_split_phase", lambda conn_, **kwargs: (calls.append(1), ([], []))[1],
    )

    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)

    assert calls == []
    assert result.auto_split == []


def test_the_split_lock_is_separate_from_the_dispatch_lock(home):
    """Two different files: holding one must never imply holding the other."""
    db_path = kb.kanban_db_path(board="default")
    with kbc._dispatch_tick_lock(db_path) as dispatch_held:
        assert dispatch_held is True
        with kbc._dispatch_split_lock(db_path) as split_held:
            assert split_held is True, (
                "the split lock must be independent of the dispatch lock — otherwise the "
                "decomposer would still freeze the board"
            )
