"""A key slot is released when the run that held it ends — including when the
worker that held it was killed.

The defect this pins: ``timed_out`` / ``crashed`` / ``stale`` / ``gave_up`` all
close the run from the DISPATCHER process. The worker never runs its own
``atexit`` release (it was SIGKILLed, or it died), so its reservation held a
card slot until the TTL backstop (``max_runtime + 30min``). Two such corpses on
a 6-slot pool were enough for ``hermes kanban dispatch`` to answer
``spawned: []`` with every ready card in ``skipped_keypool_capped``, and the
gateway logged "ready queue non-empty for 36 consecutive ticks but 0 workers
spawned" while nothing was actually busy.

What must stay true the other way round: a card whose worker is STILL ALIVE
keeps its key. Releasing a live card's slot would hand its account to the next
card, which is the single guarantee the pool exists to provide.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent import kanban_key_pool as kp
from hermes_cli import kanban_db_keypool as kbkp

BASE_URL = "https://llm.example.test/openai"


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    for key in (kp.ENV_KEYPOOL, kp.ENV_KEYPOOL_STATE):
        monkeypatch.delenv(key, raising=False)
    # ``kanban_home()`` is ROOT-anchored by design (``HERMES_HOME`` is ignored so
    # the board stays shared across profiles), so a test that only moves
    # ``HERMES_HOME`` still opens the operator's LIVE board. Pin the kanban root
    # here, once, for every test in this module.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kp.reset_process_cache()
    yield
    # ``_pool`` writes straight to ``os.environ`` (it predates the fixture);
    # without this restore the pool path leaks into every later test in the
    # session and the dispatcher's key-pool gate starts deferring THEIR cards.
    for key in (kp.ENV_KEYPOOL, kp.ENV_KEYPOOL_STATE):
        os.environ.pop(key, None)
    kp.reset_process_cache()


def _pool(tmp_path: Path, *, card_keys: int = 2, max_cards_per_key: int = 1) -> Path:
    keys = [{"id": 1, "label": "background", "role": "background", "api_key": "KEY_BG"}]
    for _id in range(2, 2 + card_keys):
        keys.append({"id": _id, "label": f"account-{_id}", "role": "card",
                     "api_key": f"KEY_CARD_{_id}"})
    path = tmp_path / "kanban-keypool.json"
    path.write_text(json.dumps({
        "version": 1, "enabled": True, "base_url": BASE_URL, "provider": "letovo",
        "background_key_id": 1, "max_cards_per_key": max_cards_per_key, "keys": keys,
    }), encoding="utf-8")
    os.environ[kp.ENV_KEYPOOL] = str(path)
    os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "kanban-keypool.state.json")
    kp.reset_process_cache()
    return path


@pytest.fixture()
def live_pid():
    """Factory for genuinely live child pids, all killed and reaped on teardown."""
    children: list[subprocess.Popen] = []

    def _make() -> int:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        children.append(proc)
        for _ in range(400):
            if kp._pid_alive(proc.pid):
                return proc.pid
            time.sleep(0.01)
        raise AssertionError("child never became alive")

    def _die(pid: int) -> None:
        """Kill AND reap: on POSIX an unreaped child stays visible to psutil."""
        for proc in children:
            if proc.pid == pid:
                proc.kill()
                proc.wait(timeout=10)
                return
        raise AssertionError(f"unknown pid {pid}")

    _make.die = _die  # type: ignore[attr-defined]
    yield _make
    for proc in children:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # pragma: no cover
            pass


def _dead_pid() -> int:
    """A pid that is definitely not running (spawn, wait, reuse the number)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _occupancy() -> dict:
    """``{key_id: live_slots}`` from the status view (stale slots excluded)."""
    status = kp.pool_status()
    out: dict[int, int] = {}
    for slot in status["reservations"]:
        if slot.get("stale"):
            continue
        kid = int(slot.get("key_id") or 0)
        out[kid] = out.get(kid, 0) + 1
    return out


# ── release_dead_task_slots ────────────────────────────────────────────────


def test_dead_workers_slot_is_freed_by_the_ending_transition(tmp_path):
    """The exact production shape: a ``timed_out`` worker whose pid is gone."""
    _pool(tmp_path, card_keys=1)
    kp.reserve_for_card("t_timed_out", 264, pid=_dead_pid())
    assert len(kp.pool_status()["reservations"]) == 1

    released = kbkp.release_dead_card_key("t_timed_out")

    assert [r["task_id"] for r in released] == ["t_timed_out"]
    assert released[0]["released_reason"] in {"pid_gone", "ttl_expired"}
    assert kp.pool_status()["reservations"] == []
    # The freed slot is genuinely available, not merely reported free.
    assert kbkp.keypool_has_free_slot() is True


def test_a_live_worker_keeps_its_key(tmp_path, live_pid):
    """Never free a slot whose owner is alive: that would share its account."""
    _pool(tmp_path, card_keys=1)
    pid = live_pid()
    kp.reserve_for_card("t_live", 1, pid=pid)

    released = kbkp.release_dead_card_key("t_live")

    assert released == []
    assert [r["task_id"] for r in kp.pool_status()["reservations"]] == ["t_live"]
    assert kbkp.keypool_has_free_slot() is False


def test_only_the_finished_cards_slots_are_touched(tmp_path, live_pid):
    """Two cards on one key: the dead one goes, the live one stays."""
    _pool(tmp_path, card_keys=1, max_cards_per_key=2)
    dead, live = _dead_pid(), live_pid()
    kp.reserve_for_card("t_dead", 1, pid=dead)
    kp.reserve_for_card("t_live", 2, pid=live)

    kbkp.release_dead_card_key("t_dead")

    assert sorted(r["task_id"] for r in kp.pool_status()["reservations"]) == ["t_live"]


def test_an_unknown_card_is_a_noop(tmp_path):
    _pool(tmp_path, card_keys=1)
    kp.reserve_for_card("t_other", 1, pid=_dead_pid())

    assert kbkp.release_dead_card_key("t_nothing") == []
    assert [r["task_id"] for r in kp.pool_status()["reservations"]] == ["t_other"]


def test_release_is_a_noop_without_a_pool(tmp_path, monkeypatch):
    monkeypatch.delenv(kp.ENV_KEYPOOL, raising=False)
    monkeypatch.delenv(kp.ENV_KEYPOOL_STATE, raising=False)
    kp.reset_process_cache()

    assert kbkp.release_dead_card_key("t_any") == []
    assert kbkp.keypool_has_free_slot() is True


# ── the tick sweep is the backstop ─────────────────────────────────────────


def test_sweep_reaps_a_corpse_no_transition_named(tmp_path):
    """A reservation orphaned by a host restart: no death path saw the card."""
    _pool(tmp_path)
    kp.reserve_for_card("t_orphan", 7, pid=_dead_pid())

    reaped = kbkp.keypool_sweep()

    assert [r["task_id"] for r in reaped] == ["t_orphan"]
    assert kp.pool_status()["reservations"] == []


def test_sweep_never_touches_a_live_slot(tmp_path, live_pid):
    _pool(tmp_path)
    pid = live_pid()
    kp.reserve_for_card("t_live", 1, pid=pid)
    assert kbkp.keypool_sweep() == []
    assert [r["task_id"] for r in kp.pool_status()["reservations"]] == ["t_live"]


# ── the spawn gate counts live slots only ──────────────────────────────────


def test_gate_does_not_count_a_corpse_against_capacity(tmp_path):
    """The starvation itself: a stale slot must not read as occupied.

    Before the fix ``used`` included stale slots, so a pool of corpses answered
    "every card key is full" forever and the whole ready queue waited on it.
    """
    _pool(tmp_path, card_keys=1)
    kp.reserve_for_card("t_corpse", 1, pid=_dead_pid())

    # Not yet swept: the row is still in the state file.
    assert len(kp.pool_status()["reservations"]) == 1
    assert kbkp.keypool_has_free_slot() is True


def test_gate_counts_every_live_slot(tmp_path, live_pid):
    _pool(tmp_path, card_keys=1)
    kp.reserve_for_card("t_live", 1, pid=live_pid())

    assert kbkp.keypool_has_free_slot() is False
    assert kbkp.keypool_has_free_slot(reserve=0) is False


def test_gate_reserves_in_tick_headroom(tmp_path):
    _pool(tmp_path, card_keys=2)
    assert kbkp.keypool_has_free_slot() is True
    assert kbkp.keypool_has_free_slot(reserve=1) is True
    assert kbkp.keypool_has_free_slot(reserve=2) is False


def test_gate_honours_max_cards_per_key(tmp_path, live_pid):
    _pool(tmp_path, card_keys=1, max_cards_per_key=2)
    kp.reserve_for_card("t_a", 1, pid=live_pid())

    assert kbkp.keypool_has_free_slot() is True  # 1 of 2 slots taken
    kp.reserve_for_card("t_b", 2, pid=live_pid())
    assert kbkp.keypool_has_free_slot() is False


# ── zombie: exited but not yet reaped is dead ──────────────────────────────


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="zombie probe is /proc")
def test_an_unreaped_zombie_does_not_hold_a_slot(tmp_path):
    """Linux: the dispatcher SIGKILLs a worker; its corpse is visible to
    ``pid_exists`` until the parent reaps it. The slot must free anyway."""
    _pool(tmp_path, card_keys=1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        assert kp._pid_alive(proc.pid) is True
        kp.reserve_for_card("t_zombie", 1, pid=proc.pid)
        proc.kill()  # killed, NOT waited on: a zombie right now
        for _ in range(400):
            state = Path(f"/proc/{proc.pid}/status")
            try:
                body = state.read_text(encoding="utf-8")
            except FileNotFoundError:  # pragma: no cover - reaped early
                break
            if "State:\tZ" in body:
                break
            time.sleep(0.01)
        assert kp._pid_alive(proc.pid) is False
        assert kbkp.keypool_has_free_slot() is True
        assert [r["task_id"] for r in kbkp.release_dead_card_key("t_zombie")] == ["t_zombie"]
    finally:
        proc.wait(timeout=10)


# ── end to end: a timed-out run leaves no STALE row ────────────────────────


def test_a_timed_out_run_frees_its_slot_in_the_same_tick(tmp_path, monkeypatch, live_pid):
    """Acceptance criterion 1, on the real dispatcher path.

    ``enforce_max_runtime`` kills the worker and closes the run; the slot must
    be gone when the call returns, without anyone running ``keypool sweep``.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    _pool(tmp_path, card_keys=1)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="slow", assignee="01-orchestrator",
                             max_runtime_seconds=1)
        kb.claim_task(conn, tid)
        worker_pid = live_pid()
        kbd._set_worker_pid(conn, tid, worker_pid)
        started = int(time.time()) - 120
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (started, tid))
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (started, tid),
            )
        # The card's worker really holds a key, and the cap is about to kill it.
        kp.reserve_for_card(tid, 1, pid=worker_pid)
        assert kbkp.keypool_has_free_slot() is False

        killed: list = []

        def _kill(pid, sig):
            killed.append((pid, sig))
            live_pid.die(pid)

        timed_out = kbd.enforce_max_runtime(conn, signal_fn=_kill)

        assert tid in timed_out
        # No STALE row for this run right after it ended.
        assert kp.pool_status()["reservations"] == []
        assert kbkp.keypool_has_free_slot() is True


def test_a_crash_frees_its_slot_in_the_same_tick(tmp_path, monkeypatch, live_pid):
    """Same criterion on the crash path (``detect_crashed_workers``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    _pool(tmp_path, card_keys=1)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="crash", assignee="01-orchestrator")
        kb.claim_task(conn, tid)
        dead = _dead_pid()
        kbd._set_worker_pid(conn, tid, dead)
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?",
                     (int(time.time()) - 120, tid))
        worker_pid = live_pid()
        kp.reserve_for_card(tid, 1, pid=worker_pid)
        assert kbkp.keypool_has_free_slot() is False
        live_pid.die(worker_pid)
        # The dispatcher's pid row must match the corpse it will reclaim.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (worker_pid, tid))
            conn.execute("UPDATE task_runs SET worker_pid = ? "
                         "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                         (worker_pid, tid))

        crashed = kbd.detect_crashed_workers(conn)

        assert tid in crashed
        assert kp.pool_status()["reservations"] == []
        assert kbkp.keypool_has_free_slot() is True


# ── a dispatch tick sweeps before it gates ─────────────────────────────────


def test_dispatch_tick_sweeps_a_corpse_before_gating(tmp_path, monkeypatch):
    """Acceptance criterion 3: with a corpse in the pool, a ready card spawns.

    The corpse's card is not in this tick's death lists (its run ended in a
    previous process), so only the tick sweep can explain it.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    _pool(tmp_path, card_keys=1)
    # This home has no profile directories, so the spawn gate would refuse the
    # card as ``skipped_nonspawnable`` before it ever reaches the key-pool gate.
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    kb.init_db()
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="waiting", assignee="01-orchestrator")
        kp.reserve_for_card("t_finished_elsewhere", 9, pid=_dead_pid())

        spawned: list = []

        def _fake_spawn(*args, **kwargs):
            spawned.append(args[0].id)
            return 4242

        original = kbd._default_spawn
        kbd._default_spawn = _fake_spawn
        try:
            res = kbd.dispatch_once(conn)
        finally:
            kbd._default_spawn = original

        assert tid not in res.skipped_keypool_capped
        assert [s[0] for s in res.spawned] == [tid]
        assert [r["task_id"] for r in res.keypool_reaped] == ["t_finished_elsewhere"]
        assert [r["task_id"] for r in res.keypool_reaped] == ["t_finished_elsewhere"]
