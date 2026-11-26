"""deploy-wave2 refuses the restart when it would cost something it cannot give back.

26.11.2026 03:19: a restart fired while admitted runs were mid-turn and dropped their work; a
worker-launched restart would kill the gateway that spawned it. The wave-2 script therefore REFUSES
before touching systemd unless it can prove: it is not a worker, nothing is running, the window is
confirmed. These tests pin every refusal, and pin that a refusal never reaches ``systemctl``.

The guards themselves live IN THE REPOSITORY (``ops/deploy-wave2.py``): a file outside version
control cannot be reviewed by diff or rolled back, which is exactly the state that let the 03:19
restart ship unchecked. The old path keeps a thin wrapper so existing callers do not break — the
wrapper is pinned here too, so it can never drift away from the canonical file.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "deploy-wave2.py"
LEGACY_WRAPPER = Path("/home/hermes/workspaces/01-orchestrator/ops/bin/deploy-wave2.py")
LEGACY_CANONICAL_COPY = LEGACY_WRAPPER.with_name("deploy-wave2.canonical.py")
MSK = timezone(timedelta(hours=3))


@pytest.fixture(scope="module")
def wave():
    assert SCRIPT.is_file(), f"the wave-2 script must exist at {SCRIPT}"
    spec = importlib.util.spec_from_file_location("deploy_wave2", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _not_a_worker_parent(monkeypatch):
    """Keep the suite honest when pytest itself is launched BY a kanban worker.

    ``worker_context()`` also looks at the parent process, so running these tests from inside a
    worker made every unrelated refusal test hit the worker gate first (exit 3 instead of 4/3-with-
    -window). The parent marker is neutralised here; the worker tests set the ENV markers, which is
    the other, independent path into the same guard and stays fully exercised.
    """
    monkeypatch.setattr("os.getppid", lambda: 1)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)


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


def test_a_worker_parent_process_is_refused_without_any_env_marker(wave, monkeypatch, capsys):
    """The second, independent signal: the launching process IS a kanban worker.

    No env marker is set here, only the parent's cmdline carries the marker — the shape of a
    ``subprocess`` call from inside a worker. The guard must refuse before systemd.
    """
    real_open = builtins.open

    class _Cmdline:
        def __init__(self, data: bytes):
            self._data = data

        def read(self, *_a):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_open(path, *args, **kwargs):
        if str(path).startswith("/proc/") and str(path).endswith("/cmdline"):
            return _Cmdline(b"python3\x00/home/hermes/.hermes/hermes-agent/hermes_cli/kanban_worker_client.py\x00")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    _no_systemctl(monkeypatch)
    assert wave.worker_context() == "родительский процесс — kanban_worker_client"
    assert wave.main([]) == 3
    assert "запуск из kanban-воркера запрещён" in capsys.readouterr().out


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


# --- the run gate never writes ----------------------------------------------------------

def test_the_run_gate_never_writes(wave, monkeypatch, board_db):
    conn = sqlite3.connect(board_db)
    conn.execute("INSERT INTO tasks VALUES ('t_live', 'running', ?)", (__import__("os").getpid(),))
    conn.commit()
    conn.close()
    before = board_db.read_bytes()
    wave.live_runs()
    assert board_db.read_bytes() == before


# --- the guards live under version control, and the old path still reaches them -----------

def test_the_script_is_in_the_repository_not_outside_it():
    """A file only on disk cannot be reviewed or rolled back: the guards must be a repo file."""
    assert SCRIPT.is_file(), f"the wave-2 guards must live in the repository at {SCRIPT}"
    rel = SCRIPT.relative_to(REPO_ROOT)
    tracked = __import__("subprocess").run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(rel)],
        capture_output=True, text=True)
    assert tracked.returncode == 0, f"ops/deploy-wave2.py must be git-tracked, got {tracked.stderr}"
    assert "def worker_context" in SCRIPT.read_text(encoding="utf-8")


def test_the_old_path_is_a_wrapper_that_reaches_the_repository_file():
    """Existing systemd-run/task callers keep working, and the old path cannot be edited stale."""
    if not LEGACY_WRAPPER.is_file():
        pytest.skip("the legacy 01-orchestrator path does not exist on this host")
    text = LEGACY_WRAPPER.read_text(encoding="utf-8")
    assert "def worker_context" not in text, \
        "the legacy path must be a wrapper, not a second copy of the guards"
    assert "ops" in text and "deploy-wave2.py" in text
    assert "main(" in text or "execv" in text, "the wrapper must actually hand over to the repo file"


def test_the_legacy_copy_matches_the_repository_file_byte_for_byte():
    """If a canonical copy was installed next to the wrapper, it may never silently diverge."""
    if not LEGACY_CANONICAL_COPY.is_file():
        pytest.skip("no canonical copy installed next to the legacy wrapper")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    assert digest(LEGACY_CANONICAL_COPY) == digest(SCRIPT), \
        "deploy-wave2.canonical.py drifted from ops/deploy-wave2.py: reinstall the copy"
