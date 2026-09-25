"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.

Guard arming
------------
The guard arms ONLY for a genuine dispatcher-spawned kanban worker whose card
is currently ``running`` and captured by this exact run. It must never arm
just because a bare ``HERMES_KANBAN_TASK`` var is present in the process env:
a long-lived gateway/api_server process started inside an old worker context
can inherit a stale task id whose card is already ``done`` (or ``blocked``/
``archived``). Arming there made every interactive session on that process
loop on a false "Task is still running — complete or block" nudge with no
terminal board tool the kernel would accept (the phantom-loop regression).

Three independent checks gate the guard (all must pass):

1. Full dispatcher worker env envelope — ``HERMES_KANBAN_RUN_ID`` / ``_DB`` /
   ``_WORKSPACE`` / ``HERMES_PROFILE`` + ``HERMES_SESSION_SOURCE=kanban``
   alongside ``HERMES_KANBAN_TASK``. These are set together by the dispatcher
   in ``hermes_cli.kanban_db._build_worker_env``; a stale parent inherits only
   the bare var.
2. Dispatcher-owned execution context — excludes delegate_task children and
   in-process cron jobs fired from a worker
   (``agent.delegation_context.is_dispatcher_owned_worker_context``).
3. Authoritative read-only board probe — the card must genuinely be
   ``running`` and bound to this run/session, mirroring
   ``kanban_db.has_authenticated_worker_binding`` (implemented here
   self-contained so this module stays decoupled from the mutation core and
   works on any board DB). A terminal card (done/blocked/archived), a foreign
   run, or a reclaimed run → no nudge.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Iterable, Optional

_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})
_DEFAULT_MAX_ATTEMPTS = 2

# Env keys the kanban dispatcher sets on every worker subprocess together with
# ``HERMES_KANBAN_TASK`` (see ``hermes_cli.kanban_db._build_worker_env``). A
# long-lived process started inside an old worker context inherits only the
# bare ``HERMES_KANBAN_TASK`` (and maybe ``HERMES_SESSION_ID`` /
# ``HERMES_PROFILE``); it never carries this full envelope. Requiring it stops
# a stale task id from arming the guard on interactive sessions.
_DISPATCHER_WORKER_ENV_REQUIRED = (
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_PROFILE",
)

# Short-lived process cache for the authoritative live-card probe so a running
# worker does not reopen the board DB on every turn-end. Keyed by task id.
_LIVE_RUNNING_CACHE: dict[str, tuple[float, bool]] = {}
_LIVE_RUNNING_TTL = 60.0


def is_dispatcher_worker_env() -> bool:
    """True only when this process carries the full dispatcher worker envelope.

    A genuine dispatcher-spawned kanban worker has ``HERMES_KANBAN_TASK`` plus
    the run/board/workspace/profile/session-source keys the dispatcher sets in
    ``hermes_cli.kanban_db._build_worker_env``. An interactive session that
    merely inherited a stale ``HERMES_KANBAN_TASK`` from a long-lived parent
    (gateway launched inside an old worker env) is NOT a dispatcher worker and
    must never be treated as one. This is the cheap first fence; it needs no
    board access and is safe to call on any hot path.
    """
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    session_source = (os.environ.get("HERMES_SESSION_SOURCE") or "").strip()
    if session_source and session_source != "kanban":
        return False
    for key in _DISPATCHER_WORKER_ENV_REQUIRED:
        if not (os.environ.get(key) or "").strip():
            return False
    return True


def _is_owner_context() -> bool:
    """True when this execution owns the dispatcher's kanban task.

    Excludes delegate_task children and cron jobs fired in-process from a
    worker, which inherit the worker's ``HERMES_KANBAN_*`` vars but are not
    dispatcher-owned kanban workers (``agent/delegation_context``).
    """
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context
    except Exception:
        return False
    try:
        return is_dispatcher_owned_worker_context()
    except Exception:
        return False


def _card_is_live_running(
    task_id: str, run_id: str, assignee: str, session_id: str
) -> bool:
    """Read-only board probe: is ``task_id`` ``running`` AND bound to this run?

    Faithfully mirrors the fail-closed binding the kanban mutation tools
    enforce (``kanban_db.has_authenticated_worker_binding``): the card must be
    ``running``, the run must be the card's current run, still active, and
    recorded with this worker's session id. Terminal cards
    (done/blocked/archived), foreign runs, or reclaimed runs → ``False``. Any
    DB/env failure also fails safe to ``False`` so a broken probe can never
    arm the guard on an interactive session.

    The SQL is kept self-contained (rather than calling
    ``has_authenticated_worker_binding``) so this module stays decoupled from
    the Kanban mutation core and works against the same ``tasks`` /
    ``task_runs`` columns on any board DB.
    """
    try:
        run_id_i = int(run_id)
    except (TypeError, ValueError):
        return False
    try:
        from hermes_cli import kanban_db

        path = kanban_db.kanban_db_path(board=None)
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT 1 FROM tasks t
                  JOIN task_runs r ON r.id = t.current_run_id
                 WHERE t.id = ? AND t.assignee = ? AND t.status = 'running'
                   AND t.current_run_id = ? AND r.task_id = t.id
                   AND r.profile = ? AND r.status = 'running'
                   AND r.ended_at IS NULL AND r.worker_session_id = ?
                """,
                (task_id, assignee, run_id_i, assignee, session_id),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def _worker_is_live_running() -> bool:
    """Cached authoritative probe for the guard's ON state."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    assignee = (os.environ.get("HERMES_PROFILE") or "").strip()
    session_id = (os.environ.get("HERMES_SESSION_ID") or "").strip()
    if not task_id or not run_id or not assignee or not session_id:
        return False
    now = time.monotonic()
    cached = _LIVE_RUNNING_CACHE.get(task_id)
    if cached and now - cached[0] < _LIVE_RUNNING_TTL:
        return cached[1]
    result = _card_is_live_running(task_id, run_id, assignee, session_id)
    _LIVE_RUNNING_CACHE[task_id] = (now, result)
    return result


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On only when this process is a genuine dispatcher-owned worker whose card
    is currently ``running`` AND captured by this run (unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it). A long-lived process
    (gateway/api_server) that merely inherited a stale ``HERMES_KANBAN_TASK``
    for an already-done card therefore never arms the guard, so interactive
    sessions get no false "Task is still running — complete or block" nudge.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    if not is_dispatcher_worker_env():
        return False
    if not _is_owner_context():
        return False
    return _worker_is_live_running()


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not an active kanban
    worker, already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "is_dispatcher_worker_env",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
