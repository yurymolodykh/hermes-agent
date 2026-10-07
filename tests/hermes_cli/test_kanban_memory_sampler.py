"""Cross-platform kanban memory sampler + swap-aware pressure classification (M1).

Covers the M1 slice of the worker-limit mission (t_45f7c356):

1. :func:`hermes_cli.kanban_db._system_memory_sample` — cascade sampler:
   psutil (when importable) -> Windows ``ctypes`` GlobalMemoryStatusEx
   (stdlib) -> Linux ``/proc/meminfo`` (legacy behaviour). All three arms
   return the same unified shape so the dispatcher and dashboard agree.
   Fail-safe: when every arm is unavailable the function returns ``{}`` and
   the dispatcher behaves exactly as before the change (``unknown``, no
   spawn restriction).
2. Swap-aware :func:`gateway.memory_status.classify_pressure` — elevated
   when avail < 20% OR swap_used > 40%; critical when avail < 10% OR
   swap_used > 60%.  Available-memory thresholds stay in the same shape
   (absolute KiB floors + fractions, now aligned with the worker-limit
   dashboard at 20%/10%) so the dashboard banner does not regress.
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
# _memory_pressure_level: swap aware
# ---------------------------------------------------------------------------


def test_pressure_level_elevated_by_swap_with_plenty_available():
    """avail ok but swap_used > 40% -> elevated."""
    sample = {
        "mem_total_kib": 8 * GIB,
        "mem_available_kib": 6 * GIB,  # 75% available — not the trigger
        "swap_total_kib": 10 * GIB,
        "swap_used_kib": 5 * GIB,  # 50% used — swap is the trigger
    }
    assert kb._memory_pressure_level(sample) == "elevated"


def test_pressure_level_critical_by_swap():
    """avail ok but swap_used > 60% -> critical."""
    sample = {
        "mem_total_kib": 8 * GIB,
        "mem_available_kib": 6 * GIB,
        "swap_total_kib": 10 * GIB,
        "swap_used_kib": 7 * GIB,  # 70% used
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
# classify_pressure swap thresholds (incl. boundaries)
# ---------------------------------------------------------------------------


class TestClassifyPressureSwap:
    def test_elevated_swap_boundary_exclusive(self) -> None:
        # 40% swap used is NOT elevated (strictly greater than 40%).
        assert (
            classify_pressure(
                8 * GIB, 16 * GIB, swap_used_kib=4 * GIB, swap_total_kib=10 * GIB
            )
            == "ok"
        )

    def test_elevated_swap_above_40(self) -> None:
        assert (
            classify_pressure(
                8 * GIB,
                16 * GIB,
                swap_used_kib=5 * GIB,
                swap_total_kib=10 * GIB,
            )
            == "elevated"
        )

    def test_critical_swap_boundary_exclusive(self) -> None:
        # 60% swap used is elevated, not critical.
        assert (
            classify_pressure(
                8 * GIB,
                16 * GIB,
                swap_used_kib=6 * GIB,
                swap_total_kib=10 * GIB,
            )
            == "elevated"
        )

    def test_critical_swap_above_60(self) -> None:
        assert (
            classify_pressure(
                8 * GIB,
                16 * GIB,
                swap_used_kib=7 * GIB,
                swap_total_kib=10 * GIB,
            )
            == "critical"
        )

    def test_swap_ignored_when_total_unknown(self) -> None:
        # No swap_total -> swap cannot contribute; avail keeps it ok.
        assert (
            classify_pressure(
                8 * GIB, 16 * GIB, swap_used_kib=9 * GIB, swap_total_kib=None
            )
            == "ok"
        )

    def test_swap_ignored_when_total_zero(self) -> None:
        assert (
            classify_pressure(
                8 * GIB, 16 * GIB, swap_used_kib=0, swap_total_kib=0
            )
            == "ok"
        )

    def test_swap_can_elevate_without_available_fraction(self) -> None:
        # total available unknown but swap high -> still classified.
        assert (
            classify_pressure(
                8 * GIB, None, swap_used_kib=5 * GIB, swap_total_kib=10 * GIB
            )
            == "elevated"
        )

    def test_existing_available_thresholds_still_apply(self) -> None:
        # Regression: absolute KiB floors + unified 20%/10% fractions.
        assert classify_pressure(32 * 1024, 8 * GIB) == "critical"
        assert classify_pressure(300 * 1024, 8 * GIB) == "critical"
        # 150 MiB of 1 GiB ≈ 14.6% — between critical (10%) and elevated (20%).
        assert classify_pressure(150 * 1024, 1 * GIB) == "elevated"
        assert classify_pressure(GIB, 2 * GIB) == "ok"
        assert classify_pressure(None, None) == "unknown"
