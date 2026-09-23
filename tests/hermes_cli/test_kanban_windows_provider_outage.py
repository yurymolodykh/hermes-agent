"""Windows end-to-end subprocess exit classification and provider outage routing."""
import json
import os
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    yield home
    kb._windows_worker_processes.clear()
    kb._recent_worker_exits.clear()


@pytest.mark.windows_only
@pytest.mark.parametrize("code, kind", [
    (0, "clean_exit"), (1, "nonzero_exit"),
    (74, "provider_unavailable"), (75, "rate_limited"),
])
def test_windows_real_child_exit_status(board, code, kind):
    proc = subprocess.Popen([sys.executable, "-c", f"import sys; sys.exit({code})"],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    kb._windows_worker_processes[proc.pid] = proc
    assert proc.wait(timeout=10) == code
    assert proc.pid in kb.reap_worker_zombies()
    assert kb._classify_worker_exit(proc.pid) == (kind, code)


@pytest.mark.windows_only
def test_provider_outage_blocks_and_wakes_owner_not_respawn(board, monkeypatch):
    # A real exited worker, not a synthesized POSIX wait status. The same
    # blocked event is what the gateway notification subscription consumes.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="pinned", assignee="test",
                             model_override="gemma4:26b", provider_override="local")
        kb.add_notify_sub(conn, task_id=tid, platform="api_server",
                          chat_id="owner-session", delivery_mode="notify+wake")
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:test")
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(74)"],
                                creationflags=subprocess.CREATE_NO_WINDOW)
        kb._windows_worker_processes[proc.pid] = proc
        assert proc.wait(timeout=10) == 74
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (proc.pid, tid))
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        kb.reap_worker_zombies()
        assert kb.detect_crashed_workers(conn) == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.block_kind == "needs_input"
        assert task.consecutive_failures == 0
        assert "local/gemma4:26b" in task.last_failure_error
        assert "letovo/deepseek-v4-flash" in task.last_failure_error
        assert "Без вашего явного ответа" in task.last_failure_error
        rows = conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"])["provider_route"] == "local/gemma4:26b"
        subs = conn.execute("SELECT chat_id,delivery_mode,last_event_id FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchall()
        assert [(r["chat_id"], r["delivery_mode"]) for r in subs] == [("owner-session", "notify+wake")]
        assert rows[0]["kind"] == "blocked"
        assert kb.detect_crashed_workers(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()[0] == 1


def test_exhausted_transport_exit_code_only_on_kanban(board):
    from cli import _kanban_failure_exit_code
    for reason in ("timeout", "server_error", "overloaded", "unknown"):
        result = {"failed": True, "failure_reason": reason, "failure_retryable": True}
        assert _kanban_failure_exit_code(result, True) == 74
        assert _kanban_failure_exit_code(result, False) == 1
        result["failure_retryable"] = False
        assert _kanban_failure_exit_code(result, True) == 1
    assert _kanban_failure_exit_code({"failed": True, "failure_reason": "rate_limit"}, True) == 75
    assert _kanban_failure_exit_code({"failed": False}, True) == 0


def test_windows_memory_sample_and_default_cap(board):
    from gateway.lifecycle_ledger import sample_memory
    sample = sample_memory()
    if os.name == "nt":
        assert sample["mem_total_kib"] > 0
        assert sample["mem_available_kib"] > 0
        assert kb.derive_default_max_in_progress(sample) is not None
    assert kb.derive_default_max_in_progress({"mem_total_kib": 4 * 1024 * 1024}) == 8
