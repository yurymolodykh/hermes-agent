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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# A transient timer needs a moment to be registered by the user manager before the caller may exit;
# 5s is also long enough that the scheduling call returns while the gateway is still healthy.
DEFAULT_DELAY_SECONDS = 5


class DeferredRestartUnavailable(RuntimeError):
    """No way to schedule the restart outside this process's tree on this host."""


def _self_cgroup() -> str:
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _cgroup_of_pid(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _in_cgroup_of_running_gateway(self_cgroup: str) -> bool:
    """True when this process sits in the cgroup of the gateway named by the PID file.

    The unit name is not a reliable oracle: installations rename it
    (``gateway.service``, ``hermes-gateway@<profile>.service``, a stand unit) and a
    hardcoded ``hermes-gateway.service`` match silently disabled the deferral, so the
    restart ran in-tree and the worker's own turn died with exit 130. Comparing the
    cgroup of the live gateway PID is unit-agnostic.
    """
    try:
        from gateway.status import get_running_pid
        pid = get_running_pid(cleanup_stale=False)
    except Exception:
        return False
    if not pid:
        return False
    gateway_cgroup = _cgroup_of_pid(pid)
    if not gateway_cgroup.strip():
        return False
    return gateway_cgroup == self_cgroup


def in_gateway_cgroup() -> bool:
    """True when this process runs inside the gateway's service cgroup.

    A restart from here tears the caller down with it, so it must be deferred. The check is the
    systemd unit of our own cgroup — not an env marker, which a CLI legitimately inherits — and
    falls back to comparing against the live gateway's own cgroup for renamed units.
    """
    text = _self_cgroup()
    if not text:
        return False
    if "hermes-gateway.service" in text:
        return True
    return _in_cgroup_of_running_gateway(text)


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
    hermes_home: Optional[str] = None,
) -> str:
    """Schedule ``hermes gateway <verb>`` in a transient user timer; return the unit name.

    The timer's service starts in its OWN cgroup, so the restart it runs happens outside the caller's
    tree. ``hermes_home`` is pinned into the unit's environment: the user manager does not inherit the
    caller's ``HERMES_HOME``, and without it the scheduled restart would target the default home even
    when the caller served a profile.

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
    ]
    home = hermes_home or os.environ.get("HERMES_HOME", "")
    if home:
        argv += ["--setenv", f"HERMES_HOME={home}"]
    # Same reasoning for the profile: the user manager inherits neither, and a restart scheduled
    # from a worker of profile X must restart X's gateway, not the default one.
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        argv += ["--setenv", f"HERMES_PROFILE={profile}"]
    # ``--in-tree`` keeps the scheduled command on the ordinary restart path: it is already outside
    # the gateway's cgroup, and re-deferring would hand the work to yet another timer forever.
    argv += ["--", hermes_bin, "gateway", verb, "--in-tree", *(extra_args or [])]
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
    # A worker started from the checkout runs ``python -m hermes_cli.main``; reuse that interpreter.
    launcher = Path(sys.executable or "")
    module_main = launcher.parent / "hermes"
    if module_main.is_file():
        return str(module_main)
    raise DeferredRestartUnavailable("cannot locate the hermes entry point to schedule a restart")


def command_hint(verb: str = "restart") -> str:
    """The exact command an operator (or an agent) should run for an out-of-tree restart."""
    return f"systemd-run --user --on-active=5 --collect --unit hermes-deferred-{verb} -- hermes gateway {verb}"
