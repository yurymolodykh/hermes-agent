"""Cross-platform kanban memory sampler + v2 pressure classification.

Covers the K1 sampler and the v2 thresholds (owner directive 08.10):

1. :func:`hermes_cli.kanban_db._system_memory_sample` — cascade sampler:
   psutil (when importable) -> Windows ``ctypes`` GlobalMemoryStatusEx
   (stdlib) -> Linux ``/proc/meminfo`` (legacy behaviour). All three arms
   return the same unified shape so the dispatcher and dashboard agree.
   Fail-safe: when every arm is unavailable the function returns ``{}`` and
   the dispatcher behaves exactly as before the change (``unknown``, no
   spawn restriction).
2. v2 :func:`gateway.memory_status.classify_pressure` — elevated when
   available < 2_592_215_552 bytes (2.4 GiB), critical when available <
   1_073_741_824 bytes (1 GiB). Absolute available bytes ONLY: swap usage
   and MemTotal fractions are retired as triggers (dynamically managed
   swap percentages carry no signal on this host).
"""

from __future__ import annotations

import sys
import types

import pytest

from gateway.memory_status import classify_pressure
from hermes_cli import kanban_db as kb

GIB = 1024 * 1024  # KiB per GiB


def _fake_psutil(vm_kib: int, avail_kib: int, swap_total_kib: int, swap_used_kib: int):
    """Install a fake ``psutil`` module in sys.modules and return it."""

    class _VM:
        def __init__(self) -> None:
            self.total = vm_kib * 1024
            self.available = avail_kib * 1024

    class _SM:
        def __init__(self) -> None:
            self.total = swap_total_kib * 1024
            self.used = swap_used_kib * 1024

    mod = types.ModuleType("psutil")
    mod.virtual_memory = lambda: _VM()
    mod.swap_memory = lambda: _SM()
    return mod


# ---------------------------------------------------------------------------
# psutil arm
# ---------------------------------------------------------------------------


def test_psutil_arm_returns_unified_format(monkeypatch):
    """(a) psutil arm parses a live psutil surface into the unified shape."""
    fake = _fake_psutil(
        vm_kib=16 * 1024**2,
        avail_kib=4 * 1024**2,
        swap_total_kib=8 * 1024**2,
        swap_used_kib=2 * 1024**2,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    sample = kb._sample_via_psutil()
    assert sample is not None
    assert sample["mem_total_kib"] == 16 * 1024**2
    assert sample["mem_available_kib"] == 4 * 1024**2
    assert sample["swap_total_kib"] == 8 * 1024**2
    assert sample["swap_used_kib"] == 2 * 1024**2
    assert sample["mem_available_pct"] == pytest.approx(25.0)
    assert sample["swap_used_pct"] == pytest.approx(25.0)


def test_psutil_arm_returns_none_when_import_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # force ImportError path
    assert kb._sample_via_psutil() is None


# ---------------------------------------------------------------------------
# cascade selection
# ---------------------------------------------------------------------------


@pytest.mark.real_memory_guard
def test_cascade_prefers_psutil(monkeypatch):
    monkeypatch.setattr(
        kb, "_sample_via_psutil", lambda: {"mem_total_kib": 1}, raising=False
    )
    monkeypatch.setattr(
        kb, "_sample_via_windows_ctypes", lambda: None, raising=False
    )
    monkeypatch.setattr(
        kb, "_sample_via_proc_meminfo", lambda: None, raising=False
    )
    assert kb._system_memory_sample() == {"mem_total_kib": 1}


@pytest.mark.real_memory_guard
def test_cascade_falls_back_to_ctypes_when_psutil_empty(monkeypatch):
    monkeypatch.setattr(kb, "_sample_via_psutil", lambda: None, raising=False)
    monkeypatch.setattr(
        kb,
        "_sample_via_windows_ctypes",
        lambda: {
            "mem_total_kib": 16 * 1024**2,
            "mem_available_kib": 4 * 1024**2,
            "swap_total_kib": 8 * 1024**2,
            "swap_used_kib": 3 * 1024**2,
            "mem_available_pct": 25.0,
            "swap_used_pct": 37.5,
        },
    )
    monkeypatch.setattr(
        kb, "_sample_via_proc_meminfo", lambda: None, raising=False
    )
    sample = kb._system_memory_sample()
    assert sample["mem_available_kib"] == 4 * 1024**2
    assert sample["swap_used_kib"] == 3 * 1024**2


@pytest.mark.real_memory_guard
def test_cascade_uses_proc_meminfo_when_first_two_empty(monkeypatch):
    monkeypatch.setattr(kb, "_sample_via_psutil", lambda: None, raising=False)
    monkeypatch.setattr(
        kb, "_sample_via_windows_ctypes", lambda: None, raising=False
    )
    monkeypatch.setattr(
        kb,
        "_sample_via_proc_meminfo",
        lambda: kb._unified_memory_sample(
            mem_total_kib=16 * 1024**2,
            mem_available_kib=4 * 1024**2,
            swap_total_kib=8 * 1024**2,
            swap_used_kib=2 * 1024**2,
        ),
    )
    sample = kb._system_memory_sample()
    assert sample["mem_total_kib"] == 16 * 1024**2
    assert sample["mem_available_pct"] == pytest.approx(25.0)


@pytest.mark.real_memory_guard
def test_cascade_unknown_when_every_arm_empty(monkeypatch):
    """Fail-safe: all arms fail -> {} -> dispatcher behaves as before."""
    monkeypatch.setattr(kb, "_sample_via_psutil", lambda: None, raising=False)
    monkeypatch.setattr(
        kb, "_sample_via_windows_ctypes", lambda: None, raising=False
    )
    monkeypatch.setattr(
        kb, "_sample_via_proc_meminfo", lambda: None, raising=False
    )
    assert kb._system_memory_sample() == {}


# ---------------------------------------------------------------------------
# _memory_pressure_level: v2 available-bytes thresholds (swap ignored)
# ---------------------------------------------------------------------------


def test_pressure_level_ok_with_half_swap_used():
    """v2: 6 GiB avail, 50% swap used -> swap ignored, level ok."""
    sample = {
        "mem_total_kib": 8 * GIB,
        "mem_available_kib": 6 * GIB,
        "swap_total_kib": 10 * GIB,
        "swap_used_kib": 5 * GIB,  # 50% used — was an elevated trigger in v1
    }
    assert kb._memory_pressure_level(sample) == "ok"


def test_pressure_level_critical_by_available_with_high_swap():
    """v2: 512 MiB avail -> critical regardless of swap (70% used)."""
    sample = {
        "mem_total_kib": 8 * GIB,
        "mem_available_kib": 512 * 1024,
        "swap_total_kib": 10 * GIB,
        "swap_used_kib": 7 * GIB,  # 70% used — irrelevant now
    }
    assert kb._memory_pressure_level(sample) == "critical"


def test_pressure_level_ok_with_low_swap():
    """Both avail and swap healthy -> ok."""
    sample = {
        "mem_total_kib": 8 * GIB,
        "mem_available_kib": 6 * GIB,
        "swap_total_kib": 10 * GIB,
        "swap_used_kib": 1 * GIB,
    }
    assert kb._memory_pressure_level(sample) == "ok"


# ---------------------------------------------------------------------------
# classify_pressure v2 absolute-bytes thresholds (incl. boundaries)
# ---------------------------------------------------------------------------


class TestClassifyPressureV2:
    def test_exact_2_4_gib_boundary_is_ok(self) -> None:
        # 2_531_461 KiB = 2_592_221_184 bytes >= 2_592_215_552 -> ok.
        assert classify_pressure(2_531_461, 24 * GIB) == "ok"

    def test_just_below_2_4_gib_is_elevated(self) -> None:
        # 2_531_460 KiB = 2_592_150_528 bytes < 2_592_215_552 -> elevated.
        assert classify_pressure(2_531_460, 24 * GIB) == "elevated"

    def test_elevated_band_sample(self) -> None:
        # 1.5 GiB available — inside (1 GiB, 2.4 GiB).
        assert classify_pressure(int(1.5 * GIB), 24 * GIB) == "elevated"

    def test_exact_one_gib_is_elevated_not_critical(self) -> None:
        # critical is strictly below 1 GiB; exactly 1 GiB is elevated.
        assert classify_pressure(1 * GIB, 24 * GIB) == "elevated"

    def test_just_below_one_gib_is_critical(self) -> None:
        # 1 GiB minus 1 KiB — the last KiB sample strictly below 1 GiB.
        assert classify_pressure(1 * GIB - 1, 24 * GIB) == "critical"

    def test_deep_critical_sample(self) -> None:
        # 300 MiB available — far below the 1 GiB line.
        assert classify_pressure(300 * 1024, 24 * GIB) == "critical"

    def test_swap_fully_retired_high_swap_keeps_ok(self) -> None:
        # 8 GiB available of 16 GiB, 50% swap used — v1 said elevated
        # (swap trigger), v2 says ok.
        assert (
            classify_pressure(
                8 * GIB, 16 * GIB, swap_used_kib=5 * GIB, swap_total_kib=10 * GIB
            )
            == "ok"
        )

    def test_swap_fully_retired_70pct_swap_keeps_ok(self) -> None:
        # v1 critical via swap (70% > 60%); v2 ok — 6 GiB avail.
        assert (
            classify_pressure(
                6 * GIB, 16 * GIB, swap_used_kib=7 * GIB, swap_total_kib=10 * GIB
            )
            == "ok"
        )

    def test_no_total_still_classifies_by_absolute_bytes(self) -> None:
        # total unknown: 512 MiB available is still critical; 2 GiB is
        # elevated; 4 GiB is ok.
        assert classify_pressure(512 * 1024, None) == "critical"
        assert classify_pressure(2 * GIB, None) == "elevated"
        assert classify_pressure(4 * GIB, None) == "ok"

    def test_malformed_sample_is_unknown(self) -> None:
        assert classify_pressure(None, None) == "unknown"
        assert classify_pressure(True, 2048 * 1024) == "unknown"
