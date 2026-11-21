"""A drained delivery's outcome reaches the job record even when a LATER, never-recorded
execution row (a fire that lost its claim) sits between the run and the drain.

Field defect (t_7a6e8032): ``cron/deliveries.db`` had ``status=failed`` /
``error='Telegram send failed: Timed out'`` for execution 739f67bd…, while ``jobs.json``
still said ``last_status=delivery_queued``, ``last_delivery_error=null``,
``failure_streak=0``. The write-back demanded that the ledger's LATEST attempt be this
execution, and a fire that lost its claim books an execution row the job never completed —
so the real outcome was silently dropped and a broken delivery looked like one in progress.

The fence that matters is the job's own bookkeeping: the execution must be the job's latest
canonical completion. Nothing else may be rewritten.
"""
from cron import delivery_outcome, delivery_queue, executions, jobs


def _job_with_queued_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="p", schedule="every 1h", deliver="telegram:fixture")
    execution = executions.create_execution(job["id"], source="direct")
    execution_id = execution["id"]
    executions.finish_execution(execution_id, success=True, delivery_outcome="queued")
    jobs.mark_job_run(job["id"], True, None, execution_id=execution_id, status="delivery_queued")
    delivery_queue.enqueue(execution_id, job, "the result")
    return job["id"], execution_id


def test_failed_drain_settles_the_job_despite_a_later_lost_claim_row(tmp_path, monkeypatch):
    job_id, execution_id = _job_with_queued_delivery(tmp_path, monkeypatch)
    # A fire that lost its claim books a NEWER execution row the job never completed.
    orphans = executions.create_execution(job_id, source="direct")
    executions.finish_execution(orphans["id"], success=False,
                               error="Fire claim ownership lost; stale result was discarded.")
    assert executions.latest_execution(job_id)["id"] == orphans["id"]

    assert delivery_queue.drain(lambda *args: "delivery error: Telegram send failed: Timed out") == 1

    saved = jobs.get_job(job_id)
    assert saved["last_status"] == "delivery_failed", "a failed send must never read as in-progress"
    assert "Timed out" in saved["last_delivery_error"]
    assert saved["last_delivery_queued"] is None
    assert saved["delivery_failure_streak"] == 1
    # Agent-failure streak stays untouched: the agent did not fail, the link did.
    assert saved["failure_streak"] == 0
    assert executions.get_execution(execution_id)["delivery_outcome"] == "failed"
    # The orphan row is not part of the job's bookkeeping and must not be touched either.
    assert executions.get_execution(orphans["id"])["delivery_outcome"] is None


def test_delivered_drain_settles_the_job_green_and_clears_the_error(tmp_path, monkeypatch):
    job_id, execution_id = _job_with_queued_delivery(tmp_path, monkeypatch)
    jobs.update_job(job_id, {"last_delivery_error": "stale error from an earlier send",
                             "delivery_failure_streak": 2})

    assert delivery_queue.drain(lambda *args: None) == 1

    saved = jobs.get_job(job_id)
    assert (saved["last_status"], saved["last_delivery_error"]) == ("ok", None)
    assert saved["delivery_failure_streak"] == 0
    assert executions.get_execution(execution_id)["delivery_outcome"] == "delivered"


def test_write_back_never_rewrites_a_newer_runs_status_and_says_so(tmp_path, monkeypatch, caplog):
    """A newer run owns the record: its status stands. The refusal is logged, not swallowed —
    a stranded ``delivery_queued`` must never be invisible."""
    job_id, _execution_id = _job_with_queued_delivery(tmp_path, monkeypatch)
    newer = executions.create_execution(job_id, source="direct")
    jobs.mark_job_run(job_id, True, None, execution_id=newer["id"], status="ok")

    delivery_queue.drain(lambda *args: "delivery error: Telegram send failed: Timed out")

    saved = jobs.get_job(job_id)
    assert (saved["last_status"], saved["last_delivery_error"]) == ("ok", None)
    assert "not written onto job" in caplog.text


def test_settle_reports_written_and_already_settled_distinctly(tmp_path, monkeypatch):
    """The return value must not claim success for work it did not do."""
    job_id, execution_id = _job_with_queued_delivery(tmp_path, monkeypatch)
    delivery_queue.drain(lambda *args: None)
    assert delivery_outcome.settle_queued_delivery(job_id, execution_id) is True  # idempotent
    assert delivery_outcome.settle_queued_delivery(job_id, "no-such-execution") is False
