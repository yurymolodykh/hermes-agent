"""Tests for the kanban worker turn-end stop guard.

Covers arming (full dispatcher worker envelope + delegated/cron exclusion +
authoritative live-card probe), the nudge text contract, and the phantom-loop
regression: a bare stale ``HERMES_KANBAN_TASK`` pointing at an already-done
card must NEVER arm the guard (t_6972f101).
"""

from __future__ import annotations

import sqlite3

import pytest

import agent.kanban_stop as ks
from agent.kanban_stop import (
    build_kanban_stop_nudge,
    is_dispatcher_worker_env,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)

_WORKER_ENV_VARS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_BOARD",
    "HERMES_PROFILE",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_ID",
    "HERMES_KANBAN_STOP_NUDGE",
)


@pytest.fixture
def clean_kanban_env(monkeypatch):
    for var in _WORKER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    ks._LIVE_RUNNING_CACHE.clear()
    return monkeypatch


def _full_worker_env(
    monkeypatch,
    task="t_live",
    run="42",
    profile="02-hermes-platform",
    sess="sess-worker-1",
    board_db="/tmp/board.sqlite",
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run)
    monkeypatch.setenv("HERMES_KANBAN_DB", board_db)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "C:/ws/" + task)
    monkeypatch.setenv("HERMES_PROFILE", profile)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("HERMES_SESSION_ID", sess)


def _stub_live(monkeypatch, value):
    monkeypatch.setattr(ks, "_card_is_live_running", lambda *a, **k: value)


# ── phantom-loop regression: bare / partial stale env must NOT arm ────────

def test_bare_stale_task_var_does_not_arm(clean_kanban_env):
    """Only HERMES_KANBAN_TASK set (the gateway stale-env shape) → no guard."""
    clean_kanban_env.setenv("HERMES_KANBAN_TASK", "t_e05caf9a")
    assert is_dispatcher_worker_env() is False
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_stale_var_with_session_profile_does_not_arm(clean_kanban_env):
    """The exact stale-gateway env (task + profile + session, no run envelope)."""
    clean_kanban_env.setenv("HERMES_KANBAN_TASK", "t_e05caf9a")
    clean_kanban_env.setenv("HERMES_PROFILE", "02-hermes-platform")
    clean_kanban_env.setenv("HERMES_SESSION_ID", "20260922_181001_c4087c")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_partial_envelope_does_not_arm(clean_kanban_env):
    """Missing any dispatch envelope key (e.g. no RUN_ID) → not a worker."""
    clean_kanban_env.setenv("HERMES_KANBAN_TASK", "t_live")
    clean_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "42")
    clean_kanban_env.setenv("HERMES_KANBAN_DB", "/tmp/b.sqlite")
    clean_kanban_env.setenv("HERMES_KANBAN_WORKSPACE", "C:/ws/t_live")
    clean_kanban_env.setenv("HERMES_SESSION_SOURCE", "kanban")
    # HERMES_PROFILE and HERMES_SESSION_ID missing
    assert is_dispatcher_worker_env() is False
    assert kanban_stop_nudge_enabled() is False


def test_non_kanban_session_source_does_not_arm(clean_kanban_env):
    _full_worker_env(clean_kanban_env)
    clean_kanban_env.setenv("HERMES_SESSION_SOURCE", "telegram")
    assert is_dispatcher_worker_env() is False
    assert kanban_stop_nudge_enabled() is False


# ── guard arming for a genuine live worker ────────────────────────────────

def test_live_running_card_arms_guard(clean_kanban_env):
    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, True)
    assert is_dispatcher_worker_env() is True
    assert kanban_stop_nudge_enabled() is True


def test_terminal_card_does_not_arm(clean_kanban_env):
    """Full envelope but card is done/blocked/archived → live probe False → no nudge."""
    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, False)
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_explicit_disable_wins_even_for_live_worker(clean_kanban_env):
    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, True)
    clean_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_delegated_child_context_does_not_arm(clean_kanban_env):
    from agent.delegation_context import delegated_child_context

    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, True)
    with delegated_child_context("child-sess"):
        assert kanban_stop_nudge_enabled() is False


# ── nudge text contract on a live worker ──────────────────────────────────

def test_env_can_disable(clean_kanban_env):
    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, True)
    clean_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clean_kanban_env):
    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, True)
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_live" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clean_kanban_env):
    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, True)
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_attempt_budget_exhausted(clean_kanban_env):
    _full_worker_env(clean_kanban_env)
    _stub_live(clean_kanban_env, True)
    assert build_kanban_stop_nudge(messages=[], attempts=2) is None


# ── authoritative live-card probe against a real board DB ─────────────────

def _make_board_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT, "
        "current_run_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, "
        "status TEXT, ended_at INTEGER, worker_session_id TEXT)"
    )
    return conn


class TestCardIsLiveRunning:
    def test_done_card_returns_false(self, tmp_path, monkeypatch):
        db = tmp_path / "board.sqlite"
        conn = _make_board_db(str(db))
        conn.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?)",
            [("t_old", "02-hermes-platform", "done", 7)],
        )
        conn.executemany(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
            [(7, "t_old", "02-hermes-platform", "completed", 1790000000, "sess-1")],
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(
            "hermes_cli.kanban_db.kanban_db_path", lambda board=None: db
        )
        assert (
            ks._card_is_live_running(
                "t_old", "7", "02-hermes-platform", "sess-1"
            )
            is False
        )

    def test_running_bound_card_returns_true(self, tmp_path, monkeypatch):
        db = tmp_path / "board.sqlite"
        conn = _make_board_db(str(db))
        conn.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?)",
            [("t_live", "02-hermes-platform", "running", 9)],
        )
        conn.executemany(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
            [(9, "t_live", "02-hermes-platform", "running", None, "sess-live")],
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(
            "hermes_cli.kanban_db.kanban_db_path", lambda board=None: db
        )
        assert (
            ks._card_is_live_running(
                "t_live", "9", "02-hermes-platform", "sess-live"
            )
            is True
        )

    def test_reclaimed_run_returns_false(self, tmp_path, monkeypatch):
        """Card re-running under a NEW run id → old run's binding is gone."""
        db = tmp_path / "board.sqlite"
        conn = _make_board_db(str(db))
        # current_run_id=11 (the new run), old run 10 ended.
        conn.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?)",
            [("t_reclaim", "02-hermes-platform", "running", 11)],
        )
        conn.executemany(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
            [
                (10, "t_reclaim", "02-hermes-platform", "completed", 1790000000, "sess-old"),
                (11, "t_reclaim", "02-hermes-platform", "running", None, "sess-new"),
            ],
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(
            "hermes_cli.kanban_db.kanban_db_path", lambda board=None: db
        )
        assert (
            ks._card_is_live_running(
                "t_reclaim", "10", "02-hermes-platform", "sess-old"
            )
            is False
        )
