"""The gateway dispatcher must not cry "stuck" when the pool is merely full.

Live 21.11.2026: with 7 workers running and every card key leased, the embedded
dispatcher logged, right through a healthy hour,

    kanban dispatcher stuck: ready queue non-empty for 36 consecutive ticks
    but 0 workers spawned. Check profile health (venv, PATH, credentials)

The hint named three causes that were all false (venv, PATH, credentials) and
sent the investigation away from the real one — capacity. These tests drive the
real watcher loop with a dispatcher that reports capacity holds and assert the
warning is gone while an operator-actionable hold still raises it.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from gateway import kanban_watchers as kw
from hermes_cli.kanban_db_dispatch import DispatchResult


class _FakeDispatcher:
    """Reports whatever DispatchResult the test configured, tick after tick."""

    def __init__(self, results_provider):
        self._results_provider = results_provider

    def auto_decompose_tick(self, *_a, **_k):
        return 0

    def tick_once(self):
        return [("default", self._results_provider())]

    def ready_nonempty(self):
        res = self._results_provider()
        return bool(res.skipped_keypool_capped or res.skipped_per_profile_capped
                    or res.skipped_env_unready or res.skipped_unassigned
                    or res.memory_pressure or res.skipped_locked)


@pytest.fixture()
def _loop_env(monkeypatch):
    """Wire the watcher's boot/settings/sleep seams so exactly 8 ticks run."""

    class _FakeRunner(kw.GatewayKanbanWatchersMixin):
        _running = True

    runner = _FakeRunner()
    runner._kanban_dispatcher_lock_handle = None
    ticks = {"n": 0}

    monkeypatch.setattr(kw.GatewayKanbanWatchersMixin, "_kanban_dispatcher_boot",
                        lambda self: (dict, object(), {}))
    monkeypatch.setattr(kw, "_resolve_dispatcher_settings",
                        lambda cfg, kb: type("S", (), {"interval": 1.0})())

    async def _to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(kw, "_to_thread_process_service", _to_thread)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda cfg: (False, 0))
    from hermes_cli import kanban_db_dispatch as _kbd_mod
    monkeypatch.setattr(_kbd_mod, "reap_worker_zombies", list)
    monkeypatch.setattr(kw.GatewayKanbanWatchersMixin, "_sleep_between_ticks",
                        lambda self, interval: _sleep(interval))
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    def _tick_hook():
        ticks["n"] += 1
        if ticks["n"] >= 8:
            runner._running = False
        return ticks["n"]

    return runner, _tick_hook, monkeypatch


def _run(runner, results_provider, tick_hook, monkeypatch, caplog):
    monkeypatch.setattr(kw, "_KanbanDispatcher", lambda kb, settings: _FakeDispatcher(results_provider))

    original_tick = _FakeDispatcher.tick_once

    def tick_once(self):
        tick_hook()
        return original_tick(self)

    monkeypatch.setattr(_FakeDispatcher, "tick_once", tick_once)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=10))
    return [rec.getMessage() for rec in caplog.records]


def test_saturated_pool_does_not_log_dispatcher_stuck(_loop_env, caplog):
    runner, tick_hook, monkeypatch = _loop_env
    messages = _run(runner, lambda: DispatchResult(skipped_keypool_capped=["t_a", "t_b"]),
                    tick_hook, monkeypatch, caplog)

    assert not any("dispatcher stuck" in m for m in messages), messages
    assert any("saturated" in m and "keypool_capped=2" in m for m in messages), messages


def test_per_profile_cap_alone_does_not_log_dispatcher_stuck(_loop_env, caplog):
    runner, tick_hook, monkeypatch = _loop_env
    messages = _run(runner,
                    lambda: DispatchResult(skipped_per_profile_capped=[("t_a", "02-hermes-platform", 3)]),
                    tick_hook, monkeypatch, caplog)

    assert not any("dispatcher stuck" in m for m in messages), messages


def test_env_unready_still_logs_dispatcher_stuck(_loop_env, caplog):
    runner, tick_hook, monkeypatch = _loop_env
    messages = _run(runner,
                    lambda: DispatchResult(skipped_env_unready=[("t_a", "no committed dependency environment")]),
                    tick_hook, monkeypatch, caplog)

    stuck = [m for m in messages if "dispatcher stuck" in m]
    assert stuck, messages
    assert "env_unready=1" in stuck[0]
