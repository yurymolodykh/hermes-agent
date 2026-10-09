"""KANBAN_ENV_SANITIZE — gateway `run` entry guard (t_1b5b0e48).

The gateway is never a kanban worker.  Dispatcher worker identity lives in env
vars ``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_RUN_ID`` and is injected
*explicitly per worker subprocess* by ``hermes_cli.kanban_db`` when the
dispatcher spawns a worker (``env["HERMES_KANBAN_TASK"] = task.id`` at spawn).

When a restart/launcher chain is itself started inside a worker session
(supervisors, port-watchers, leak/reasoning restarters, Task Scheduler
one-shots, detached ``gateway run --replace`` spawns, terminal re-layouts),
the gateway process can inherit a STALE copy of these vars.  Every agent
session created inside such a gateway would then try to KANBAN_BIND the
gateway to that worker run in ``agent/agent_init.py`` and get rejected with
``KANBAN_BINDING`` — breaking all chat sessions served by the gateway.

These helpers strip the worker identity from the gateway process env at
``gateway run`` entry and from gateway child spawns.  They deliberately do
NOT touch HERMES_KANBAN_BOARD / HERMES_KANBAN_HOME / HERMES_KANBAN_DB /
HERMES_KANBAN_WORKSPACES_ROOT — those pin which board the gateway dispatches
on (dispatch_in_gateway) and must survive for the gateway's own kanban
dispatcher.

Legit dispatcher-worker spawns are unaffected: workers are launched as
``hermes ... chat -q``/``run-task`` (agent_init path), never as ``gateway
run``, and their TASK/RUN_ID are re-injected per child at spawn.  Clearing
the vars at ``gateway run`` entry can therefore never kill a worker identity.
"""

from __future__ import annotations

import logging
import os
import sys

# Worker-session identity vars we strip.  The gateway's own dispatch pin
# (HERMES_KANBAN_BOARD / *_HOME / *_DB / *_WORKSPACES_ROOT) is intentionally
# NOT in this list.
WORKER_IDENTITY_ENV_VARS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
)

# Logger name matches hermes_cli.gateway's module logger, so the warning
# lands in the same gateway log stream.
_GATEWAY_LOGGER_NAME = "hermes_cli.gateway"


def _env_source() -> dict[str, object]:
    """Best-effort ident of the process that passed the env into us."""
    source: dict[str, object] = {"parent_pid": None, "parent_cmdline": None}
    try:
        ppid = os.getppid()
        source["parent_pid"] = ppid
        try:
            import psutil

            parent = psutil.Process(ppid)
            source["parent_cmdline"] = " ".join(parent.cmdline() or [])[:400]
        except Exception:
            pass
    except Exception:
        pass
    return source


def sanitized_child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy of ``base`` (default ``os.environ``) minus kanban worker identity.

    Safe to pass as ``env=`` to Popen/Start-Process so a spawned gateway never
    inherits the calling session's stale TASK/RUN_ID while still keeping the
    gateway's board pin (BOARD/HOME/DB/WORKSPACES_ROOT).
    """
    env = dict(os.environ if base is None else base)
    for var in WORKER_IDENTITY_ENV_VARS:
        env.pop(var, None)
    return env


def strip_gateway_worker_env(log_warning: bool = True) -> dict[str, object]:
    """Remove stale kanban worker identity from this process's ``os.environ``.

    Idempotent: a clean env is untouched.  Returns
    ``{"dropped": {var: value, ...}, "source": {"parent_pid", "parent_cmdline"}}``
    so callers can record exactly what was cleared and from where.
    """
    dropped: dict[str, str] = {}
    for var in WORKER_IDENTITY_ENV_VARS:
        value = os.environ.get(var)
        if value is not None and value != "":
            dropped[var] = value
            del os.environ[var]

    source: dict[str, object] = {}
    if dropped:
        source = _env_source()
        if log_warning:
            detail = ", ".join(f"{k}={v}" for k, v in dropped.items())
            src = ""
            if source.get("parent_cmdline"):
                src = (
                    " (env-source: parent_pid="
                    f"{source.get('parent_pid')} "
                    f"cmdline={source.get('parent_cmdline')})"
                )
            argv = " ".join(sys.argv) or "(empty argv)"
            try:
                logging.getLogger(_GATEWAY_LOGGER_NAME).warning(
                    "KANBAN_ENV_SANITIZE: gateway run inherited stale kanban "
                    "worker env: %s — cleared.%s argv=%s",
                    detail,
                    src,
                    argv,
                )
            except Exception:
                pass  # never let the diagnostic itself crash gateway startup
    return {"dropped": dropped, "source": source}
