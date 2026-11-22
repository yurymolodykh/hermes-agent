"""Turn-result exit policy shared by CLI views and authority-owned workers.

Worker identity is supplied by the caller, not inferred from a daemon's environment.
"""
from typing import Optional

# A persistence failure is not one bucket: the card, the board and the operator's alerts need the
# category, not a guess from an empty response (19.11.2026: a failed session write looked like a
# normal chat reply, exit 1, and the run was booked as a generic crash).
SESSION_PERSIST_MARKER = "[session-persist-failed]"
# ``failure_kind`` values written into ``worker_result``. Kept stable: alerting and statistics read them.
FAILURE_KIND_SESSION_PERSIST = "session_persist"
FAILURE_KIND_TOOL_DENIED = "tool_denied"
FAILURE_KIND_TURN_ERROR = "turn_error"
FAILURE_KIND_INTERRUPTED = "turn_interrupted"

# Reasons whose botching is the operator's tool policy, not the card's work.
_TOOL_DENIED_REASONS = frozenset({"approval_denied", "tool_denied", "permission_denied",
                                  "guardrail_halt", "data_policy_denied"})


# Provider walls do not spend a Kanban task's retry budget.
TRANSIENT_PROVIDER_REASONS = frozenset({
    "rate_limit", "upstream_rate_limit", "billing", "overloaded", "server_error", "timeout",
})
# These failures need an operator change; the dispatcher parks the task immediately.
TERMINAL_PROVIDER_REASONS = frozenset({
    "auth", "auth_permanent", "model_not_found", "ssl_cert_verification", "upstream_blocked",
})


def turn_exit_code(
    result, *, kanban_worker: bool, credentials_rate_limited: bool = False,
    credentials_terminal: bool = False,
    transient_reasons=TRANSIENT_PROVIDER_REASONS, terminal_reasons=TERMINAL_PROVIDER_REASONS,
) -> int:
    """Preserve completion, interruption and retryable/terminal provider outcomes."""
    if not isinstance(result, dict):
        if credentials_rate_limited and kanban_worker:
            from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
            return KANBAN_RATE_LIMIT_EXIT_CODE
        if credentials_terminal and kanban_worker:
            from hermes_cli.kanban_db import KANBAN_TERMINAL_PROVIDER_EXIT_CODE
            return KANBAN_TERMINAL_PROVIDER_EXIT_CODE
        return 1
    if result.get("interrupted"):
        return 130
    if not (result.get("failed") or result.get("partial") or result.get("completed") is False):
        return 0
    if kanban_worker:
        reason = result.get("failure_reason")
        if reason in transient_reasons:
            from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
            return KANBAN_RATE_LIMIT_EXIT_CODE
        if reason in terminal_reasons:
            from hermes_cli.kanban_db import KANBAN_TERMINAL_PROVIDER_EXIT_CODE
            return KANBAN_TERMINAL_PROVIDER_EXIT_CODE
    return 1


def failure_kind(result, *, interrupted_cause: str = "") -> Optional[str]:
    """Category of a failed turn, for ``worker_result.failure_kind`` — ``None`` on a clean turn.

    A card that failed says *why* in one stable word: the board can alert and count by category
    without re-parsing free text. ``session_persist`` is the case that motivated this
    (19.11.2026): a session that could not be saved looked like an ordinary reply.
    """
    if not isinstance(result, dict):
        return FAILURE_KIND_TURN_ERROR
    if result.get("interrupted"):
        return FAILURE_KIND_INTERRUPTED
    failed = bool(result.get("failed") or result.get("partial") or result.get("completed") is False)
    if not failed:
        return None
    reason = str(result.get("failure_reason") or "")
    if reason.startswith("session_persistence_failed") or result.get("turn_exit_reason") == "session_persistence_failed":
        return FAILURE_KIND_SESSION_PERSIST
    if reason.partition(":")[0] in _TOOL_DENIED_REASONS:
        return FAILURE_KIND_TOOL_DENIED
    if result.get("error") and SESSION_PERSIST_MARKER in str(result.get("error")):
        return FAILURE_KIND_SESSION_PERSIST
    return FAILURE_KIND_TURN_ERROR


def credential_failure_flags(exc) -> dict:
    """``turn_exit_code`` credential kwargs for an exception raised before any turn result exists.

    Same predicates as the one-shot CLI's ``_ensure_runtime_credentials``: a quota/rate-limit
    ``AuthError`` is transient, only an explicit re-authentication requirement is terminal. The
    cause chain is walked because agent construction wraps the resolver's error.

    ``KeyPoolSlotBusy`` (every per-card key occupied) is transient for the same reason a quota
    wall is: the card never started, and its work is still owed. It maps to the CONFIGURATION
    exit (75, EX_TEMPFAIL) so the dispatcher requeues it with no failure counted; a plain
    ``KeyPoolError`` (pool not in force, key rejected mid-run) stays a crash, since a retry
    cannot heal it."""
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if _is_key_pool_slot_busy(exc):
            return {"credentials_rate_limited": True, "credentials_terminal": False}
        if isinstance(exc, AuthError):
            return {"credentials_rate_limited": is_rate_limited_auth_error(exc),
                    "credentials_terminal": bool(exc.relogin_required)}
        exc = exc.__cause__ or exc.__context__
    return {}


def _is_key_pool_slot_busy(exc) -> bool:
    """True when *exc* is the key pool's "every card key is taken" wait state."""
    from agent.kanban_key_pool import KeyPoolSlotBusy
    return isinstance(exc, KeyPoolSlotBusy)
