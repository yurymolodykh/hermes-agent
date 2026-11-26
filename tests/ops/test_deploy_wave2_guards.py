"""deploy-wave2 refuses the restart when it would cost something it cannot give back.

26.11.2026 03:19: a restart fired while admitted runs were mid-turn and dropped their work; a
worker-launched restart would kill the gateway that spawned it. The wave-2 script therefore REFUSES
before touching systemd unless it can prove: it is not a worker, nothing is running, the window is
confirmed. These tests pin every refusal, and pin that a refusal never reaches ``systemctl``.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path("/home/hermes/workspaces/01-orchestrator/ops/bin/deploy-wave2.py")
MSK = timezone(timedelta(hours=3))


@pytest.fixture(scope="module")
def wave():
    assert SCRIPT.is_file(), f"the wave-2 script must exist at {SCRIPT}"
    spec = importlib.util.spec_from_file_location("deploy_wave2", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def board_db(tmp_path, monkeypatch, wave):
    """An isolated board with one table, so no test reads the live ``kanban.db``."""
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, worker_pid INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(wave, "KANBAN_DB", str(path))
    return path


def _window(tmp_path, until):
    path = tmp_path / "window.txt"
    path.write_text(until.isoformat() + "\n")
    return str(path)


def _no_systemctl(monkeypatch):
    """Any systemctl invocation fails the test: a refused run must not touch the gateway."""
    def boom(*_a, **_kw):
        raise AssertionError("a refused wave must never call systemctl")
    monkeypatch.setattr("subprocess.run", boom)


# --- 1. never from inside a worker ----------------------------------------------------------------

def test_a_worker_launched_run_is_refused(wave, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_7b2f251e")
    _no_systemctl(monkeypatch)
    assert wave.main([]) == 3
    assert "запуск из kanban-воркера запрещён" in capsys.readouterr().out


def test_every_worker_marker_refuses(wave, monkeypatch):
    for marker in wave._WORKER_ENV:
        monkeypatch.setenv(marker, "x")
        _no_systemctl(monkeypatch)
        assert wave.main([]) == 3, f"{marker} alone must refuse the run"
        monkeypatch.delenv(marker)


# --- 2. never while admitted runs are alive -------------------------------------------------------

def test_live_runs_refuse_the_restart(wave, monkeypatch, board_db, capsys):
    conn = sqlite3.connect(board_db)
    conn.execute("INSERT INTO tasks VALUES ('t_live', 'running', ?)", (__import__("os").getpid(),))
    conn.commit()
    conn.close()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    _no_systemctl(monkeypatch)

    assert wave.main([]) == 4
    out = capsys.readouterr().out
    assert "1 живых прогонов" in out and "t_live" in out


def test_an_orphaned_running_row_does_not_block(wave, monkeypatch, board_db):
    """A dead PID means nothing can be interrupted, so the wave may proceed past this gate."""
    conn = sqlite3.connect(board_db)
    conn.execute("INSERT INTO tasks VALUES ('t_dead', 'running', 3999999)")
    conn.commit()
    conn.close()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    _no_systemctl(monkeypatch)

    # Passes the run gate, then refuses for the missing window — never a restart.
    assert wave.main([]) == 3


def test_an_unreadable_board_refuses_rather_than_guesses(wave, monkeypatch, tmp_path):
    monkeypatch.setattr(wave, "KANBAN_DB", str(tmp_path / "missing.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    _no_systemctl(monkeypatch)
    assert wave.main([]) == 4


# --- 3. never without a confirmed window ----------------------------------------------------------

def test_a_missing_window_refuses(wave, monkeypatch, board_db):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    _no_systemctl(monkeypatch)
    assert wave.main([]) == 3


def test_an_expired_window_refuses(wave, monkeypatch, board_db, tmp_path, capsys):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    _no_systemctl(monkeypatch)
    past = _window(tmp_path, datetime.now(MSK) - timedelta(hours=1))
    assert wave.main(["--window", past]) == 3
    assert "окно истекло" in capsys.readouterr().out


def test_a_future_window_is_accepted(wave, monkeypatch, board_db, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    future = _window(tmp_path, datetime.now(MSK) + timedelta(hours=2))
    assert wave.window_ok(future) is None


def test_a_garbage_marker_is_not_a_window(wave, tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("не дата\n")
    assert wave.window_ok(str(path)) is not None


# --- the guards read the board read-only ----------------------------------------------------------

def test_the_run_gate_never_writes(wave, monkeypatch, board_db):
    conn = sqlite3.connect(board_db)
    conn.execute("INSERT INTO tasks VALUES ('t_live', 'running', ?)", (__import__("os").getpid(),))
    conn.commit()
    conn.close()
    before = board_db.read_bytes()
    wave.live_runs()
    assert board_db.read_bytes() == before
