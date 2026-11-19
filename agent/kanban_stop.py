"""Turn-end guard for kanban workers, which must end with a terminal board tool that hands
the card to whoever owns it next (``kanban_complete``, ``kanban_block``,
``kanban_request_review``, ``kanban_request_changes``). Some models narrate the next step
and stop with no tool calls; Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``. Policy-only: return a bounded synthetic nudge so the loop continues
instead of exiting.
"""

from __future__ import annotations

import os
import logging
from typing import Any, Iterable, Optional

from agent.delegation_context import owned_kanban_task

logger = logging.getLogger(__name__)


# Every tool that ends this worker's responsibility for the card, not just the two that
# close it out: ``kanban_request_review`` moves it to ``review`` (goals.py's continuation /
# finalize prompts tell builders to call it) and ``kanban_request_changes`` returns it to
# ``ready`` (the sdlc-review skill tells reviewers to). Nudging after either asks a worker
# that did the right thing to ``kanban_complete`` a card it must not close.
_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_schedule",
    "kanban_request_review",
    "kanban_request_changes",
})

_DEFAULT_MAX_ATTEMPTS = 2

# The full dispatcher envelope a genuine kanban worker carries (set by the
# dispatcher in ``hermes_cli.kanban_db_dispatch._build_worker_env``). A bare
# ``HERMES_KANBAN_TASK`` is NOT evidence of a worker: a long-lived process
# started inside an old worker context inherits it.
_DISPATCHER_WORKER_ENV_REQUIRED = (
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_PROFILE",
)


def is_dispatcher_worker_env() -> bool:
    """True only when this process carries the full dispatcher worker envelope.

    A genuine dispatcher-spawned kanban worker has ``HERMES_KANBAN_TASK`` plus the
    run/board/workspace/profile keys the dispatcher sets for every worker. An
    interactive session that merely inherited a stale ``HERMES_KANBAN_TASK`` from a
    long-lived parent (a gateway launched inside an old worker env) is NOT a
    dispatcher worker and must never be treated as one. Cheap first fence: needs no
    board access, safe on any hot path.
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


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set for the dispatcher-owned worker, unless
    ``HERMES_KANBAN_STOP_NUDGE`` disables it. In-process delegate_task children and cron runs
    inherit the env var but own no board task and carry no kanban toolset."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(owned_kanban_task())


def worker_card_closed(task_id: Optional[str] = None, run_id: Optional[str] = None) -> bool:
    """True when this worker's attempt no longer owns the card.

    A worker that hands the card off (``kanban_complete`` / ``kanban_block`` /
    ``kanban_request_review``) has finished its responsibility, but nothing ends its
    conversation turn: the model keeps calling tools, burns the remaining iteration
    budget and finally exits as ``timed_out`` — an alarm on a card that was already
    closed minutes earlier, and a second handoff event on top of the real one.

    The authority is the board, not the transcript: the card's own run must still be
    OPEN and still be THIS process's run. A card whose ``current_run_id`` is NULL
    (closed by a terminal transition) or names a successor run is not ours to keep
    working.

    Fails CLOSED to False — an unreadable board must never truncate a live turn.
    """
    tid = (task_id or owned_kanban_task() or "").strip()
    if not tid:
        return False
    db_path = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    if not db_path:
        try:
            from hermes_cli.kanban_db import kanban_db_path
            db_path = str(kanban_db_path())
        except Exception:
            return False
    pinned_run = (run_id if run_id is not None else os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    try:
        import sqlite3
        from pathlib import Path
        conn = sqlite3.connect(f"{Path(db_path).absolute().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT status, current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.debug("kanban handoff check: board read failed for %s", tid, exc_info=True)
        return False
    if row is None:
        return False
    status = str(row[0] or "")
    current_run = row[1]
    if status not in {"running"} or current_run is None:
        # Any non-running status, or a card whose run was closed by the handoff.
        return True
    if pinned_run and str(current_run) != pinned_run:
        # A successor attempt owns the card now; this one must not keep working it.
        return True
    return False


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        role = msg.get("role")
        if role == "assistant" and any(
            _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS for tc in msg.get("tool_calls") or []
        ):
            return True
        if role == "tool" and str(msg.get("name") or "") in _TERMINAL_KANBAN_TOOLS:
            return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a kanban worker, already completed/blocked, budget exhausted)."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    # The transcript is the status source: this text is only reached when the session made no
    # handoff call, so it never tells a worker to close a card it already sent to review.
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` has not been handed off: this session made no terminal board "
        "call (`kanban_complete` / `kanban_request_review` / `kanban_block`). Ending now "
        "causes a protocol violation (clean exit with the card still `running`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work is done "
        "and needs no review, `kanban_request_review(summary=...)` if it is a code "
        "change that needs same-card review, OR `kanban_block(reason=...)` if you are "
        "blocked. Reviewers approve with `kanban_complete` or send the card back with "
        "`kanban_request_changes(reason=...)`.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = ["build_kanban_stop_nudge", "is_dispatcher_worker_env", "kanban_stop_nudge_enabled",
           "session_called_kanban_terminal", "worker_card_closed"]
