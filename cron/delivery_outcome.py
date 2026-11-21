"""Write a queued cron delivery's terminal outcome back onto the run that queued it.

An agent run hands its send to ``cron.delivery_queue`` and books ``last_status =
"delivery_queued"`` / ledger ``delivery_outcome = "queued"``. Once a gateway drain sends,
refuses, suppresses or fences the send ``unknown``, the job record and the execution
ledger must carry the real result (main's inline ``ok``/``delivered`` and
``delivery_failed``/``failed`` + ``last_delivery_error``); otherwise ``hermes cron list``
says "delivery still in progress" forever and refused sends never surface.

Both sides call :func:`settle_queued_delivery`: the drain right after it terminalizes a
row, and the run right after its own bookkeeping (a drain can finish before the run's
``mark_job_run`` lands). Every write is fenced on the execution id, so the call is
idempotent and never rewrites a later run's status.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Queue terminal status -> ledger delivery_outcome. ``unknown`` (owner died mid-send, never
# retried) is surfaced as a failure: the user cannot be told the result arrived.
_LEDGER_OUTCOME = {
    "delivered": "delivered", "suppressed": "suppressed", "failed": "failed", "unknown": "failed",
}


def _settle_job_record(job_id: str, execution_id: str, delivery_error: Optional[str]) -> bool:
    """Stamp the queue's terminal outcome onto the job while *execution_id* is still the job's
    latest recorded completion.

    The fence is the job's OWN bookkeeping (``canonical_completions[-1]``), never the ledger's
    latest attempt: a fire that lost its claim books an execution row the job never completed,
    and demanding that row BE this execution silently threw the real outcome away — exactly how
    a failed delivery stayed ``delivery_queued`` with ``last_delivery_error=null`` forever.
    """
    from cron.jobs import _with_job, save_jobs

    def apply(jobs, _i, job):
        completions = job.get("canonical_completions") or []
        if not completions or completions[-1] != execution_id:
            return False
        status = job.get("last_status")
        if delivery_error:
            if status in ("delivery_queued", "ok"):
                # Agent succeeded but the result never reached the user: never green.
                status = "delivery_failed"
        elif status == "delivery_queued":
            status = "ok"
        if (status, delivery_error) == (job.get("last_status"), job.get("last_delivery_error")):
            return True  # already settled (the drain and the run both call this)
        job["last_status"] = status
        job["last_delivery_error"] = delivery_error
        # A delivery that never landed must be countable without touching ``failure_streak``
        # (that one is agent-failures-only, see cron.jobs._record_run_outcome) so a flaky link
        # cannot trip the "N failures in a row — pause it" nudge.
        job["delivery_failure_streak"] = (
            int(job.get("delivery_failure_streak") or 0) + 1 if delivery_error else 0)
        job["last_delivery_queued"] = None
        save_jobs(jobs)
        return True

    return bool(_with_job(job_id, apply, missing=False))


def _terminal(execution_id: str) -> Optional[tuple[str, Optional[str]]]:
    """``(ledger_outcome, delivery_error)`` once the queue row is terminal, else None."""
    from cron.delivery_queue import get_status

    row = get_status(str(execution_id))
    if not row or row["status"] not in _LEDGER_OUTCOME:
        return None
    delivery_error = None
    if row["status"] in ("failed", "unknown"):
        delivery_error = str(row.get("error") or f"delivery {row['status']}")
    return _LEDGER_OUTCOME[row["status"]], delivery_error


def settled_outcome(execution_id: str) -> Optional[str]:
    """The ledger outcome a drain already reached for *execution_id* (None while queued)."""
    terminal = _terminal(execution_id)
    return terminal[0] if terminal else None


def settle_queued_delivery(job_id: Optional[str], execution_id: Optional[str]) -> bool:
    """Apply the queue's terminal outcome for *execution_id*.

    True once the outcome is on the record — either this call wrote it or an earlier one did.
    False while the send is still in flight OR when a NEWER completion owns the job (that run's
    status must never be overwritten); the latter is logged, not swallowed, so a stranded
    ``delivery_queued`` is never invisible.
    """
    if not job_id or not execution_id:
        return False
    terminal = _terminal(str(execution_id))
    if terminal is None:
        return False
    from cron.executions import settle_delivery_outcome

    settle_delivery_outcome(str(execution_id), terminal[0])
    if _settle_job_record(str(job_id), str(execution_id), terminal[1]):
        return True
    from cron.jobs import get_job

    job = get_job(str(job_id)) or {}
    if (job.get("last_status"), job.get("last_delivery_error")) == (
            "delivery_failed" if terminal[1] else "ok", terminal[1]):
        return True
    completions = job.get("canonical_completions") or []
    if completions and completions[-1] != str(execution_id):
        logger.warning(
            "Cron delivery %s: outcome %s not written onto job %s — a newer run (%s) owns the "
            "record; leaving its status alone", execution_id, terminal[0], job_id, completions[-1])
        return False
    logger.warning("Cron delivery %s: outcome %s could not be written onto job %s "
                   "(job missing or fenced out)", execution_id, terminal[0], job_id)
    return False


def settle_quietly(job_id: Optional[str], execution_id: Optional[str]) -> None:
    """Bookkeeping failure must not undo a terminal at-most-once send or fail its run."""
    try:
        settle_queued_delivery(job_id, execution_id)
    except Exception:
        logger.warning("Cron delivery %s: outcome write-back failed", execution_id, exc_info=True)
