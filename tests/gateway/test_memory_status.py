"""Tests for gateway.memory_status — the /api/status memory rollup (NS-656)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.memory_status import classify_pressure, collect_memory_status
from gateway.shutdown_watchdog import get_loop_heartbeat_path
from gateway.lifecycle_ledger import get_lifecycle_sentinel_path

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _write_heartbeat(
    home: Path,
    *,
    updated_at: datetime = _NOW,
    mem: dict | None = None,
) -> None:
    path = get_loop_heartbeat_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": 12345,
        "updated_at": updated_at.isoformat(),
        "monotonic": 1.0,
    }
    if mem is not None:
        payload["mem"] = mem
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_sentinel(home: Path, payload: dict) -> None:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestClassifyPressure:
    """v2 thresholds (owner directive 08.10): absolute available bytes only.

    elevated: available < 2_592_215_552 bytes (2.4 GiB);
    critical: available < 1_073_741_824 bytes (1 GiB);
    swap and MemTotal fractions are NOT triggers any more.
    """

    def test_plentiful_memory_is_ok(self) -> None:
        # 4 GiB available of 8 GiB total — far above the 2.4 GiB line.
        assert classify_pressure(4 * 1024 * 1024, 8 * 1024 * 1024) == "ok"

    def test_exact_2_4_gib_boundary_is_ok(self) -> None:
        # 2_531_461 KiB = 2_592_221_184 bytes — the first KiB sample at or
        # above the 2.4 GiB threshold (2_592_215_552 bytes): ok.
        assert classify_pressure(2_531_461, 8 * 1024 * 1024) == "ok"

    def test_just_below_2_4_gib_is_elevated(self) -> None:
        # 2_531_460 KiB = 2_592_150_528 bytes — the last KiB sample strictly
        # below the 2.4 GiB threshold: elevated.
        assert classify_pressure(2_531_460, 8 * 1024 * 1024) == "elevated"

    def test_exact_one_gib_is_not_critical(self) -> None:
        # Exactly 1 GiB available: critical is strictly-less-than, so this
        # lands in the elevated band (1 GiB <= avail < 2.4 GiB).
        assert classify_pressure(1024 * 1024, 2 * 1024 * 1024) == "elevated"

    def test_just_below_one_gib_is_critical(self) -> None:
        # 1 GiB minus 1 KiB — the last sample strictly below 1 GiB.
        assert (
            classify_pressure(1024 * 1024 - 1, 8 * 1024 * 1024) == "critical"
        )

    def test_absolute_bytes_work_without_total(self) -> None:
        # total is ignored: 512 MiB available is critical with any total.
        assert classify_pressure(512 * 1024, None) == "critical"
        # 2 GiB available, unknown total: above 2.4 GiB? No — elevated.
        assert classify_pressure(2 * 1024 * 1024, None) == "elevated"

    def test_swap_no_longer_raises_pressure(self) -> None:
        # v2: swap fully retired as a trigger. 70% swap used with 6 GiB
        # available stays ok; 95% swap used likewise.
        assert (
            classify_pressure(
                6 * 1024 * 1024,
                8 * 1024 * 1024,
                swap_used_kib=7 * 1024 * 1024,
                swap_total_kib=10 * 1024 * 1024,
            )
            == "ok"
        )
        assert (
            classify_pressure(
                3 * 1024 * 1024,
                8 * 1024 * 1024,
                swap_used_kib=19 * 1024 * 1024,
                swap_total_kib=20 * 1024 * 1024,
            )
            == "ok"
        )

    def test_missing_sample_is_unknown(self) -> None:
        assert classify_pressure(None, None) == "unknown"

    def test_bool_is_not_an_int(self) -> None:
        # True == 1 in Python — must not classify as "1 KiB available".
        assert classify_pressure(True, 2048 * 1024) == "unknown"


class TestCollectMemoryStatus:
    def test_no_files_yields_unknown(self, tmp_path: Path) -> None:
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["pressure"] == "unknown"
        assert status["gateway_rss_mb"] is None
        assert status["last_boot_unclean"] is False
        assert status["last_boot_suspected_oom"] is False

    def test_fresh_heartbeat_reports_pressure_and_numbers(
        self, tmp_path: Path
    ) -> None:
        _write_heartbeat(
            tmp_path,
            updated_at=_NOW - timedelta(seconds=30),
            mem={
                "rss_kib": 400 * 1024,
                "mem_total_kib": 1024 * 1024,
                "mem_available_kib": 50 * 1024,
                "swap_used_kib": 200 * 1024,
            },
        )
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["pressure"] == "critical"
        assert status["gateway_rss_mb"] == 400
        assert status["system_total_mb"] == 1024
        assert status["system_available_mb"] == 50
        assert status["swap_used_mb"] == 200
        assert status["sampled_at"] is not None

    def test_stale_heartbeat_keeps_numbers_but_unknown_pressure(
        self, tmp_path: Path
    ) -> None:
        # A dead gateway's final gasp must not render a live "critical"
        # banner forever.
        _write_heartbeat(
            tmp_path,
            updated_at=_NOW - timedelta(hours=2),
            mem={
                "rss_kib": 400 * 1024,
                "mem_total_kib": 1024 * 1024,
                "mem_available_kib": 10 * 1024,
            },
        )
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["pressure"] == "unknown"
        assert status["system_available_mb"] == 10
        assert status["sampled_at"] is not None

    def test_future_heartbeat_is_treated_as_stale(self, tmp_path: Path) -> None:
        # Clock skew / restored snapshots: a timestamp from the future is
        # not evidence about the present either.
        _write_heartbeat(
            tmp_path,
            updated_at=_NOW + timedelta(hours=1),
            mem={"mem_total_kib": 1024 * 1024, "mem_available_kib": 10 * 1024},
        )
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["pressure"] == "unknown"

    def test_sentinel_flags_surface(self, tmp_path: Path) -> None:
        _write_sentinel(
            tmp_path,
            {
                "phase": "running",
                "pid": 999,
                "started_at": "2026-08-13T01:00:00+00:00",
                "prior_unclean_exit": True,
                "prior_suspected_oom": True,
            },
        )
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["last_boot_unclean"] is True
        assert status["last_boot_suspected_oom"] is True
        # boot_id identifies the reporting life so the dashboard can key
        # banner dismissal per incident (a NEW restart re-surfaces it).
        assert status["boot_id"] == "2026-08-13T01:00:00+00:00"

    def test_boot_id_absent_or_malformed_stays_none(self, tmp_path: Path) -> None:
        _write_sentinel(
            tmp_path,
            {"phase": "running", "pid": 999, "started_at": 12345},
        )
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["boot_id"] is None
        assert collect_memory_status(tmp_path.joinpath("nohome"), now=_NOW)[
            "boot_id"
        ] is None

    def test_clean_sentinel_has_no_flags(self, tmp_path: Path) -> None:
        _write_sentinel(
            tmp_path,
            {"phase": "exited", "pid": 999, "exit_reason": "graceful_shutdown"},
        )
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["last_boot_unclean"] is False
        assert status["last_boot_suspected_oom"] is False

    def test_corrupt_files_never_raise(self, tmp_path: Path) -> None:
        hb = get_loop_heartbeat_path(tmp_path)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("{not json", encoding="utf-8")
        sentinel = get_lifecycle_sentinel_path(tmp_path)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("[]", encoding="utf-8")  # valid JSON, wrong shape
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["pressure"] == "unknown"

    def test_heartbeat_without_mem_block(self, tmp_path: Path) -> None:
        # Non-Linux hosts: sample_memory() returns {} so the heartbeat has
        # no mem key at all.
        _write_heartbeat(tmp_path, updated_at=_NOW)
        status = collect_memory_status(tmp_path, now=_NOW)
        assert status["pressure"] == "unknown"
        assert status["gateway_rss_mb"] is None
