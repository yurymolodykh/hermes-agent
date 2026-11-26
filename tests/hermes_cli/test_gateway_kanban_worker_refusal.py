"""Tests: a kanban worker must never start a second gateway owner.

Incident 26.11: a card's worker (``-m agent.managed_worker``) spawned
``hermes gateway run``. Because the dispatcher hands its own environment to every
worker it spawns, the worker carried the live gateway's supervisor markers
(``INVOCATION_ID`` / ``HERMES_SUPERVISED_CHILD``). ``_running_under_gateway_supervisor()``
reads exactly those markers, so inside the worker every conflict guard returned
early and the worker was free to bind the same ``HERMES_HOME`` and ``state.db`` as
the live gateway (pid 2125256 beside pid 1999484, both serving :9120).

The worker's own identity is ``HERMES_KANBAN_TASK`` — a marker the live gateway
never carries — so that is what the refusal keys on.
"""

from __future__ import annotations

import pytest

from hermes_cli import gateway as gateway_cli


@pytest.fixture
def no_supervisor_markers(monkeypatch):
    """Make the inherited-marker premise explicit: the worker looks supervised."""
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)


def test_refuses_gateway_run_from_kanban_worker(no_supervisor_markers, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_aa6015fa")
    with pytest.raises(SystemExit) as exc:
        gateway_cli._guard_kanban_worker_gateway()
    assert exc.value.code == 78


def test_refuses_on_workspace_marker_without_task_id(monkeypatch):
    """A worker's shell keeps HERMES_KANBAN_WORKSPACE when the task id was consumed.

    Measured on the live host: the incident's worker shell carried
    HERMES_KANBAN_WORKSPACE and HERMES_KANBAN_BOARD but no HERMES_KANBAN_TASK, so a
    guard keyed on the task id alone would have let the second gateway start.
    """
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE",
                       "/home/hermes/.hermes/kanban/workspaces/t_43e43ad3")
    with pytest.raises(SystemExit) as exc:
        gateway_cli._guard_kanban_worker_gateway()
    assert exc.value.code == 78


def test_the_live_gateway_environment_is_allowed(monkeypatch):
    """The real gateway carries NO HERMES_KANBAN_* key — verified on pid 2144916."""
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    gateway_cli._guard_kanban_worker_gateway()  # the supervised gateway itself: allowed


def test_refusal_is_not_bound_to_the_supervisor_markers(no_supervisor_markers, monkeypatch):
    """Without a kanban marker the guard is silent, however supervised it looks."""
    gateway_cli._guard_kanban_worker_gateway()  # returns, no SystemExit


def test_plain_shell_run_is_allowed(monkeypatch):
    """No kanban marker at all: the operator's own ``gateway run`` still works."""
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                 "INVOCATION_ID", "HERMES_SUPERVISED_CHILD"):
        monkeypatch.delenv(name, raising=False)
    gateway_cli._guard_kanban_worker_gateway()


def test_blank_markers_are_not_a_worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "   ")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "\t")
    gateway_cli._guard_kanban_worker_gateway()


def test_guard_runs_before_any_conflict_guard_in_cmd_run(monkeypatch):
    """The refusal must precede the guards that the inherited markers neutralize."""
    calls: list[str] = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setattr(gateway_cli, "_guard_kanban_worker_gateway",
                        lambda: calls.append("kanban") or (_ for _ in ()).throw(SystemExit(78)))
    monkeypatch.setattr(gateway_cli, "_maybe_redirect_run_to_s6_supervision",
                        lambda args: calls.append("s6") or False)
    with pytest.raises(SystemExit):
        gateway_cli._cmd_run(object())
    assert calls == ["kanban"]


def test_guard_runs_first_inside_run_gateway(monkeypatch):
    calls: list[str] = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setattr(gateway_cli, "_guard_kanban_worker_gateway",
                        lambda: calls.append("kanban") or (_ for _ in ()).throw(SystemExit(78)))
    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway",
                        lambda: calls.append("docker"))
    with pytest.raises(SystemExit):
        gateway_cli.run_gateway()
    assert calls == ["kanban"]
