"""Red proof for the key-pool release fix, using ONLY pre-existing APIs.

Run this file against the BASE tree (before the fix): every test here must fail
there, and pass on the branch. It deliberately imports nothing from the new
``hermes_cli.kanban_db_keypool`` module, so a red run proves the BEHAVIOUR was
missing — not merely that a new name is absent.
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

BASE_URL = "https://llm.example.test/openai"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    for key in (kp.ENV_KEYPOOL, kp.ENV_KEYPOOL_STATE):
        monkeypatch.delenv(key, raising=False)
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


def _pool(tmp_path: Path, *, card_keys: int = 1) -> None:
    keys = [{"id": 1, "label": "background", "role": "background", "api_key": "KEY_BG"}]
    for _id in range(2, 2 + card_keys):
        keys.append({"id": _id, "label": f"account-{_id}", "role": "card",
                     "api_key": f"KEY_CARD_{_id}"})
    path = tmp_path / "kanban-keypool.json"
    path.write_text(json.dumps({
        "version": 1, "enabled": True, "base_url": BASE_URL, "provider": "letovo",
        "background_key_id": 1, "max_cards_per_key": 1, "keys": keys,
    }), encoding="utf-8")
    os.environ[kp.ENV_KEYPOOL] = str(path)
    os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "kanban-keypool.state.json")
    kp.reset_process_cache()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture()
def live_pid():
    children: list[subprocess.Popen] = []

    def _make() -> int:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(proc)
        for _ in range(400):
            if kp._pid_alive(proc.pid):
                return proc.pid
            time.sleep(0.01)
        raise AssertionError("child never became alive")

    def _die(pid: int) -> None:
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


def test_gate_does_not_count_a_corpse_against_capacity(tmp_path):
    """Base behaviour: ``used`` counts a dead owner's slot, so capacity reads 0."""
    from hermes_cli import kanban_db_dispatch as kbd

    _pool(tmp_path, card_keys=1)
    kp.reserve_for_card("t_corpse", 1, pid=_dead_pid())

    assert kbd._keypool_has_free_slot() is True


def test_a_timed_out_run_frees_its_slot_in_the_same_tick(tmp_path, live_pid):
    """Base behaviour: the killed worker's slot is still there after the end."""
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
                (started, tid))
        kp.reserve_for_card(tid, 1, pid=worker_pid)
        assert kbd._keypool_has_free_slot() is False

        def _kill(pid, sig):
            live_pid.die(pid)

        assert tid in kbd.enforce_max_runtime(conn, signal_fn=_kill)

        # The acceptance criterion, stated once: no STALE row for this run.
        assert kp.pool_status()["reservations"] == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="zombie probe is /proc")
def test_an_unreaped_zombie_does_not_hold_a_slot(tmp_path):
    """Base behaviour: ``psutil.pid_exists`` calls a zombie alive."""
    from hermes_cli import kanban_db_dispatch as kbd

    _pool(tmp_path, card_keys=1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        kp.reserve_for_card("t_zombie", 1, pid=proc.pid)
        proc.kill()
        for _ in range(400):
            try:
                body = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
            except FileNotFoundError:  # pragma: no cover
                break
            if "State:\tZ" in body:
                break
            time.sleep(0.01)
        assert kp._pid_alive(proc.pid) is False
        assert kbd._keypool_has_free_slot() is True
    finally:
        proc.wait(timeout=10)
