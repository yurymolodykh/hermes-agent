"""Schedule a gateway restart OUTSIDE the calling process's tree (OPS-12).

A gateway restart kills every process in the service cgroup (``KillMode=mixed``) and then
``ExecStopPost=gateway.cgroup_cleanup`` SIGKILLs whatever is left. A Kanban worker that restarts
the gateway therefore SIGKILLs *itself* (exit 130, "owner admission settled interrupted") in the
middle of its own card — and every sibling worker with it.

A restart issued from inside a worker must not run in that tree. ``systemd-run --user --on-active``
creates a transient timer owned by the user manager, so the restart command runs after the caller
is gone, in its own unit, outside any cgroup the stop tears down. The worker only *schedules* it
and exits its turn cleanly, so its card keeps its work.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Optional

# A transient timer needs a moment to be registered by the user manager before the caller may exit;
# 5s is also long enough that the scheduling call returns while the gateway is still healthy.
DEFAULT_DELAY_SECONDS = 5


class DeferredRestartUnavailable(RuntimeError):
    """No way to schedule the restart outside this process's tree on this host."""


def in_gateway_cgroup() -> bool:
    """True when this process runs inside the gateway's service cgroup.

    A restart from here tears the caller down with it, so it must be deferred. The check is the
    systemd unit of our own cgroup — not an env marker, which a CLI legitimately inherits.
    """
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return False
    return "hermes-gateway.service" in text


def _systemd_run_available() -> bool:
    """``systemd-run --user`` must actually reach the user manager, not merely exist on PATH."""
    if os.name == "nt" or sys.platform != "linux":
        return False
    binary = shutil.which("systemd-run")
    if binary is None:
        return False
    try:
        probe = subprocess.run(
            [binary, "--user", "--scope", "--quiet", "--", "/bin/sh", "-c", "exit 0"],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def schedule_deferred_restart(
    verb: str = "restart", *, delay_seconds: int = DEFAULT_DELAY_SECONDS, extra_args: Optional[list[str]] = None,
) -> str:
    """Schedule ``hermes gateway <verb>`` in a transient user timer; return the unit name.

    Raises :class:`DeferredRestartUnavailable` when the host cannot create one — the caller must
    then warn and refuse rather than restart in-tree and kill its own turn.
    """
    if not _systemd_run_available():
        raise DeferredRestartUnavailable(
            "systemd-run --user is unavailable (no reachable user D-Bus session); cannot schedule "
            "a restart outside this process's cgroup"
        )
    hermes_bin = _hermes_binary()
    unit = f"hermes-deferred-{verb}-{os.getpid()}"
    argv = [
        "systemd-run", "--user", "--collect",
        f"--on-active={int(delay_seconds)}",
        "--unit", unit,
        "--property", "Type=oneshot",
        "--", hermes_bin, "gateway", verb,
        *(extra_args or []),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise DeferredRestartUnavailable(
            f"systemd-run could not schedule the restart: {(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
    return unit


def _hermes_binary() -> str:
    """The Hermes entry point this process was launched with, else ``hermes`` from PATH."""
    candidate = shutil.which("hermes")
    if candidate:
        return candidate
    # ``hermes-agent/.hermes/bin/hermes`` ships with the checkout; fall back to the module runner.
    raise DeferredRestartUnavailable("cannot locate the hermes entry point to schedule a restart")


def command_hint(verb: str = "restart") -> str:
    """The exact command an operator (or an agent) should run for an out-of-tree restart."""
    return f"systemd-run --user --on-active=5 --collect --unit hermes-deferred-{verb} -- hermes gateway {verb}"
