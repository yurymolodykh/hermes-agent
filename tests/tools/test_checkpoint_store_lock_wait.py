"""A snapshot waits for a sibling snapshot instead of losing every checkpoint in the run.

The store lock used to be grab-or-refuse: a second taker got ``PruneError: checkpoint store is
busy`` after 0 s and the run went on with no rollback point at all (t_312ea8d7, t_977db51b,
t_33063b55). Serialization must survive — two snapshots still may not interleave — so the fix is
a bounded wait, not a weaker lock.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tools.checkpoint_manager as checkpoints
from tools import checkpoint_pruning


def _hold_lock_program() -> str:
    return (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from tools.checkpoint_pruning import store_lock\n"
        "with store_lock(Path(sys.argv[1])):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(float(sys.argv[2]))\n"
    )


@pytest.fixture
def held_store(tmp_path, monkeypatch):
    """Spawn a real second process holding the store lock for ``hold`` seconds."""
    monkeypatch.setattr(checkpoints, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    base = tmp_path / "checkpoints"
    (base / "store").mkdir(parents=True)
    children: list[subprocess.Popen] = []

    def hold(seconds: float) -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", _hold_lock_program(), str(base), str(seconds)],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(Path(checkpoints.__file__).resolve().parents[1])},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        children.append(child)
        assert child.stdout.readline().strip() == "held"
        return child

    yield hold

    for child in children:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=30)


def test_snapshot_waits_out_a_sibling_snapshot_and_succeeds(tmp_path, held_store, monkeypatch):
    monkeypatch.setattr(checkpoint_pruning, "SNAPSHOT_LOCK_TIMEOUT_SECONDS", 30.0)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("one", encoding="utf-8")

    holder = held_store(2.0)
    manager = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
    started = time.monotonic()
    assert manager.ensure_checkpoint(str(project), "through the holder") is True
    waited = time.monotonic() - started
    holder.communicate(timeout=30)

    assert waited >= 1.5, f"the taker must actually have waited for the holder, waited {waited:.2f}s"
    assert [row["reason"] for row in manager.list_checkpoints(str(project))] == ["through the holder"]


def test_snapshot_reports_the_wait_honestly(tmp_path, held_store, monkeypatch, caplog):
    monkeypatch.setattr(checkpoint_pruning, "SNAPSHOT_LOCK_TIMEOUT_SECONDS", 30.0)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("one", encoding="utf-8")

    holder = held_store(1.5)
    manager = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
    with caplog.at_level("INFO", logger="tools.checkpoint_pruning"):
        assert manager.ensure_checkpoint(str(project), "logged wait") is True
    holder.communicate(timeout=30)

    assert any("waited" in record.getMessage() and "snapshot of" in record.getMessage()
               for record in caplog.records), caplog.text


def test_snapshot_gives_up_at_the_bound_with_the_wait_in_the_message(tmp_path, held_store, monkeypatch):
    """A wedged holder must surface as a visible failure, never a hang."""
    monkeypatch.setattr(checkpoint_pruning, "SNAPSHOT_LOCK_TIMEOUT_SECONDS", 1.0)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("one", encoding="utf-8")

    held_store(30.0)
    manager = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
    started = time.monotonic()
    # The contract of t_4c4455f3: never raises, always returns, records the failure.
    assert manager.ensure_checkpoint(str(project), "bounded") is False
    elapsed = time.monotonic() - started

    assert 0.9 <= elapsed < 10, f"the bound must be honoured, took {elapsed:.2f}s"
    assert manager._last_failure is not None
    error = manager._last_failure["error"]
    assert "busy" in error and "waited" in error and "sibling" not in error
    assert "1.0s" in error, error


def test_unbounded_wait_is_honoured_when_timeout_is_none(tmp_path, held_store):
    """``timeout=None`` means wait forever, not refuse at t=0 — the docstring's contract."""
    base = tmp_path / "checkpoints"
    held_store(1.5)

    started = time.monotonic()
    with checkpoint_pruning.store_lock(base, timeout=None, purpose="unbounded probe"):
        waited = time.monotonic() - started

    assert waited >= 1.2, f"timeout=None must wait for the holder, returned after {waited:.2f}s"


def test_destructive_maintenance_does_not_queue_behind_a_writer(tmp_path, held_store):
    """``clear_all`` keeps grab-or-refuse: queueing there would delay a destructive op behind
    a routine snapshot for no benefit."""
    from tools import checkpoint_maintenance

    held_store(10.0)
    started = time.monotonic()
    result = checkpoint_maintenance.clear_all(tmp_path / "checkpoints")
    elapsed = time.monotonic() - started

    assert result["deleted"] is False
    assert elapsed < 2.0, f"clear_all must not wait for a live writer, took {elapsed:.2f}s"


def test_concurrent_snapshots_still_serialize_on_one_store(tmp_path, monkeypatch):
    """The wait must not weaken serialization: parallel takers on one store all land."""
    base = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoints, "CHECKPOINT_BASE", base)
    program = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from tools import checkpoint_manager as c\n"
        "c.CHECKPOINT_BASE = Path(sys.argv[1])\n"
        "mgr = c.CheckpointManager(enabled=True, max_total_size_mb=0)\n"
        "ok = mgr.ensure_checkpoint(sys.argv[2], 'concurrent ' + sys.argv[3])\n"
        "print(f'{sys.argv[3]} {ok}', flush=True)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(checkpoints.__file__).resolve().parents[1])}
    children = []
    for index in range(3):
        project = tmp_path / f"proj{index}"
        project.mkdir()
        for number in range(80):
            (project / f"f{number}.txt").write_text("z" * 200, encoding="utf-8")
        children.append(subprocess.Popen(
            [sys.executable, "-u", "-c", program, str(base), str(project), str(index)],
            cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8"))
    results = []
    for child in children:
        output, error = child.communicate(timeout=600)
        assert child.returncode == 0, output + error
        results.append(output.strip())
    assert all(line.endswith(" True") for line in results), results

    refs = subprocess.run(
        ["git", "--git-dir", str(base / "store"), "for-each-ref", "refs/hermes",
         "--format=%(refname)"], capture_output=True, text=True, check=True)
    assert len(refs.stdout.split()) == 3
    fsck = subprocess.run(
        ["git", "--git-dir", str(base / "store"), "fsck", "--no-progress"],
        capture_output=True, text=True)
    assert fsck.returncode == 0, fsck.stderr
