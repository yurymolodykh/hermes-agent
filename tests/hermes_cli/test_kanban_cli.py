"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The CLI tests exercise board behavior against an isolated filesystem,
    # not the host profile registry.  Profile validation is covered separately.
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_list_hides_done_tasks_at_or_past_configured_retention(
    kanban_home, monkeypatch
):
    """Default list uses the configured inclusive done-retention boundary."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    now = 1_700_000_000
    assert DEFAULT_CONFIG["kanban"]["list_done_retention_hours"] == 12.0
    with kb.connect() as conn:
        stale_done = kb.create_task(conn, title="stale done")
        exact_done = kb.create_task(conn, title="12 hour done")
        recent_done = kb.create_task(conn, title="recent done")
        null_done = kb.create_task(conn, title="undated done")
        ready = kb.create_task(conn, title="ready")
        blocked = kb.create_task(conn, title="blocked")
        kb.complete_task(conn, stale_done, result="old")
        kb.complete_task(conn, exact_done, result="boundary")
        kb.complete_task(conn, recent_done, result="new")
        kb.complete_task(conn, null_done, result="legacy")
        kb.block_task(conn, blocked, reason="needs input")
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 12 * 60 * 60 - 1, stale_done),
        )
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 12 * 60 * 60, exact_done),
        )
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - (11 * 60 + 59) * 60, recent_done),
        )
        conn.execute("UPDATE tasks SET completed_at = NULL WHERE id = ?", (null_done,))
        conn.commit()

    monkeypatch.setattr(kc.time, "time", lambda: now)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    default_titles = {row["title"] for row in json.loads(kc.run_slash("list --json"))}
    done_titles = {
        row["title"] for row in json.loads(kc.run_slash("list --status done --json"))
    }
    historical_titles = {
        row["title"] for row in json.loads(kc.run_slash("list --historical --json"))
    }

    assert "stale done" not in default_titles
    assert {"recent done", "undated done", "ready", "blocked"} <= default_titles
    assert "12 hour done" not in default_titles
    assert {"stale done", "12 hour done", "recent done", "undated done"} == done_titles
    assert {"stale done", "12 hour done", "recent done", "undated done", "ready", "blocked"} <= historical_titles

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"list_done_retention_hours": 0.5}},
    )
    override_titles = {
        row["title"] for row in json.loads(kc.run_slash("list --json"))
    }
    assert "recent done" not in override_titles
    assert "undated done" in override_titles

    with kb.connect() as conn:
        assert kb.get_task(conn, stale_done).status == "done"
        assert kb.get_task(conn, stale_done).completed_at == now - 12 * 60 * 60 - 1


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()


# ---------------------------------------------------------------------------
# do_not_deliver regression (t_1e52d756): the CLI --do-not-deliver flag marks
# a synthetic card; ordinary creates never set it.
# ---------------------------------------------------------------------------


def test_kanban_cli_do_not_deliver_flag_marks_synthetic_card(kanban_home):
    """``--do-not-deliver`` on the create subcommand flags the new card.

    Synthetic readback cards created through the CLI carry the flag so
    terminal/branch-summary routing can skip them.
    """
    import re
    from hermes_cli import kanban_db as kb

    out = kc.run_slash(
        "create '[F7 readback] mission A (host stays open)' "
        "--assignee alice --do-not-deliver"
    )
    m = re.search(r"(t_[a-f0-9]+)", out)
    assert m, f"create must print a task id; got {out!r}"
    tid = m.group(1)

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.do_not_deliver is True, task.do_not_deliver
        row = conn.execute(
            "SELECT do_not_deliver FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["do_not_deliver"] == 1


def test_kanban_cli_create_without_flag_leaves_do_not_deliver_zero(kanban_home):
    """Ordinary ``/kanban create`` never sets do_not_deliver (default 0)."""
    import re
    from hermes_cli import kanban_db as kb

    out = kc.run_slash("create 'ordinary card' --assignee alice")
    m = re.search(r"(t_[a-f0-9]+)", out)
    assert m, f"create must print a task id; got {out!r}"
    tid = m.group(1)

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.do_not_deliver is False, task.do_not_deliver
        row = conn.execute(
            "SELECT do_not_deliver FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["do_not_deliver"] == 0




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------




def test_cli_create_without_budget_flags_gets_board_defaults(kanban_home):
    """t_ed744fd2: `kanban create` with no budget flags gets the board default.

    The limit must be a property of the CARD (DB row), not of the caller — this
    is the CLI surface of ``kanban.default_max_runtime_seconds`` /
    ``default_max_turns``.
    """
    import re
    from hermes_cli import kanban_db as kb

    out = kc.run_slash("create 'default budget card' --assignee alice")
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.max_runtime_seconds == 3 * 60 * 60
        assert task.max_turns == 300


def test_cli_create_with_max_turns_flag_overrides_the_default(kanban_home):
    """`--max-turns` is the per-card override the docs promise."""
    import re
    from hermes_cli import kanban_db as kb

    out = kc.run_slash(
        "create 'big card' --assignee alice --max-runtime 8h --max-turns 600"
    )
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.max_runtime_seconds == 8 * 60 * 60
        assert task.max_turns == 600
