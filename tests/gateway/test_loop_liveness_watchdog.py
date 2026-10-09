"""Gateway event-loop freeze backstops for issue #69089."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from gateway.shutdown_watchdog import (
    _arm_loop_floor_timer,
    start_loop_liveness_watchdog,
)


def _immediate_loop() -> MagicMock:
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda callback: callback()
    return loop


def test_loop_liveness_watchdog_stop_during_dump_disarms_hard_exit():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    handle_ready = threading.Event()
    handle_ref = {}
    exit_codes = []

    def stop_during_dump(*_args, **_kwargs) -> None:
        assert handle_ready.wait(timeout=2.0)
        handle_ref["handle"].stop()

    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback",
            side_effect=stop_during_dump,
        ) as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert handle is not None
        handle_ref["handle"] = handle
        handle_ready.set()
        handle.join(timeout=2.0)

    assert not handle.is_alive()
    critical.assert_called_once()
    dump.assert_called_once_with(all_threads=True)
    assert exit_codes == []


def test_loop_liveness_watchdog_stop_during_final_miss_disarms_hard_exit():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    probe_scheduled = threading.Event()
    release_probe = threading.Event()
    probe_event_ref = {}
    handle_ref = {}
    exit_codes = []

    class FinalStrikeLimit:
        def __gt__(self, _strikes: int) -> bool:
            # If strike evaluation is reached, keep recheck #2 from masking a
            # missing post-probe recheck #1 in this boundary test.
            handle_ref["handle"]._stop_event.clear()
            return False

    def hold_scheduled_probe(callback) -> None:
        probe_event_ref["event"] = callback.__self__
        probe_scheduled.set()
        assert release_probe.wait(timeout=2.0)

    loop.call_soon_threadsafe.side_effect = hold_scheduled_probe
    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
    ):
        handle = start_loop_liveness_watchdog(
            loop,
            probe_interval=0.01,
            probe_timeout=0.01,
            max_strikes=FinalStrikeLimit(),
        )
        assert handle is not None
        handle_ref["handle"] = handle
        assert probe_scheduled.wait(timeout=2.0), "watchdog did not schedule a probe"

        def stop_during_miss() -> bool:
            handle.stop()
            return False

        probe_event_ref["event"].is_set = stop_during_miss
        release_probe.set()
        handle.join(timeout=1.0)

    assert not handle.is_alive()
    assert exit_codes == []
    critical.assert_not_called()
    dump.assert_not_called()


def test_loop_liveness_watchdog_stop_after_first_recheck_skips_final_actions():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    probe_scheduled = threading.Event()
    release_probe = threading.Event()

    def hold_scheduled_probe(callback) -> None:
        probe_scheduled.set()
        assert release_probe.wait(timeout=2.0)

    loop.call_soon_threadsafe.side_effect = hold_scheduled_probe
    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit") as hard_exit,
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert handle is not None
        assert probe_scheduled.wait(timeout=2.0), "watchdog did not schedule a probe"

        original_is_set = handle._stop_event.is_set
        is_set_calls = 0

        def stop_on_final_recheck() -> bool:
            nonlocal is_set_calls
            is_set_calls += 1
            # With the forced immediate timeout: _wait_for_probe is call 1,
            # recheck #1 is call 2, and recheck #2 is call 3.
            if is_set_calls == 3:
                handle.stop()
            return original_is_set()

        handle._stop_event.is_set = stop_on_final_recheck
        with patch(
            "gateway.shutdown_watchdog.time.monotonic", side_effect=[0.0, 1.0]
        ):
            release_probe.set()
            handle.join(timeout=1.0)

    assert is_set_calls == 3
    assert not handle.is_alive()
    critical.assert_not_called()
    dump.assert_not_called()
    hard_exit.assert_not_called()


def test_gateway_config_loop_watchdog_round_trip():
    """loop_watchdog is a config.yaml knob: default on, nested-gateway form honored."""
    from gateway.config import GatewayConfig

    assert GatewayConfig.from_dict({}).loop_watchdog is True
    assert GatewayConfig.from_dict({"loop_watchdog": False}).loop_watchdog is False
    assert (
        GatewayConfig.from_dict(
            {"gateway": {"loop_watchdog": "off"}}
        ).loop_watchdog
        is False
    )
    config = GatewayConfig.from_dict({"loop_watchdog": False})
    assert config.to_dict()["loop_watchdog"] is False


def test_gateway_config_loop_watchdog_tuning_round_trip():
    """Watchdog tolerance knobs parse, serialize, and clamp malformed values."""
    from gateway.config import GatewayConfig

    # Defaults
    default = GatewayConfig.from_dict({})
    assert default.loop_watchdog is True
    assert default.loop_watchdog_probe_interval_s == 30.0
    assert default.loop_watchdog_probe_timeout_s == 10.0
    assert default.loop_watchdog_max_strikes == 3

    # Explicit values round-trip
    cfg = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": 45,
            "loop_watchdog_probe_timeout_s": 15,
            "loop_watchdog_max_strikes": 12,
        }
    )
    assert cfg.loop_watchdog_probe_interval_s == 45.0
    assert cfg.loop_watchdog_probe_timeout_s == 15.0
    assert cfg.loop_watchdog_max_strikes == 12
    d = cfg.to_dict()
    assert d["loop_watchdog_probe_interval_s"] == 45.0
    assert d["loop_watchdog_probe_timeout_s"] == 15.0
    assert d["loop_watchdog_max_strikes"] == 12

    # Nested gateway.* form honored
    nested = GatewayConfig.from_dict(
        {
            "gateway": {
                "loop_watchdog_probe_interval_s": 60,
                "loop_watchdog_probe_timeout_s": 20,
                "loop_watchdog_max_strikes": 20,
            }
        }
    )
    assert nested.loop_watchdog_probe_interval_s == 60.0
    assert nested.loop_watchdog_probe_timeout_s == 20.0
    assert nested.loop_watchdog_max_strikes == 20

    # Malformed / degenerate values fall back to safe defaults
    clamped = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": 0,
            "loop_watchdog_probe_timeout_s": -5,
            "loop_watchdog_max_strikes": 0,
        }
    )
    assert clamped.loop_watchdog_probe_interval_s == 30.0
    assert clamped.loop_watchdog_probe_timeout_s == 10.0
    assert clamped.loop_watchdog_max_strikes == 3


def test_gateway_config_loop_watchdog_nonfinite_values_degrade():
    """NaN/Inf tuning values fall back to defaults instead of reaching the
    watchdog's Event.wait loop (or aborting config load via int(inf))."""
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": float("inf"),
            "loop_watchdog_probe_timeout_s": float("nan"),
            "loop_watchdog_max_strikes": float("inf"),  # int() would raise
        }
    )
    assert cfg.loop_watchdog_probe_interval_s == 30.0
    assert cfg.loop_watchdog_probe_timeout_s == 10.0
    assert cfg.loop_watchdog_max_strikes == 3

    # Oversized-but-finite values also clamp to defaults.
    big = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": 86400,
            "loop_watchdog_probe_timeout_s": 7200,
            "loop_watchdog_max_strikes": 10**9,
        }
    )
    assert big.loop_watchdog_probe_interval_s == 30.0
    assert big.loop_watchdog_probe_timeout_s == 10.0
    assert big.loop_watchdog_max_strikes == 3


def test_load_gateway_config_bridges_loop_watchdog_keys(tmp_path, monkeypatch):
    """The real startup loader must honor gateway.loop_watchdog* from
    config.yaml — from_dict's nested fallback never sees the yaml gateway
    section because load_gateway_config builds gw_data flat."""
    from gateway.config import load_gateway_config

    (tmp_path / "config.yaml").write_text(
        "gateway:\n"
        "  loop_watchdog: false\n"
        "  loop_watchdog_probe_interval_s: 45\n"
        "  loop_watchdog_probe_timeout_s: 15\n"
        "  loop_watchdog_max_strikes: 12\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.config.get_hermes_home", lambda: tmp_path)

    cfg = load_gateway_config()
    assert cfg.loop_watchdog is False
    assert cfg.loop_watchdog_probe_interval_s == 45.0
    assert cfg.loop_watchdog_probe_timeout_s == 15.0
    assert cfg.loop_watchdog_max_strikes == 12


def test_gateway_runner_liveness_guards_start_and_stop():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._loop_floor_timer_handle = None
    runner._loop_liveness_watchdog = None
    runner.config = None
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    floor_timer = MagicMock()
    watchdog = MagicMock()
    watchdog.is_alive.return_value = True

    with (
        patch(
            "gateway.run._arm_loop_floor_timer", return_value=floor_timer
        ) as arm_floor,
        patch(
            "gateway.run.start_loop_liveness_watchdog", return_value=watchdog
        ) as start_watchdog,
    ):
        runner._start_loop_liveness_guards(loop)

    arm_floor.assert_called_once_with(loop)
    start_watchdog.assert_called_once_with(
        loop,
        probe_interval=30.0,
        probe_timeout=10.0,
        max_strikes=3,
        db_lock_grace_s=300.0,
    )
    assert runner._loop_floor_timer_handle is floor_timer
    assert runner._loop_liveness_watchdog is watchdog

    runner._stop_loop_liveness_guards()

    watchdog.stop.assert_called_once_with()
    floor_timer.cancel.assert_called_once_with()
    assert runner._loop_liveness_watchdog is None
    assert runner._loop_floor_timer_handle is None


def test_gateway_config_loop_watchdog_db_lock_grace_round_trip():
    """The DB-lock grace budget parses, clamps, and round-trips (default 300s)."""
    from gateway.config import GatewayConfig
    from gateway.shutdown_watchdog import DEFAULT_LOOP_WATCHDOG_DB_LOCK_GRACE_S

    assert DEFAULT_LOOP_WATCHDOG_DB_LOCK_GRACE_S == 300.0
    assert GatewayConfig.from_dict({}).loop_watchdog_db_lock_grace_s == 300.0

    explicit = GatewayConfig.from_dict({"loop_watchdog_db_lock_grace_s": 600})
    assert explicit.loop_watchdog_db_lock_grace_s == 600.0
    assert explicit.to_dict()["loop_watchdog_db_lock_grace_s"] == 600.0

    nested = GatewayConfig.from_dict(
        {"gateway": {"loop_watchdog_db_lock_grace_s": 900}}
    )
    assert nested.loop_watchdog_db_lock_grace_s == 900.0

    # Degenerate values fall back to the 300s default.
    for bad in (-5, float("nan"), float("inf"), 999999):
        cfg = GatewayConfig.from_dict({"loop_watchdog_db_lock_grace_s": bad})
        assert cfg.loop_watchdog_db_lock_grace_s == 300.0, bad


def test_loop_watchdog_db_lock_grace_defers_hard_exit_within_budget():
    """A loop frozen on a state.db write lock must NOT be killed inside grace."""
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    # Probes are scheduled but never respond: the loop looks frozen.
    loop.call_soon_threadsafe.side_effect = lambda callback: None
    exit_codes = []

    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
    ):
        handle = start_loop_liveness_watchdog(
            loop,
            probe_interval=0.01,
            probe_timeout=0.001,
            max_strikes=1,
            db_lock_grace_s=30.0,
            db_lock_detector=lambda: True,  # freeze classified as DB-lock wait
        )
        assert handle is not None
        # Several probe cycles, still far inside the 30s grace budget.
        time.sleep(0.12)
        handle.stop()
        handle.join(timeout=2.0)

    assert not handle.is_alive()
    assert exit_codes == []  # the hard exit must have been deferred
    assert dump.call_count == 0
    # The distinct DB-lock grace message must have been logged.
    grace_messages = " | ".join(
        str(call[0][0]) if call and call[0] else ""
        for call in critical.call_args_list
    )
    assert "DB-lock grace" in grace_messages, grace_messages


def test_loop_watchdog_db_lock_grace_budget_exhausted_hard_exits_with_reason():
    """Past the DB-lock grace budget the watchdog still hard-exits (75), with a
    distinct lifecycle reason so operators see it was a waited-out DB lock."""
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda callback: None
    exit_codes = []
    mark_reasons = []

    mono = [0]

    def advancing_monotonic() -> float:
        mono[0] += 50.0
        return mono[0]

    with (
        patch("gateway.shutdown_watchdog.logger.critical"),
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
        patch(
            "gateway.lifecycle_ledger.mark_exited",
            side_effect=lambda *a, **kw: mark_reasons.append(kw.get("reason")),
        ),
        patch("gateway.shutdown_watchdog.time.monotonic", side_effect=advancing_monotonic),
    ):
        handle = start_loop_liveness_watchdog(
            loop,
            probe_interval=0.005,
            probe_timeout=0.005,
            max_strikes=1,
            db_lock_grace_s=5.0,
            db_lock_detector=lambda: True,
        )
        handle.join(timeout=3.0)

    assert exit_codes == [75]
    assert mark_reasons == ["loop_liveness_watchdog_db_lock"]
    dump.assert_called_once_with(all_threads=True)


def test_loop_watchdog_non_db_freeze_keeps_fast_exit():
    """Regression guard: a NON-DB wedge keeps the tight 3-strike hard exit."""
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda callback: None
    exit_codes = []
    mark_reasons = []

    with (
        patch("gateway.shutdown_watchdog.logger.critical"),
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
        patch(
            "gateway.lifecycle_ledger.mark_exited",
            side_effect=lambda *a, **kw: mark_reasons.append(kw.get("reason")),
        ),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.005, probe_timeout=0.005, max_strikes=1
        )
        handle.join(timeout=3.0)

    assert exit_codes == [75]
    assert mark_reasons == ["loop_liveness_watchdog"]
    dump.assert_called_once_with(all_threads=True)
