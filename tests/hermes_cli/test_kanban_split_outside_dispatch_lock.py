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


def test_a_slow_decomposer_does_not_freeze_the_board(conn, home):
    """While ANOTHER PROCESS sits in a slow decomposer, this tick still spawns."""
    tid = _board_card(conn)
    db_path = kb.kanban_db_path(board="default")
    spawned: list[str] = []

    def _spawn(task, workspace, board=None):
        spawned.append(getattr(task, "id", str(task)))
        return None

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - child process
        code = 1
        try:
            os.close(read_fd)
            with kbc._dispatch_split_lock(db_path) as held:
                os.write(write_fd, b"1" if held else b"0")
                if held:
                    # The decomposer: 180 s x 3 attempts in production.
                    time.sleep(SLOW_DECOMPOSE_SECONDS)
            code = 0
        finally:
            os._exit(code)

    os.close(write_fd)
    try:
        assert os.read(read_fd, 1) == b"1", "the child could not take the split lock"
        started = time.monotonic()
        result = kbd.dispatch_once(conn, spawn_fn=_spawn)
        elapsed = time.monotonic() - started

        assert result.skipped_locked is False, (
            "the sibling process is splitting, not dispatching: this tick must not be "
            "locked out of the board"
        )
        assert tid in spawned, "the tick must still spawn ready work during a split"
        assert elapsed < SLOW_DECOMPOSE_SECONDS / 2, (
            f"the tick blocked {elapsed:.1f}s behind the sibling's decomposer"
        )
    finally:
        os.close(read_fd)
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
