"""Contention for the per-profile checkpoint store lock (tools/checkpoint_pruning.store_lock).

Why this file exists
--------------------
A profile has ONE ``.<profile>/checkpoints`` store and one ``.checkpoints.lock`` beside it, so
every card that profile runs contends for the same lock. ``store_lock`` used to take it with
``wait=False``: the loser raised ``PruneError: checkpoint store is busy`` in ~0.03 s and the run
continued with no rollback point — 108 such card notices were counted on a live board
(22-24.11.2026), reproduced here as half of all concurrent takes being lost.

Three properties are asserted, each with real processes and the real filesystem:

* **A second taker waits and enters.** A holder that keeps the lock for longer than one
  snapshot takes is joined, not refused.
* **Two concurrent snapshots of one profile both land.** This is the live scenario (two cards of
  one profile); it fails on the old ``wait=False`` code.
* **A real timeout still refuses — and names the holder.** The message carries the holder's pid
  and cannot degrade into a bare "store is busy".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.checkpoint_pruning import (
    PruneError,
    store_lock,
    store_lock_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _spawn(code: str, *args: str) -> subprocess.Popen:
    """Run a snippet as a real second process with this checkout importable."""
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
    )


_HOLD_CODE = """
import sys, time
from pathlib import Path
from tools.checkpoint_pruning import store_lock

base, mark, hold_seconds = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
with store_lock(base, timeout=60):
    mark.touch()                       # the parent waits for this: the lock is definitively held
    time.sleep(hold_seconds)
print("released", flush=True)
"""

_RACE_CODE = """
import sys, json, time
from pathlib import Path
import tools.checkpoint_manager as C

base, project, go, tag = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
C.CHECKPOINT_BASE = base
mgr = C.CheckpointManager(enabled=True, max_snapshots=5, max_total_size_mb=0)
(project / "ready").touch()            # the parent releases both children together
while not go.exists():
    time.sleep(0.001)
(project / (tag + ".txt")).write_text(tag, encoding="utf-8")
ok = mgr.ensure_checkpoint(str(project), "race " + tag)
print(json.dumps({"ok": ok, "failure": mgr._last_failure}), flush=True)
"""


@pytest.fixture()
def base(tmp_path):
    return tmp_path / "checkpoints"


def _holder_at_least(base: Path, mark: Path, hold_seconds: float) -> subprocess.Popen:
    holder = _spawn(_HOLD_CODE, str(base), str(mark), str(hold_seconds))
    deadline = time.monotonic() + 60
    while not mark.exists():
        assert holder.poll() is None, holder.stderr.read()
        assert time.monotonic() < deadline, "holder never took the lock"
        time.sleep(0.01)
    return holder


def test_waiter_joins_a_held_store_lock(base, tmp_path):
    """A snapshot arriving while the lock is held waits for it instead of being refused."""
    holder = _holder_at_least(base, tmp_path / "held", 1.5)
    try:
        # The old code raised PruneError here within ~0.03 s.
        started = time.monotonic()
        with store_lock(base, timeout=10):
            waited = time.monotonic() - started
        assert waited > 0.2, f"entered without waiting ({waited:.3f}s) — the holder was not seen"
    finally:
        holder.wait(timeout=60)
    assert holder.returncode == 0, holder.stderr.read()


def test_two_concurrent_snapshots_of_one_profile_both_land(base, tmp_path):
    """Two cards of one profile snapshot at the same moment: neither may lose its checkpoint."""
    from tests.waiting import wait_until

    go = tmp_path / "go"
    projects = [tmp_path / f"proj-{i}" for i in range(2)]
    for project in projects:
        project.mkdir()
    procs = [
        _spawn(_RACE_CODE, str(base), str(project), str(go), f"p{i}")
        for i, project in enumerate(projects)
    ]
    # Both children announce readiness by touching their own file: no clock is involved.
    wait_until(lambda: all((p / "ready").exists() for p in projects), timeout=120)
    go.touch()
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    # A refusal would arrive as a ``store`` failure record; neither take may carry one.
    assert results[0]["failure"] is None, results[0]
    assert results[1]["failure"] is None, results[1]
    assert results[0]["ok"] and results[1]["ok"], results


def test_timeout_still_refuses_and_names_the_holder(base, tmp_path):
    """Past the wait the snapshot is refused — but the message says who held the lock."""
    holder = _holder_at_least(base, tmp_path / "held", 8.0)
    try:
        with pytest.raises(PruneError) as excinfo:
            with store_lock(base, timeout=0.3):
                pass
        message = str(excinfo.value)
        assert "busy" in message
        assert f"pid {holder.pid}" in message, message
        assert "gave up after" in message
        assert "\n" not in message, message
    finally:
        holder.wait(timeout=60)


def test_zero_timeout_refuses_immediately_without_waiting(base, tmp_path):
    """``timeout=0`` keeps the fail-fast shape for callers that must not queue."""
    holder = _holder_at_least(base, tmp_path / "held", 8.0)
    try:
        started = time.monotonic()
        with pytest.raises(PruneError):
            with store_lock(base, timeout=0):
                pass
        assert time.monotonic() - started < 1.0
    finally:
        holder.wait(timeout=60)


def test_store_lock_can_be_taken_again_after_release(base):
    """The wait must not deadlock the sequential path: take, release, take again (default args)."""
    for _ in range(3):
        with store_lock(base):
            assert store_lock_path(base).exists()


def test_holder_hint_is_honest_when_nothing_holds_the_lock(base):
    """No invented pid: an unheld lock reports that no process holds a descriptor."""
    module = sys.modules["tools.checkpoint_pruning"]
    if not hasattr(module, "lock_holder_hint"):
        pytest.skip("base revision has no holder probe")
    lock = store_lock_path(base)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    hint = module.lock_holder_hint(lock)
    assert "pid" not in hint or "unknown" in hint
