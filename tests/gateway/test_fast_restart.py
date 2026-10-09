"""Tests for the fast-restart path (path A1).

``/restart fast`` (and CLI ``hermes gateway restart --fast`` / env
``HERMES_GATEWAY_FAST_RESTART=1``) must shut the gateway down quickly even
with active long-running turns: the after-turn wait is skipped, the drain
budget is zero for both chat agents and cron work (the cron floor is not
applied), and in-flight sessions are pre-marked ``resume_pending`` with a
dedicated reason so the next boot auto-resumes them.

Acceptance criteria (A1):
  1. ``/restart fast`` completes ``stop()`` in <= 15s with one active
     ~10-hour turn (simulated here by a non-finishing running agent).
  2. The interrupted session is auto-resumed after the restart
     (``resume_pending`` + reason in ``_AUTO_RESUME_REASONS``).
  3. New functions are covered by these unit tests.
  4. ``FAST_RESTART`` marker line written to gateway.log.
  5. The service-restart exit code 75 (EX_TEMPFAIL) is preserved on the
     fast path.
  6. A live detached kanban worker — spawned outside ``process_registry``,
     exactly as ``hermes_cli/kanban_db.py:_default_spawn()`` does — survives
     the ``stop(fast=True)`` subprocess sweep unchanged (t_83dff32f).
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.platforms.base import MessageEvent, MessageType
from gateway.restart import (
    FAST_RESTART_ENV,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    resolve_cron_drain_budget,
)
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


# ---------------------------------------------------------------------------
# resolve_cron_drain_budget(fast=True)
# ---------------------------------------------------------------------------


class TestResolveCronDrainBudgetFast:
    def test_fast_returns_zero_immediately(self):
        """Fast mode must not apply the cron floor — budget is 0."""
        assert (
            resolve_cron_drain_budget(
                0.0, 30.0, watchdog_delay=60.0, elapsed=1.0, fast=True
            )
            == 0.0
        )

    def test_fast_zeroes_even_a_long_configured_drain(self):
        assert (
            resolve_cron_drain_budget(
                5.0, 300.0, watchdog_delay=60.0, elapsed=0.0, fast=True
            )
            == 0.0
        )

    def test_non_fast_keeps_existing_floor(self):
        assert (
            resolve_cron_drain_budget(
                0.0, 30.0, watchdog_delay=60.0, elapsed=1.0, fast=False
            )
            == 30.0
        )


# ---------------------------------------------------------------------------
# request_restart(fast=True) skips the after-turn wait
# ---------------------------------------------------------------------------


class TestRequestRestartFast:
    @pytest.mark.asyncio
    async def test_fast_skips_after_turn_wait_and_forwards_fast_to_stop(self):
        runner, _adapter = make_restart_runner()
        # A running agent that would otherwise defer stop() for a long
        # after-turn wait (restart_after_turn_timeout defaults to 1800s).
        runner._running_agents = {"sess-1": object()}
        runner.stop = AsyncMock()
        runner._await_active_work_before_restart = AsyncMock(
            side_effect=AssertionError("after-turn wait must be skipped in fast mode")
        )

        assert runner.request_restart(
            detached=False, via_service=True, fast=True
        ) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

        runner._await_active_work_before_restart.assert_not_awaited()
        runner.stop.assert_awaited_once_with(
            restart=True,
            detached_restart=False,
            service_restart=True,
            fast=True,
        )

    @pytest.mark.asyncio
    async def test_non_fast_still_waits_for_active_turn(self):
        runner, _adapter = make_restart_runner()
        runner._running_agents = {"sess-1": object()}
        runner.stop = AsyncMock()
        awaited = []

        async def _fake_wait():
            awaited.append(True)

        runner._await_active_work_before_restart = _fake_wait

        assert runner.request_restart(
            detached=False, via_service=True, fast=False
        ) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

        assert awaited == [True]
        runner.stop.assert_awaited_once_with(
            restart=True,
            detached_restart=False,
            service_restart=True,
            fast=False,
        )


# ---------------------------------------------------------------------------
# stop(fast=True): zero drain + fast_restart resume_pending + FAST_RESTART log
# ---------------------------------------------------------------------------


class TestStopFast:
    @pytest.mark.asyncio
    async def test_fast_stop_marks_resume_pending_with_fast_reason(self):
        """AC2: interrupted sessions carry the fast_restart reason."""
        runner, adapter = make_restart_runner()
        adapter.disconnect = AsyncMock()
        runner._restart_drain_timeout = 0.05

        running_agent = MagicMock()
        session_key = "agent:main:telegram:dm:A"
        runner._running_agents = {session_key: running_agent}

        session_store = MagicMock()
        session_store.mark_resume_pending = MagicMock(return_value=True)
        runner.session_store = session_store

        with patch("gateway.status.remove_pid_file"), patch(
            "gateway.status.write_runtime_status"
        ):
            await runner.stop(
                restart=True,
                detached_restart=False,
                service_restart=True,
                fast=True,
            )

        calls = session_store.mark_resume_pending.call_args_list
        assert calls, "fast stop never marked any session resume_pending"
        for args in calls:
            assert args[0][1] == "fast_restart"

    @pytest.mark.asyncio
    async def test_fast_stop_completes_fast_with_active_long_turn(self):
        """AC1: stop() returns quickly even with an active never-ending turn."""
        runner, adapter = make_restart_runner()
        adapter.disconnect = AsyncMock()
        # A "10-hour turn": agent stays in _running_agents forever.
        runner._running_agents = {"agent:main:telegram:dm:LONG": object()}

        session_store = MagicMock()
        session_store.mark_resume_pending = MagicMock(return_value=True)
        runner.session_store = session_store

        started = time.monotonic()
        with patch("gateway.status.remove_pid_file"), patch(
            "gateway.status.write_runtime_status"
        ):
            await runner.stop(restart=True, fast=True)
        elapsed = time.monotonic() - started

        assert elapsed < 15.0, f"fast stop took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_fast_stop_leaves_detached_kanban_worker_alive(self, tmp_path):
        """AC6 (t_83dff32f): a live detached kanban worker survives fast-stop.

        Kanban workers are launched by ``hermes_cli/kanban_db.py:_default_spawn()``
        as raw ``subprocess.Popen`` (``stdin=DEVNULL``, ``stdout``→per-task log,
        ``start_new_session=True``, ``CREATE_NO_WINDOW`` on Windows) and tracked
        ONLY in the module-level ``kanban_db._windows_worker_processes`` dict —
        never registered in ``tools.process_registry`` (kanban_db.py contains
        zero references to it). ``stop(fast=True)`` kills tool subprocesses by
        calling exactly ``process_registry.kill_all()`` (gateway/run.py
        ``_kill_tool_subprocesses``), which only iterates registered sessions.

        This test runs a real fast-stop while a real detached worker of the
        same spawn shape (deliberately outside the registry) is alive, then
        asserts the worker is still running when ``stop()`` returns. The
        registry sweep is swapped for a recording spy so this test never
        sweeps other genuinely-registered sessions that earlier tests in this
        pytest process may hold; the real-sweep property is covered by
        ``test_process_registry.py::test_kill_all_leaves_unregistered_detached_process_alive``.
        """
        from unittest.mock import patch as _patch

        from tools import process_registry as _pr

        runner, adapter = make_restart_runner()
        adapter.disconnect = AsyncMock()
        runner.session_store = MagicMock()

        log_file = tmp_path / "kanban_task.log"
        log_f = open(log_file, "wb")
        kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **kwargs,
        )
        log_f.close()
        try:
            assert worker.poll() is None, "detached worker died before fast-stop"

            kill_all_calls = []

            def _spy_kill_all(*args, **kwargs):
                kill_all_calls.append((args, kwargs))
                return 0

            with _patch.object(
                _pr.process_registry, "kill_all", _spy_kill_all
            ), _patch("gateway.status.remove_pid_file"), _patch(
                "gateway.status.write_runtime_status"
            ):
                await runner.stop(restart=True, fast=True)

            # Fast-stop performed the registry sweep…
            assert kill_all_calls, "fast-stop never ran process_registry.kill_all()"
            # …and yet the detached worker survived the whole fast-stop.
            assert worker.poll() is None, (
                f"fast-stop killed detached worker pid {worker.pid} (outside "
                "process_registry; kanban workers are spawned by "
                "kanban_db._default_spawn and tracked only in "
                "kanban_db._windows_worker_processes)"
            )
        finally:
            worker.kill()
            worker.wait(timeout=10)

    @pytest.mark.asyncio
    async def test_fast_stop_emits_fast_restart_log_marker(self, caplog):
        """AC4: a FAST_RESTART json line is written during the fast stop."""
        import logging

        runner, adapter = make_restart_runner()
        adapter.disconnect = AsyncMock()
        runner._running_agents = {"agent:main:telegram:dm:LONG": object()}

        session_store = MagicMock()
        session_store.mark_resume_pending = MagicMock(return_value=True)
        runner.session_store = session_store

        with patch("gateway.status.remove_pid_file"), patch(
            "gateway.status.write_runtime_status"
        ):
            with caplog.at_level(logging.INFO, logger="gateway.run"):
                await runner.stop(restart=True, fast=True)

        marker_lines = [
            r.getMessage() for r in caplog.records if "FAST_RESTART" in str(r.getMessage())
        ]
        assert marker_lines, "no FAST_RESTART marker line emitted"
        payload = json.loads(marker_lines[-1].split(" ", 1)[1])
        assert payload["reason"] == "fast-restart"
        assert payload["active_agents"] == 1
        assert "ts" in payload and "downtime_target" in payload


# ---------------------------------------------------------------------------
# _AUTO_RESUME_REASONS membership (also AC2)
# ---------------------------------------------------------------------------


class TestAutoResumeReasons:
    def test_fast_restart_reason_eligible_for_startup_auto_resume(self):
        assert "fast_restart" in gateway_run.GatewayRunner._AUTO_RESUME_REASONS


# ---------------------------------------------------------------------------
# /restart fast slash parsing
# ---------------------------------------------------------------------------


class TestRestartFastSlash:
    @pytest.mark.asyncio
    async def test_restart_fast_arg_forwards_fast_flag(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.delenv("INVOCATION_ID", raising=False)

        runner, _adapter = make_restart_runner()
        runner.request_restart = MagicMock(return_value=True)

        event = MessageEvent(
            text="/restart fast",
            message_type=MessageType.TEXT,
            source=make_restart_source(),
            message_id="m1",
            platform_update_id=424242,
        )
        result = await runner._handle_restart_command(event)

        assert "fast" in result.lower() or "перезапуск" in result.lower()
        called_kwargs = runner.request_restart.call_args.kwargs
        assert called_kwargs.get("fast") is True

    @pytest.mark.asyncio
    async def test_plain_restart_does_not_set_fast(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.delenv("INVOCATION_ID", raising=False)

        runner, _adapter = make_restart_runner()
        runner.request_restart = MagicMock(return_value=True)

        event = MessageEvent(
            text="/restart",
            message_type=MessageType.TEXT,
            source=make_restart_source(),
            message_id="m1",
            platform_update_id=12345,
        )
        await runner._handle_restart_command(event)

        called_kwargs = runner.request_restart.call_args.kwargs
        assert called_kwargs.get("fast") is False


# ---------------------------------------------------------------------------
# env alternative HERMES_GATEWAY_FAST_RESTART=1
# ---------------------------------------------------------------------------


class TestFastRestartEnv:
    def test_env_truthy_values_enable_fast(self, monkeypatch):
        for value in ("1", "true", "yes", "on"):
            monkeypatch.setenv(FAST_RESTART_ENV, value)
            assert (
                gateway_run.GatewayRunner._fast_restart_env_requested()
                is True
            )

    def test_env_absent_or_falsey_disables_fast(self, monkeypatch):
        monkeypatch.delenv(FAST_RESTART_ENV, raising=False)
        assert (
            gateway_run.GatewayRunner._fast_restart_env_requested()
            is False
        )
        monkeypatch.setenv(FAST_RESTART_ENV, "0")
        assert (
            gateway_run.GatewayRunner._fast_restart_env_requested()
            is False
        )
        monkeypatch.setenv(FAST_RESTART_ENV, "no")
        assert (
            gateway_run.GatewayRunner._fast_restart_env_requested()
            is False
        )


# ---------------------------------------------------------------------------
# service-restart exit code 75 preserved on the fast path (AC 5)
# ---------------------------------------------------------------------------


class TestExitCodePreserved:
    """The fast path must keep the EX_TEMPFAIL(75) service-restart contract."""

    def test_constant_is_ex_tempfail_75(self):
        assert GATEWAY_SERVICE_RESTART_EXIT_CODE == 75

    @pytest.mark.asyncio
    async def test_fast_via_service_restart_sets_exit_code_75(
        self, monkeypatch, tmp_path
    ):
        """AC: a via-service fast restart still exits with 75 + reason set."""
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.delenv("INVOCATION_ID", raising=False)

        runner, adapter = make_restart_runner()
        adapter.disconnect = AsyncMock()
        # One active long turn that never finishes — fast path must still
        # reach the terminal exit-code region.
        runner._running_agents = {"agent:main:telegram:dm:LONG": object()}

        session_store = MagicMock()
        session_store.mark_resume_pending = MagicMock(return_value=True)
        runner.session_store = session_store

        started = time.monotonic()
        with patch("gateway.status.remove_pid_file"), patch(
            "gateway.status.write_runtime_status"
        ):
            await runner.stop(
                restart=True,
                detached_restart=False,
                service_restart=True,
                fast=True,
            )
        elapsed = time.monotonic() - started

        assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE == 75
        assert runner._exit_reason == "Gateway restart requested"
        assert elapsed < 15.0, f"fast stop took {elapsed:.2f}s"
