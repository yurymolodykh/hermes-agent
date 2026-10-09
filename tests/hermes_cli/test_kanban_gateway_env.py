"""Unit tests for the gateway kanban-worker env sanitizer (t_1b5b0e48).

The gateway is never a kanban worker.  A stale HERMES_KANBAN_TASK /
HERMES_KANBAN_RUN_ID inherited from a worker-session launcher chain makes
every gateway-hosted agent session try (and fail) a KANBAN_BIND in
agent_init.  ``hermes_cli.kanban_gateway_env`` strips the worker identity at
``gateway run`` entry while preserving the gateway's own dispatch pin
(HERMES_KANBAN_BOARD / *_HOME / *_DB / *_WORKSPACES_ROOT).
"""

import logging
import os

from hermes_cli.kanban_gateway_env import (
    WORKER_IDENTITY_ENV_VARS,
    sanitized_child_env,
    strip_gateway_worker_env,
)


def test_strip_removes_worker_identity_and_keeps_gateway_pin(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "prod-board")
    monkeypatch.setenv("HERMES_KANBAN_HOME", r"C:\kanban\home")
    monkeypatch.setenv("HERMES_KANBAN_DB", r"C:\kanban\state.db")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", r"C:\kanban\ws")

    result = strip_gateway_worker_env(log_warning=False)

    assert "HERMES_KANBAN_TASK" not in os.environ
    assert "HERMES_KANBAN_RUN_ID" not in os.environ
    # gateway dispatch pin must survive
    assert os.environ.get("HERMES_KANBAN_BOARD") == "prod-board"
    assert os.environ.get("HERMES_KANBAN_HOME") == r"C:\kanban\home"
    assert os.environ.get("HERMES_KANBAN_DB") == r"C:\kanban\state.db"
    assert os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT") == r"C:\kanban\ws"
    assert result["dropped"] == {
        "HERMES_KANBAN_TASK": "t_abc123",
        "HERMES_KANBAN_RUN_ID": "42",
    }


def test_strip_is_noop_when_env_clean(monkeypatch):
    for var in WORKER_IDENTITY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    result = strip_gateway_worker_env(log_warning=False)
    assert result["dropped"] == {}
    assert result["source"] == {}


def test_strip_is_idempotent(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    strip_gateway_worker_env(log_warning=False)
    assert "HERMES_KANBAN_TASK" not in os.environ
    second = strip_gateway_worker_env(log_warning=False)
    assert second["dropped"] == {}


def test_sanitized_child_env(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "prod-board")
    env = sanitized_child_env()
    assert "HERMES_KANBAN_TASK" not in env
    assert "HERMES_KANBAN_RUN_ID" not in env
    assert env["HERMES_KANBAN_BOARD"] == "prod-board"
    # os.environ itself untouched
    assert os.environ.get("HERMES_KANBAN_TASK") == "t_abc123"


def test_warning_emitted_with_env_source(caplog, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_warn")
    with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway"):
        strip_gateway_worker_env(log_warning=True)
    messages = [r.message for r in caplog.records]
    assert any("KANBAN_ENV_SANITIZE" in m for m in messages)
    assert any("t_warn" in m for m in messages)
    # env-source is captured when a parent process exists
    assert any("env-source" in m for m in messages)
