"""Tests for cron durable resume tokens (cron half of the gateway-restart
survival work).

Flow under test (acceptance for t_5e028f69):

1. ``run_job`` writes a ``resume_token`` into jobs.json BEFORE it constructs
   the agent, so a mid-run gateway kill/restart leaves a durable pointer to
   the interrupted run's session (session id is the run's ``_cron_session_id``).
2. On the next scheduler start, ``rearm_resumable_cron_jobs`` (called from
   ``CronScheduler.recover_interrupted``) finds jobs whose run never reached a
   terminal write and re-arms them:
     - ``resume`` mode → stamps ``resume_session`` and forces ``next_run_at``
       so the first tick relaunches the run on the SAME session id, with a
       continuation prompt ("Продолжай ... НЕ повторяй уже выполненные шаги").
     - ``rerun`` mode (``resume_mode="rerun"``, or no_agent/script jobs) →
       clears the token; the job re-fires per its normal schedule.
3. A terminal status write (success, real failure, delivery-terminal) clears
   the token; the graceful-shutdown interrupt marking PRESERVES it so restart
   can resume, rather than losing an interrupted LLM run.

These tests deliberately avoid patching away the resume machinery: they drive
``run_job`` for real (AIAgent stubbed, store redirected to a tmp profile) so a
later scheduler-start re-arm path is exercised against the same store code a
real restart would touch.
"""

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as jobs
from cron.jobs import (
    clear_resume_token,
    rearm_resumable_cron_jobs,
    set_resume_token,
    use_cron_store,
)
from cron.scheduler import run_job


@contextlib.contextmanager
def _noop_lock():
    """Stand-in for _jobs_lock when a test drives store helpers directly."""
    yield


def _base_job(**overrides):
    """Minimal LLM-agent job dict for run_job (mirrors provider-pin tests)."""
    job = {
        "id": "resume-test",
        "name": "resume test",
        "prompt": "Summarize the month.",
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "base_url": None,
    }
    job.update(overrides)
    return job


def _write_job_to_store(tmp_path, record: dict) -> str:
    """Persist ``record`` (by its ``id``) into the tmp profile's cron store.

    Returns the job id. Fields on the record are written verbatim so tests can
    plant ``resume_token`` / ``resume_session`` / ``fire_claim`` / ``script``
    exactly as production would store them.
    """
    with use_cron_store(tmp_path), jobs._jobs_lock():
        all_jobs = jobs.load_jobs()
        for i, rec in enumerate(all_jobs):
            if rec["id"] == record["id"]:
                all_jobs[i] = record
                break
        else:
            all_jobs.append(record)
        jobs.save_jobs(all_jobs)
    return record["id"]


def _read_job(tmp_path, job_id):
    with use_cron_store(tmp_path), jobs._jobs_lock():
        for rec in jobs.load_jobs():
            if rec["id"] == job_id:
                return rec
    return None


def _drive_run_job(job, tmp_path):
    """Drive run_job against a tmp profile with a stubbed AIAgent.

    Returns (success, output, final_response, error, agent_kwargs) where
    ``agent_kwargs`` is the keyword args the AIAgent constructor was called
    with (to assert session_id continuity), or None if never constructed.
    """
    fake_db = MagicMock()
    with use_cron_store(tmp_path), \
         patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={
                 "api_key": "test-key",
                 "base_url": "https://example.invalid/v1",
                 "provider": "nous",
                 "api_mode": "chat_completions",
             },
         ), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent

        success, output, final_response, error = run_job(job)
        agent_kwargs = None
        if mock_agent_cls.called:
            agent_kwargs = dict(mock_agent_cls.call_args.kwargs)
        # The job prompt is passed to run_conversation, not the constructor;
        # surface it alongside the constructor kwargs for assertions.
        if mock_agent_cls.called:
            agent_kwargs["_run_conversation_prompt"] = mock_agent.run_conversation.call_args.args[0]

    return success, output, final_response, error, agent_kwargs


class TestWriteBeforeLaunch:
    """Acceptance #1: jobs.json gets a resume_token before the agent launches."""

    def test_run_job_writes_token_before_agent(self, tmp_path):
        job = _base_job()
        _write_job_to_store(
            tmp_path,
            {
                "id": job["id"],
                "name": job["name"],
                "prompt": job["prompt"],
                "model": None,
                "provider": None,
                "provider_snapshot": None,
                "base_url": None,
                "schedule": {"kind": "interval", "seconds": 3600},
            },
        )
        success, output, final_response, error, agent_kwargs = _drive_run_job(job, tmp_path)

        assert success is True
        assert final_response == "ok"
        assert agent_kwargs is not None
        agent_session_id = agent_kwargs.get("session_id")

        # run_job must have persisted a durable resume_token tied to the session
        # the agent actually ran on, BEFORE constructing it.
        stored = _read_job(tmp_path, job["id"])
        token = stored.get("resume_token") if stored else None
        assert isinstance(token, dict)
        assert token["session_id"] == agent_session_id
        assert "started_at" in token

    def test_terminal_write_clears_token(self, tmp_path):
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hello", schedule="* * * * *", name="resume-clear"
            )
            job_id = created["id"]
            set_resume_token(job_id, {"session_id": "s1", "fire_owner": "w1"})
            assert jobs.get_job(job_id)["resume_token"]["session_id"] == "s1"

            # Success = terminal. Token must be gone; status terminal.
            assert jobs.mark_job_run(job_id, True) is True
            final = jobs.get_job(job_id)
            assert "resume_token" not in final
            assert final.get("last_status") == "ok"

    def test_failure_write_clears_token(self, tmp_path):
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hello", schedule="* * * * *", name="resume-fail"
            )
            job_id = created["id"]
            set_resume_token(job_id, {"session_id": "s2"})
            assert jobs.mark_job_run(job_id, False, "agent blew up") is True
            final = jobs.get_job(job_id)
            assert "resume_token" not in final
            assert final.get("last_status") == "error"

    def test_interrupt_marking_preserves_token(self, tmp_path):
        """Graceful-shutdown write must PRESERVE the token so restart can resume."""
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hello", schedule="* * * * *", name="resume-interrupt"
            )
            job_id = created["id"]
            set_resume_token(job_id, {"session_id": "s3", "fire_owner": "w1"})
            # mark_running_jobs_interrupted calls mark_job_run(...,
            # preserve_resume_token=True) under the hood.
            assert (
                jobs.mark_job_run(
                    job_id, False, "interrupted", preserve_resume_token=True
                )
                is True
            )
            final = jobs.get_job(job_id)
            assert final.get("resume_token", {}).get("session_id") == "s3"


class TestRecoveryRearm:
    """rearm_resumable_cron_jobs: resume mode re-arms; rerun/foreign clears."""

    @staticmethod
    def _plant(job_id, token, tmp_path, **job_overrides):
        record = {
            "id": job_id,
            "name": f"job-{job_id}",
            "prompt": "hello",
            "schedule": {"kind": "interval", "seconds": 3600},
            "repeat": {"times": 10, "completed": 0},
            "next_run_at": None,
            "resume_token": token,
        }
        record.update(job_overrides)
        _write_job_to_store(tmp_path, record)
        return job_id

    def test_resume_mode_arms_next_fire(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "_jobs_lock", _noop_lock, raising=True)
        self._plant(
            "j-resume",
            {"session_id": "persist-session-1", "fire_owner": "owner-a"},
            tmp_path,
        )

        with use_cron_store(tmp_path):
            staged = rearm_resumable_cron_jobs()
        after = _read_job(tmp_path, "j-resume")

        assert staged == 1
        assert after.get("resume_session") == "persist-session-1"
        assert after.get("next_run_at")

    def test_rerun_mode_clears_token(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "_jobs_lock", _noop_lock, raising=True)
        self._plant(
            "j-rerun",
            {"session_id": "old-session", "fire_owner": "owner-a"},
            tmp_path,
            resume_mode="rerun",
        )

        with use_cron_store(tmp_path):
            staged = rearm_resumable_cron_jobs()
        after = _read_job(tmp_path, "j-rerun")

        assert staged == 0
        assert "resume_token" not in after
        assert "resume_session" not in after

    def test_script_job_always_reruns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "_jobs_lock", _noop_lock, raising=True)
        self._plant(
            "j-script",
            {"session_id": "old-session", "fire_owner": "owner-a"},
            tmp_path,
            script="/tmp/x.sh",
        )

        with use_cron_store(tmp_path):
            staged = rearm_resumable_cron_jobs()
        after = _read_job(tmp_path, "j-script")

        assert staged == 0
        assert "resume_token" not in after

    def test_foreign_owner_claim_is_plain_rerun(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "_jobs_lock", _noop_lock, raising=True)
        self._plant(
            "j-foreign",
            {"session_id": "old-session", "fire_owner": "owner-original"},
            tmp_path,
            fire_claim={"by": "owner-different", "at": "when"},
        )

        with use_cron_store(tmp_path):
            staged = rearm_resumable_cron_jobs()
        after = _read_job(tmp_path, "j-foreign")

        assert staged == 0
        assert "resume_token" not in after
        assert "resume_session" not in after


class TestMidRunKillResume:
    """Acceptance #2: kill mid-run → new scheduler resumes → terminal status."""

    def test_kill_sim_resume_to_completed(self, tmp_path):
        # ── Simulate run 1 starting (token written, session 'cron_a_run_1').
        #    The gateway is then "killed" before any terminal write.
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hello", schedule="0 0 * * *", name="kill-resume"
            )
            job_id = created["id"]
            assert set_resume_token(
                job_id,
                {
                    "session_id": "cron_a_run_1",
                    "fire_owner": "owner-a",
                    "started_at": "2026-01-01T00:00:00",
                },
            )
            # No mark_job_run: the run was killed mid-flight.

        # ── New scheduler starts: recover_interrupted re-arms it for resume.
        with use_cron_store(tmp_path):
            staged = rearm_resumable_cron_jobs()
        assert staged == 1
        rearmed = _read_job(tmp_path, job_id)
        assert rearmed.get("resume_session") == "cron_a_run_1"

        # ── The first tick relaunches the run on the SAME session, with a
        #    continuation prompt (fresh agent, prior history available).
        job = _read_job(tmp_path, job_id) or _base_job(id=job_id, prompt="hello")
        success, output, final_response, error, agent_kwargs = _drive_run_job(job, tmp_path)

        assert success is True
        assert final_response == "ok"
        assert agent_kwargs is not None
        # Resumed on the interrupted session, not a fresh cron_* id.
        assert agent_kwargs.get("session_id") == "cron_a_run_1"
        # The continuation instruction must be in the delivered prompt.
        prompt = agent_kwargs.get("_run_conversation_prompt") or ""
        assert "cron_a_run_1" in prompt
        assert "Продолжай" in prompt
        assert "НЕ повторяй уже выполненные шаги" in prompt

        # ── The resumed run reaches a terminal status → token cleared.
        with use_cron_store(tmp_path), jobs._jobs_lock():
            assert jobs.mark_job_run(job_id, True) is True
            final = jobs.get_job(job_id)
            assert "resume_token" not in final
            assert final.get("last_status") == "ok"


class TestUpdateJobPreservesToken:
    """update_job must never silently drop an active durable resume_token.

    The resume_token / resume_session pair is the durable "owner interrupted,
    resume from here" signal for an interrupted long LLM cron run. A blind
    field-merge rewrite of a job (update_job) touching unrelated fields
    (name, prompt, schedule, no_agent, ...) must preserve the pair — losing it
    would strand the interrupted run with nothing to resume (F1 follow-up on
    the fast-gateway-restart mission).
    """

    def test_partial_rewrite_preserves_active_token(self, tmp_path):
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hello", schedule="0 0 * * *", name="update-resume"
            )
            job_id = created["id"]
            assert set_resume_token(
                job_id,
                {"session_id": "session-a", "fire_owner": "owner-a", "started_at": "2026-01-01"},
            )
            # Plant the per-run resume_session marker the re-arm path would have
            # stamped before the next tick (real restart state).
            with use_cron_store(tmp_path), jobs._jobs_lock():
                all_jobs = jobs.load_jobs()
                for i, rec in enumerate(all_jobs):
                    if rec["id"] == job_id:
                        all_jobs[i] = {**rec, "resume_session": "session-a"}
                        break
                jobs.save_jobs(all_jobs)

            # Rewrite with unrelated fields only — must not clobber the pair.
            updated = jobs.update_job(job_id, {"name": "renamed"})
            assert updated["resume_token"]["session_id"] == "session-a"
            assert updated["resume_token"]["fire_owner"] == "owner-a"
            assert updated["resume_session"] == "session-a"

            persisted = jobs.get_job(job_id)
            assert persisted["resume_token"]["session_id"] == "session-a"
            assert persisted["resume_session"] == "session-a"
            assert persisted["name"] == "renamed"

    def test_schedule_rewrite_preserves_token(self, tmp_path):
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hello", schedule="0 0 * * *", name="schedule-rewrite"
            )
            job_id = created["id"]
            set_resume_token(job_id, {"session_id": "session-b", "fire_owner": ""})

            # Schedule change recomputes next_run_at and rewrites the record.
            updated = jobs.update_job(job_id, {"schedule": "*/5 * * * *"})
            assert updated["resume_token"]["session_id"] == "session-b"
            persisted = jobs.get_job(job_id)
            assert persisted["resume_token"]["session_id"] == "session-b"
            assert persisted["schedule_display"] in {"*/5 * * * *", "every 5 minutes"}

    def test_job_without_token_stays_without_token(self, tmp_path):
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hi", schedule="0 0 * * *", name="no-token-update"
            )
            job_id = created["id"]
            updated = jobs.update_job(job_id, {"name": "still-no-token"})
            assert "resume_token" not in updated
            assert "resume_session" not in updated

    def test_explicit_resume_token_update_is_honored(self, tmp_path):
        """An explicit resume_token/resume_session in `updates` wins, so a
        caller that deliberately resets resume state keeps full control
        (otherwise there would be no way to re-point the pair though
        update_job)."""
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(
                prompt="hi", schedule="0 0 * * *", name="explicit-token"
            )
            job_id = created["id"]
            set_resume_token(job_id, {"session_id": "old", "fire_owner": "w1"})
            updated = jobs.update_job(
                job_id,
                {"resume_token": {"session_id": "new", "fire_owner": "w2"}},
            )
            assert updated["resume_token"]["session_id"] == "new"


class TestClearAndHelpers:
    def test_clear_resume_token(self, tmp_path):
        with use_cron_store(tmp_path), jobs._jobs_lock():
            created = jobs.create_job(prompt="hi", schedule="0 0 * * *")
            job_id = created["id"]
            set_resume_token(job_id, {"session_id": "s", "fire_owner": ""})
            assert clear_resume_token(job_id) is True
            assert "resume_token" not in jobs.get_job(job_id)

    def test_set_resume_token_missing_job(self, tmp_path):
        with use_cron_store(tmp_path), jobs._jobs_lock():
            assert set_resume_token("no-such-job", {"session_id": "s"}) is False
