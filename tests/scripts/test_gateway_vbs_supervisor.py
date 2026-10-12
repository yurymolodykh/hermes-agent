"""Tests for the versioned gateway VBS-supervisor exit-code contract (F2).

Background (fast-gateway-restart follow-up F2, t_60a7f508): the production
Windows launcher chain is ``wscript Hermes_Gateway_01-orchestrator.vbs ->
safe_gateway_start.cmd -> python -m hermes_cli.main gateway run``. Two defects
were observed in the 48 h observation window:

1. The VBS wrapper launched with ``Run(..., 0, False)`` — ``bWaitOnReturn=False``
   — so it never observed the child gateway's exit code. When the gateway exits
   with 75 (EX_TEMPFAIL, the service-manager "restart me" contract), the parent
   could not see it, and 4 external kill-restarts happened without exit-75
   semantics.

2. ``safe_gateway_start.cmd`` unconditionally runs ``gateway run`` even when
   port 8642 is already bound (a live gateway), so a lightning restart can
   spawn a duplicate that fights the running instance.

This module versioned at ``scripts/gateway_vbs_supervisor.py`` encodes the
correct contract in a *testable* pure-Python core:

- ``decide_restart(exit_code, restarts_so_far, max_restarts)`` — the bounded
  exit-75 restart-loop policy: 75 with headroom → schedule a restart; 0 →
  clean exit, stop; other codes → stop (do not restart on permanent failure).
- ``supervisor_run_plan(exit_sequence, max_restarts)`` — walk a sequence of
  child exit codes and return how many restarts would fire before the loop
  stops (clean exit, permanent code, or the restart budget is exhausted).
- ``gateway_port_guard(port, bindable, health_ok)`` — the duplicate-start
  guard: refuse to launch when the port is already bound and healthy, so a
  second launcher cannot spawn a twin gateway.

The production VBS/cmd deployment artifacts are thin wrappers over this policy
(see ``scripts/fast-restart-e2e/README.md`` and the deploy note in
``scripts/gateway_vbs_supervisor.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "gateway_vbs_supervisor.py"
EX_TEMPFAIL = 75


@pytest.fixture(scope="module")
def sup():
    """Load the versioned supervisor module once (it is not a package)."""
    assert MODULE_PATH.exists(), MODULE_PATH
    spec = importlib.util.spec_from_file_location(
        "gateway_vbs_supervisor", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gateway_vbs_supervisor"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_module_defines_exit_75_contract(sup):
    assert sup.EX_TEMPFAIL == EX_TEMPFAIL
    assert sup.EX_TEMPFAIL_NAME == "EX_TEMPFAIL"


# ---------------------------------------------------------------------------
# Bounded exit-75 restart policy
# ---------------------------------------------------------------------------


def test_exit_0_clean_no_restart(sup):
    assert sup.decide_restart(0, restarts_so_far=0, max_restarts=2) == "stop"


def test_exit_75_with_headroom_restarts(sup):
    assert sup.decide_restart(75, restarts_so_far=0, max_restarts=2) == "restart"
    assert sup.decide_restart(75, restarts_so_far=1, max_restarts=2) == "restart"


def test_exit_75_budget_exhausted_stops(sup):
    assert sup.decide_restart(75, restarts_so_far=2, max_restarts=2) == "stop"


def test_permanent_code_never_restarts(sup):
    assert sup.decide_restart(78, restarts_so_far=0, max_restarts=2) == "stop"
    assert sup.decide_restart(1, restarts_so_far=0, max_restarts=2) == "stop"
    assert sup.decide_restart(0, restarts_so_far=5, max_restarts=2) == "stop"


def test_restart_loop_is_bounded_plan(sup):
    """A child that always exits 75 must restart at most max_restarts times,
    then stop (the loop is protected — it cannot spin forever)."""
    plan = sup.supervisor_run_plan(
        exit_sequence=[75, 75, 75, 75, 75],
        max_restarts=2,
    )
    assert plan["restarts"] == 2
    assert plan["stopped"] is True
    assert plan["stop_reason"] == "max_restarts_exceeded"


def test_restart_then_clean_exit_stops_early(sup):
    plan = sup.supervisor_run_plan(exit_sequence=[75, 75, 0, 75], max_restarts=3)
    assert plan["restarts"] == 2
    assert plan["stopped"] is True
    assert plan["stop_reason"] == "clean_exit"


def test_permanent_code_stops_immediately(sup):
    plan = sup.supervisor_run_plan(exit_sequence=[75, 1], max_restarts=3)
    assert plan["restarts"] == 1
    assert plan["stopped"] is True
    assert plan["stop_reason"] == "permanent_exit"


def test_config_code_stops_immediately(sup):
    plan = sup.supervisor_run_plan(exit_sequence=[75, 78], max_restarts=3)
    assert plan["restarts"] == 1
    assert plan["stopped"] is True
    assert plan["stop_reason"] == "config_fatal"


def test_supervisor_run_plan_reports_observed_codes(sup):
    plan = sup.supervisor_run_plan(exit_sequence=[75, 0], max_restarts=2)
    assert plan["observed_exit_codes"] == [75, 0]
    assert plan["final_exit_code"] == 0


# ---------------------------------------------------------------------------
# Duplicate-start guard (port 8642)
# ---------------------------------------------------------------------------


def test_port_guard_empty_when_free(sup):
    assert sup.gateway_port_guard(8642, port_bound=False, health_ok=False) == ""


def test_port_guard_reports_conflict_when_healthy(sup):
    # 8642 already accepting + healthy gateway → refuse to start a duplicate.
    msg = sup.gateway_port_guard(8642, port_bound=True, health_ok=True)
    assert "8642" in msg
    assert "running" in msg.lower()


def test_port_guard_allows_when_unhealthy_stale(sup):
    # Port is FREE (not bound) → safe to start; no warning at all.
    msg = sup.gateway_port_guard(8642, port_bound=False, health_ok=False)
    assert msg == ""


def test_port_guard_warns_on_bound_but_unhealthy_port(sup):
    # Port bound but health dead → stale/starting gateway; do not hard-fail
    # (bounded cooldown is the caller's job), but still warn.
    msg = sup.gateway_port_guard(8642, port_bound=True, health_ok=False)
    assert "8642" in msg


def test_port_guard_happy_healthy_refuses(sup):
    msg = sup.gateway_port_guard(8642, port_bound=True, health_ok=True)
    assert msg  # must not be empty when a healthy twin would be spawned
