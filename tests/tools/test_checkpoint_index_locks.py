"""Checkpoint index locks + the visible-failure channel (tools/checkpoint_locks.py).

Why this file exists
--------------------
A worker killed mid-``git add`` leaves ``indexes/<hash>.lock`` behind; every later
checkpoint of that project then dies with "File exists", and before this change the
failure only reached ``agent.log``.  Two properties are asserted here, each with a real
``git`` rather than a mock of one:

* **A dead lock is repaired, a live one is never touched.**  The proof comes from
  ``hermes_cli._early_recovery._held_open`` (``/proc`` fd scan on Linux), the age gate is a
  second independent gate, and the retry path may skip the gate but never the proof.
* **Every real refusal reaches the notice channel.**  All five stages of ``_take()``
  (``unstage excluded``, ``oversize cleanup``, ``git write-tree``, ``git commit-tree``,
  ``git update-ref``) record a failure and post one notice, while the routine
  "nothing to snapshot" skips (``no changes``, ``empty tree``) stay at ``logger.debug``.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import tools.checkpoint_manager as checkpoints
from tools.checkpoint_locks import (
    index_lock_path,
    journal_path,
    release_dead_lock,
    sweep_index_locks,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Isolated checkpoint base — never writes to ~/.hermes/."""
    root = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoints, "CHECKPOINT_BASE", root)
    return root


@pytest.fixture()
def project(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "main.py").write_text("print('hello')\n", encoding="utf-8")
    return d


@pytest.fixture()
def manager(base):
    return checkpoints.CheckpointManager(enabled=True, max_snapshots=50)


@pytest.fixture()
def notices(manager):
    """Collect what the manager would say on the visible channel."""
    seen: list[str] = []
    manager.notice_callback = seen.append
    return seen


def _store(base: Path) -> Path:
    return checkpoints._store_path(base)


def _index_file(base: Path, work: Path) -> Path:
    return checkpoints._index_path(_store(base), checkpoints._project_hash(str(work)))


def _aged(path: Path, seconds: float) -> Path:
    """Backdate ``path`` so the age gate sees a lock old enough to be debris."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def _journal_records(base: Path) -> list[dict]:
    path = journal_path(base)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# =========================================================================
# Five failure stages of _take() → one notice each
# =========================================================================

def _fail_on(predicate):
    """``_run_git`` that refuses the matching call and passes every other one through."""
    original = checkpoints._run_git

    def run(args, *rest, **kwargs):
        if predicate(list(args)):
            return False, "", "simulated refusal for this stage"
        return original(args, *rest, **kwargs)

    return run


def _stage_matcher(stage: str):
    """The exact git call that carries ``stage`` in ``_take()``."""
    return {
        # ``ls-files --ignored --exclude-from=<store>/info/exclude`` is only ever asked by
        # ``_unstage_excluded``; the ``--others`` preflight call has extra flags.
        "unstage excluded": lambda args: args[:2] == ["ls-files", "--cached"]
        and any(a.startswith("--exclude-from=") for a in args),
        # Bare ``ls-files --cached -z`` belongs to ``_drop_oversize_from_index``.
        "oversize cleanup": lambda args: args == ["ls-files", "--cached", "-z"],
        "git write-tree": lambda args: args == ["write-tree"],
        "git commit-tree": lambda args: args[:1] == ["commit-tree"],
        "git update-ref": lambda args: args[:1] == ["update-ref"],
    }[stage]


@pytest.mark.parametrize("stage", [
    "unstage excluded",
    "oversize cleanup",
    "git write-tree",
    "git commit-tree",
    "git update-ref",
])
def test_each_real_refusal_reaches_the_notice_channel(
    base, project, manager, notices, monkeypatch, stage,
):
    """Every stage that can refuse a checkpoint must say so out loud, not only in the log."""
    monkeypatch.setattr(checkpoints, "_run_git", _fail_on(_stage_matcher(stage)))

    assert manager.ensure_checkpoint(str(project), "stage probe") is False

    report = manager.lock_report()
    assert report["last_failure"] is not None, f"{stage} refusal was swallowed"
    assert report["last_failure"]["stage"] == stage
    assert report["last_failure"]["error"], "the failure must carry the git error text"
    assert len(notices) == 1, f"{stage} must produce exactly one notice, got {notices}"
    assert stage in notices[0]
    assert str(project) in notices[0]


def test_routine_skips_are_not_failures_and_post_no_notice(base, project, manager, notices):
    """``no changes`` and ``empty tree`` are normal: no notice, no failure record."""
    assert manager.ensure_checkpoint(str(project), "seed") is True
    manager.new_turn()
    assert manager.ensure_checkpoint(str(project), "unchanged") is False

    empty = project.parent / "empty-project"
    empty.mkdir()
    manager.new_turn()
    assert manager.ensure_checkpoint(str(empty), "empty") is False

    assert notices == []
    assert manager.lock_report()["last_failure"] is None


def test_retry_path_record_survives_the_callers_git_add_branch(
    base, project, manager, notices, monkeypatch,
):
    """A held lock records ``git add (index lock held)``; the caller must not overwrite it.

    The retry path knows *why* staging failed and names it.  The outer ``_take()`` branch
    ("git add failed") would replace that with a vaguer stage, so it is written only when
    nothing was recorded yet.
    """
    index_file = _index_file(base, project)

    def refuse_add(args, *rest, **kwargs):
        if args[:1] == ["add"]:
            return False, "", f"fatal: unable to create '{index_lock_path(index_file)}': File exists"
        return True, "", ""

    monkeypatch.setattr(checkpoints, "_run_git", refuse_add)
    monkeypatch.setattr("tools.checkpoint_locks.release_dead_lock", lambda *a, **k: False)

    assert manager.ensure_checkpoint(str(project), "held") is False

    failure = manager.lock_report()["last_failure"]
    assert failure["stage"] == "git add (index lock held)"
    assert len(notices) == 1


def test_plain_git_add_failure_is_recorded_under_git_add(base, project, manager, notices, monkeypatch):
    """A staging refusal that is not lock contention is still one visible notice."""
    monkeypatch.setattr(
        checkpoints, "_run_git",
        _fail_on(lambda args: args[:1] == ["add"]),
    )

    assert manager.ensure_checkpoint(str(project), "add probe") is False

    failure = manager.lock_report()["last_failure"]
    assert failure["stage"] == "git add"
    assert len(notices) == 1


# =========================================================================
# The notice channel itself (card comment)
# =========================================================================

@pytest.fixture()
def card_db(tmp_path, monkeypatch):
    """A throwaway board with one card, and that card id in the worker env."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import checkpoint_notice

    home = tmp_path / "kanban_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(checkpoint_notice, "_sent", set())

    kbc.init_db()
    conn = kbc.connect()
    task_id = kb.create_task(conn, title="checkpoint probe", assignee="02-hermes-platform",
                             created_by="test")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    try:
        yield conn, task_id
    finally:
        conn.close()


def test_failure_lands_a_comment_on_the_workers_card(base, project, manager, card_db, monkeypatch):
    """The point of the whole change: a dead checkpoint stream is visible on the card."""
    conn, task_id = card_db
    monkeypatch.setattr(
        checkpoints, "_run_git",
        _fail_on(lambda args: args == ["write-tree"]),
    )

    assert manager.ensure_checkpoint(str(project), "card probe") is False

    rows = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["author"] == "checkpoint-manager"
    assert "git write-tree" in rows[0]["body"]


def test_the_same_notice_is_posted_only_once(base, project, manager, card_db, monkeypatch):
    """A per-turn failure must not spam the thread with one identical comment per turn."""
    conn, task_id = card_db
    monkeypatch.setattr(
        checkpoints, "_run_git",
        _fail_on(lambda args: args == ["write-tree"]),
    )

    for turn in range(3):
        manager.new_turn()
        assert manager.ensure_checkpoint(str(project), f"probe {turn}") is False

    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchall()
    assert len(rows) == 1


# =========================================================================
# Dead lock repaired, live lock kept
# =========================================================================

def test_startup_sweep_repairs_a_lock_left_by_a_killed_worker(base, project, manager):
    """The exact failure mode: a zero-byte lock outlives its owner and blocks every snapshot."""
    assert manager.ensure_checkpoint(str(project), "before the crash") is True
    lock = index_lock_path(_index_file(base, project))
    lock.write_bytes(b"")
    _aged(lock, 3600)
    assert lock.stat().st_size == 0

    (project / "after.py").write_text("x = 1\n", encoding="utf-8")
    successor = checkpoints.CheckpointManager(enabled=True, max_snapshots=50)

    assert successor.ensure_checkpoint(str(project), "after the crash") is True
    assert not lock.exists(), "the abandoned lock must be gone"
    assert successor.lock_report()["released"] == [str(lock)]
    assert [r["action"] for r in _journal_records(base)] == ["removed"]


def test_lock_left_by_a_killed_worker_is_released_by_the_retry_path(base, project, manager):
    """A lock that just refused staging is retried immediately, without waiting for the gate."""
    assert manager.ensure_checkpoint(str(project), "before the crash") is True
    lock = index_lock_path(_index_file(base, project))
    lock.write_bytes(b"")  # fresh mtime: the startup sweep would keep it
    (project / "after.py").write_text("x = 1\n", encoding="utf-8")

    # A fresh manager: its startup sweep runs first and must keep the young lock, so the
    # repair has to come from the retry path that saw git refuse.
    successor = checkpoints.CheckpointManager(enabled=True, max_snapshots=50)
    seen: list[str] = []
    successor.notice_callback = seen.append
    assert successor.ensure_checkpoint(str(project), "after the crash") is True
    assert not lock.exists()
    assert successor.lock_report()["released"] == [str(lock)]
    assert any("released an abandoned index lock" in note for note in seen)


def test_a_live_holder_keeps_its_lock(base, project, manager):
    """A ``git`` that is really holding ``indexes/<hash>.lock`` is never overruled by age."""
    assert manager.ensure_checkpoint(str(project), "seed") is True
    store = _store(base)
    index_file = _index_file(base, project)
    lock = index_lock_path(index_file)

    proc = _hold_index_lock(index_file, store, project)
    try:
        assert lock.exists(), "the live git must own the lock before the assertion means anything"
        _aged(lock, 3600)
        result = sweep_index_locks(store, min_age=0)
        assert len(result.removed) == 0
        assert [Path(d.path) for d in result.kept] == [lock]
        assert result.kept[0].reason.startswith("owner possible")
        assert "pid" in result.kept[0].holder, "the journal must name the process that holds it"
        assert not release_dead_lock(lock, store=store)
        assert lock.exists()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_young_lock_is_gated_at_startup_but_released_on_the_retry_path(base, project):
    """Two independent gates: age protects a lock nobody can prove dead yet; a refusal does not wait."""
    assert checkpoints.CheckpointManager(enabled=True).ensure_checkpoint(
        str(project), "seed") is True
    store = _store(base)
    lock = index_lock_path(_index_file(base, project))
    lock.write_bytes(b"")

    gated = sweep_index_locks(store)
    assert gated.removed == []
    assert [Path(d.path) for d in gated.kept] == [lock]
    assert gated.kept[0].reason == "younger than age gate"

    assert release_dead_lock(lock, store=store) is True
    assert not lock.exists()
    assert _journal_records(base)[-1]["reason"].endswith("(retry path)")


def _hold_index_lock(index_file: Path, store: Path, work: Path) -> subprocess.Popen:
    """Start a real ``git`` that takes ``<index>.lock`` and holds it until stdin closes."""
    index_file.parent.mkdir(parents=True, exist_ok=True)
    if not (store / "HEAD").exists():
        subprocess.run(["git", "init", "-q", "--bare", str(store)], check=True)
    env = {
        **os.environ,
        "GIT_DIR": str(store),
        "GIT_WORK_TREE": str(work),
        "GIT_INDEX_FILE": str(index_file),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    proc = subprocess.Popen(
        ["git", "update-index", "--index-info"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, cwd=str(work),
    )
    lock = index_lock_path(index_file)
    deadline = time.time() + 20
    while time.time() < deadline:
        if lock.exists():
            return proc
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("the probe git never created its index lock")


# =========================================================================
# Sweep unit behaviour
# =========================================================================

def test_a_killed_worker_run_is_repaired_end_to_end(base, project):
    """Live probe shape: SIGKILL a real ``git add`` mid-flight, then snapshot again.

    This is the card's own acceptance scenario, at test scale: the killed git leaves a
    zero-byte lock, and the very next ``ensure_checkpoint`` of that project must succeed.
    """
    import signal

    assert checkpoints.CheckpointManager(enabled=True).ensure_checkpoint(
        str(project), "before the kill") is True
    index_file = _index_file(base, project)
    store = _store(base)
    (project / "work.py").write_text("y = 2\n", encoding="utf-8")

    proc = _hold_index_lock(index_file, store, project)
    lock = index_lock_path(index_file)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    _aged(lock, 3600)
    assert lock.exists() and lock.stat().st_size == 0

    assert checkpoints.CheckpointManager(enabled=True).ensure_checkpoint(
        str(project), "after the kill") is True
    assert not lock.exists()


def test_age_gate_is_configurable_from_the_documented_env(monkeypatch):
    """The gate is an operator knob, not a hard-coded ten minutes."""
    from tools.checkpoint_locks import DEFAULT_MIN_AGE_SECONDS, min_age_seconds

    monkeypatch.delenv("HERMES_CHECKPOINT_LOCK_MIN_AGE_MINUTES", raising=False)
    assert min_age_seconds() == DEFAULT_MIN_AGE_SECONDS

    monkeypatch.setenv("HERMES_CHECKPOINT_LOCK_MIN_AGE_MINUTES", "0.5")
    assert min_age_seconds() == 30

    monkeypatch.setenv("HERMES_CHECKPOINT_LOCK_MIN_AGE_MINUTES", "not-a-number")
    assert min_age_seconds() == DEFAULT_MIN_AGE_SECONDS


def test_release_dead_lock_refuses_an_absent_lock_and_a_live_one(base):
    """``release_dead_lock`` is the single destructive primitive: both refusals matter."""
    store = _store(base)
    missing = store / "indexes" / "ghost.lock"
    assert release_dead_lock(missing, store=store) is False

    live = store / "indexes" / "live.lock"
    live.parent.mkdir(parents=True, exist_ok=True)
    proc = _hold_index_lock(live.with_suffix(""), store, base.parent)
    try:
        assert release_dead_lock(live, store=store) is False
        assert live.exists()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_non_empty_lock_is_never_debris(base):
    """A lock with bytes in it is a live index being written, whatever its age."""
    store = _store(base)
    (store / "indexes").mkdir(parents=True)
    lock = store / "indexes" / "abcdef.lock"
    lock.write_bytes(b"index payload")
    _aged(lock, 7200)

    result = sweep_index_locks(store, min_age=0)

    assert result.removed == []
    assert result.kept[0].reason == "not empty"
    assert lock.exists()


def test_dry_run_reports_without_removing(base):
    """``hermes checkpoints locks`` must be able to report a fleet before touching it."""
    store = _store(base)
    (store / "indexes").mkdir(parents=True)
    lock = store / "indexes" / "abcdef.lock"
    lock.write_bytes(b"")
    _aged(lock, 3600)

    result = sweep_index_locks(store, dry_run=True)

    assert lock.exists()
    assert [d.action for d in result.removed] == ["would-remove"]
    assert result.journal_path is None
    assert not journal_path(base).exists(), "a report-only run must not write the journal"


def test_journal_records_every_decision_including_kept_ones(base):
    """A fleet cleanup must be attachable to a card: who was removed *and* who was spared."""
    store = _store(base)
    (store / "indexes").mkdir(parents=True)
    removed = store / "indexes" / "aaaa.lock"
    removed.write_bytes(b"")
    _aged(removed, 3600)
    kept = store / "indexes" / "bbbb.lock"
    kept.write_bytes(b"live")

    result = sweep_index_locks(store, min_age=0)

    assert result.journal_path == str(journal_path(base))
    records = _journal_records(base)
    actions = {Path(r["path"]).name: r["action"] for r in records}
    assert actions == {"aaaa.lock": "removed", "bbbb.lock": "kept"}
    assert all("ts" in r and "age_seconds" in r and "size_bytes" in r for r in records)


def test_sweep_ignores_locks_outside_the_indexes_directory(base):
    """Only ``indexes/*.lock`` is in scope; a store's own refs/packs are never swept."""
    store = _store(base)
    (store / "indexes").mkdir(parents=True)
    stray = store / "HEAD.lock"
    stray.write_bytes(b"")
    _aged(stray, 3600)

    result = sweep_index_locks(store, min_age=0)

    assert result.scanned == 0
    assert stray.exists()


def test_locks_single_profile_reports_only_the_active_store(base):
    """Without ``--all-profiles`` the command stays on this profile's store."""
    from hermes_cli import checkpoints as cli

    indexes = _store(base) / "indexes"
    indexes.mkdir(parents=True)
    lock = indexes / "abcdef.lock"
    lock.write_bytes(b"")
    _aged(lock, 3600)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.cmd_locks(argparse.Namespace(all_profiles=False, clean=False))
    output = buffer.getvalue()

    assert code == 0
    assert str(base) in output
    assert "removable=1" in output
    assert "Run with --clean to remove them" in output
    assert lock.exists()


def test_locks_skips_a_profile_without_a_store(base):
    """A profile that never snapshotted anything is skipped, not reported as zero."""
    from hermes_cli import checkpoints as cli

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.cmd_locks(argparse.Namespace(all_profiles=False, clean=False))

    assert code == 0
    assert "no store/indexes — skipped" in buffer.getvalue()
    assert "Total: removable=0 kept=0" in buffer.getvalue()


# =========================================================================
# `hermes checkpoints locks` over a fleet of profiles
# =========================================================================

@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    """Two profiles plus the root, each with a store holding one abandoned lock."""
    root = tmp_path / "hermes_root"
    bases = [root / "checkpoints"] + [root / "profiles" / name / "checkpoints"
                                      for name in ("01-orchestrator", "02-hermes-platform")]
    locks = []
    for base in bases:
        indexes = _store(base) / "indexes"
        indexes.mkdir(parents=True)
        lock = indexes / "deadbeef.lock"
        lock.write_bytes(b"")
        locks.append(_aged(lock, 3600))

    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: root)
    return bases, locks


def _run_locks(*, clean: bool = False) -> tuple[int, str]:
    from hermes_cli import checkpoints as cli

    args = argparse.Namespace(all_profiles=True, clean=clean)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.cmd_locks(args)
    return code, buffer.getvalue()


def test_locks_all_profiles_dry_run_removes_nothing(fleet):
    """Report-only is the default: an operator reads the fleet before anything is deleted."""
    bases, locks = fleet

    code, output = _run_locks()

    assert code == 0
    assert all(lock.exists() for lock in locks)
    assert "removable=1" in output
    for base in bases:
        assert str(base) in output
        assert not journal_path(base).exists()
    assert "Total: removable=3 kept=0" in output


def test_locks_all_profiles_clean_removes_and_journals_every_profile(fleet):
    """``--clean`` removes and leaves one journal line per profile — the auditable trace."""
    bases, locks = fleet

    code, output = _run_locks(clean=True)

    assert code == 0
    assert not any(lock.exists() for lock in locks)
    journal_lines = []
    for base in bases:
        records = _journal_records(base)
        assert len(records) == 1, f"{base} journal: {records}"
        assert records[0]["action"] == "removed"
        journal_lines.append(records[0]["path"])
        assert str(journal_path(base)) in output
    assert sorted(journal_lines) == sorted(str(lock) for lock in locks)
    assert "Total: removable=3 kept=0" in output


def test_locks_all_profiles_keeps_a_live_holders_lock(fleet):
    """A fleet run leaves a lock a running git owns, even with ``--clean``.

    The probe widens from "this fd is open" to "a git works in this store", so the
    sibling lock of the same store is spared too: a live ``git add`` may not have opened
    its own lock file yet, and deleting a neighbour's would be the same gamble.
    """
    bases, locks = fleet
    store = _store(bases[1])
    index_file = store / "indexes" / "live"
    lock = index_lock_path(index_file)

    proc = _hold_index_lock(index_file, store, bases[1].parent)
    try:
        _aged(lock, 3600)
        code, output = _run_locks(clean=True)
        assert code == 0
        assert lock.exists()
        sibling = store / "indexes" / "deadbeef.lock"
        assert sibling.exists()
        assert "kept=2" in output
        # The two other profiles' debris is still cleaned.
        assert not locks[0].exists() and not locks[2].exists()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
