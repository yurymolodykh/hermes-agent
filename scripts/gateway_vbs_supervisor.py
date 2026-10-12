#!/usr/bin/env python3
"""Versioned contract for the Windows gateway VBS-supervisor (fast-gateway-restart F2).

Production launch chain (this host, profile 01-orchestrator)::

    wscript Hermes_Gateway_01-orchestrator.vbs
        -> cmd.exe /c safe_gateway_start.cmd
        -> python -m hermes_cli.main --profile 01-orchestrator gateway run

F2 observed defect (48 h observation window, closing 2026-10-12): the VBS
wrapper spawns the child with ``sh.Run(..., 0, False)`` — ``bWaitOnReturn=False``
— so it never sees the gateway's exit code.  When the gateway exits 75
(EX_TEMPFAIL, the service-manager "restart me" contract), the supervisor cannot
act on it; the window saw 4 external kill-restarts that never carried exit-75
semantics.  ``safe_gateway_start.cmd`` also unconditionally launches ``gateway
run``, so a fast restart can spawn a duplicate that fights the gateway already
bound to port 8642.

This module is the VERSIONED, unit-testable core of that policy.  The thin VBS
/cmd deployment wrappers (in the profile's ``gateway-service/``) delegate to it:

* ``bWaitOnReturn=True`` in the VBS wrapper (so the parent observes the child's
  exit code) — the VBS change is one boolean; the *decision* logic lives here.
* ``gateway_port_guard`` — the duplicate-start guard for port 8642.
* ``decide_restart`` / ``supervisor_run_plan`` — the *bounded* exit-75 restart
  policy (restart-loop protection): at most ``max_restarts`` restarts per
  invocation, then stop.  A permanent (non-75) exit never restarts.

Testing: ``tests/scripts/test_gateway_vbs_supervisor.py``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# EX_TEMPFAIL from sysexits.h — the service-manager "restart me" contract used
# by the gateway's graceful restart path (see gateway/restart.py).
EX_TEMPFAIL = 75
EX_TEMPFAIL_NAME = "EX_TEMPFAIL"

# GATEWAY_FATAL_CONFIG_EXIT_CODE — a permanent config failure must never be
# masked by an exit-75 restart loop.
EX_CONFIG = 78

DEFAULT_MAX_RESTARTS = 3
GATEWAY_PORT = 8642


def decide_restart(exit_code: int, *, restarts_so_far: int, max_restarts: int) -> str:
    """Return the restart decision for one observed child exit code.

    - ``"restart"`` — exit code is 75 (EX_TEMPFAIL → the gateway asked to be
      restarted) AND the bounded budget still has headroom.
    - ``"stop"`` — clean exit (0), permanent failure (anything ≠ 75), or the
      restart budget is exhausted (restart-loop protection).
    """
    if exit_code == EX_TEMPFAIL and restarts_so_far < max_restarts:
        return "restart"
    return "stop"


def supervisor_run_plan(
    exit_sequence: list[int],
    *,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
) -> dict:
    """Simulate a supervisor observing a sequence of child exit codes.

    Walk ``exit_sequence`` as if each element were the return code of one
    gateway launch.  A 75 (with budget headroom) consumes one restart and the
    next code is observed; the loop ends on a clean/permanent exit or when the
    restart budget is exhausted (bounded — it can never spin forever).

    Returns a dict with ``restarts`` (number actually fired), ``stopped``,
    ``stop_reason``, ``observed_exit_codes`` and ``final_exit_code``.
    """
    restarts = 0
    observed: list[int] = []
    reason = "budget_exhausted_at_end"
    for code in exit_sequence:
        observed.append(int(code))
        decision = decide_restart(
            int(code), restarts_so_far=restarts, max_restarts=max_restarts
        )
        if decision == "restart":
            restarts += 1
            continue  # next launch observes the next code
        if int(code) == 0:
            reason = "clean_exit"
        elif int(code) == EX_TEMPFAIL:
            reason = "max_restarts_exceeded"
        else:
            reason = "permanent_exit"
            if int(code) == EX_CONFIG:
                reason = "config_fatal"
        return {
            "restarts": restarts,
            "stopped": True,
            "stop_reason": reason,
            "observed_exit_codes": observed,
            "final_exit_code": int(code),
        }
    # Exit sequence ended without a terminal code (all 75 with headroom to
    # spare, sequence just ended) — treat as stopped-at-end-of-observation.
    return {
        "restarts": restarts,
        "stopped": True,
        "stop_reason": "observation_end",
        "observed_exit_codes": observed,
        "final_exit_code": EX_TEMPFAIL if observed else 0,
    }


def gateway_port_guard(
    port: int = GATEWAY_PORT,
    *,
    port_bound: bool,
    health_ok: bool,
) -> str:
    """Return a non-empty conflict message if a SECOND gateway would be a
    duplicate of a live one, otherwise the empty string (safe to start).

    ``port_bound`` — True when something is listening on loopback ``port``.
    ``health_ok`` — True when the gateway health endpoint reports healthy.

    The duplicate-start guard: when port ``port`` is both bound AND healthy, a
    new ``gateway run`` would fight the running instance — the caller must NOT
    start a duplicate (return a hard conflict).  When bound but unhealthy
    (starting / stale / draining), we still warn: the bounded cooldown in the
    caller is what prevents a restart storm.
    """
    if not port_bound:
        return ""
    if health_ok:
        return (
            f"port {port} is already bound and healthy — a live gateway is "
            "running; refusing to start a duplicate (fast-gateway-restart F2)"
        )
    return (
        f"port {port} is bound but the gateway health probe is not OK on it — "
        "a previous instance may still be starting/draining; apply a bounded "
        "cooldown before relaunching"
    )


if __name__ == "__main__":  # pragma: no cover - smoke / self-check
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--guard-port",
        type=int,
        default=None,
        metavar="PORT",
        help="Emulate the safe_gateway_start.cmd duplicate-start preflight: "
        "print a conflict and exit 3 when a healthy gateway is already on "
        "PORT, otherwise print OK and exit 0.",
    )
    args = parser.parse_args()
    if args.guard_port is not None:
        try:
            from hermes_cli.browser_connect import local_port_in_use
            import urllib.request

            port = int(args.guard_port)
            bound = local_port_in_use(port)
            healthy = False
            if bound:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2
                    ) as resp:
                        healthy = resp.status == 200
                except Exception:
                    healthy = False
            msg = gateway_port_guard(port, port_bound=bound, health_ok=healthy)
            if msg:
                print(msg)
                raise SystemExit(3)
            print(f"OK: port {port} is free (or not healthy) — safe to start")
            raise SystemExit(0)
        except SystemExit:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            print(f"port guard error: {exc}")
            raise SystemExit(0)  # never hard-block the launcher on probe error
    plan = supervisor_run_plan([75, 75, 75, 75], max_restarts=2)
    print("restarts:", plan["restarts"], "| stopped:", plan["stopped"], "|", plan["stop_reason"])
    print("port-guard(8642 bound+healthy):", gateway_port_guard(8642, port_bound=True, health_ok=True) or "(none)")
