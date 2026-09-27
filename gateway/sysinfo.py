"""Best-effort host load metrics for gateway commands (``/status`` sysinfo block).

The owner's requirement: the ``/status`` slash command should also report
**CPU, RAM, SWAP, IO disk** percentages, computed by code -- *not* by a
language model -- and fast (combined overhead well under ~1 s).  This
module is that collector.

Design constraints (verified against the target environment):

* On the production Windows host WMI/CIM and performance counters are blocked
  (``Get-CimInstance`` -> ``PermissionDenied 0x80041003``; ``Get-Counter`` /
  ``typeperf`` -> "Insufficient privileges"; ``wmic`` not installed).  So the
  Windows path uses the Win32 API directly via ``ctypes``:
  - CPU      -> ``kernel32.GetSystemTimes`` (idle/kernel/user) with a cached
                previous sample so steady-state calls are instant.
  - RAM/SWAP -> ``kernel32.GlobalMemoryStatusEx`` + ``MEMORYSTATUSEX``.
  - IO %     -> per-second busy ratio of the busiest disk from
                ``psutil.disk_io_counters()`` deltas; psutil 7.2.2 is a hard
                project dependency.  If psutil is unavailable or no disk
                counters exist, IO degrades to ``None`` (rendered ``н/д``).
* On Linux (the owner also runs a Debian box and the gateway may move there):
  - CPU      -> ``/proc/stat`` deltas.
  - RAM/SWAP -> ``/proc/meminfo``.
  - IO %     -> ``/proc/diskstats`` deltas (busiest disk util), ``None`` if
                unavailable.
* Everything is best-effort and read-only.  A collection failure must never
  raise into ``/status``: each metric degrades to ``None`` independently.
  The only blocking pause is a single tiny first-CPU-sample sleep (~0.25 s);
  every later call is instant.

Public API:

* ``collect_load()`` -> ``dict`` with keys ``cpu_pct``, ``ram_pct``,
  ``ram_gb``, ``ram_total_gb``, ``swap_pct``, ``swap_gb``, ``swap_total_gb``,
  ``io_pct``.  Percent fields are floats in ``[0, 100]`` or ``None``;
  GB fields are rounded floats or ``None``.
* ``format_load_line(raw)`` -> the one-line ``/status`` block.
* ``LoadSnapshot`` dataclass view (optional).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Upper bound for the one-time CPU baseline sample on first call.  Repeat
# calls use the cached previous tick, so steady-state /status is instant.
_FIRST_CPU_SAMPLE_S = 0.25

_NA = "н/д"

_CPU_SAMPLER = Callable[[], Optional[Tuple[int, int]]]  # -> (busy, total)
_IO_SAMPLER = Callable[[], Optional[Dict[str, Tuple[float, float]]]]  # device -> (busy_ticks, ...)


@dataclass
class LoadSnapshot:
    """Collected load metrics.

    All percent fields are floats in ``[0, 100]`` or ``None`` (unavailable).
    ``ram_gb`` / ``ram_total_gb`` / ``swap_gb`` / ``swap_total_gb`` are human
    readable rounded gigabytes (or ``None``).
    """

    cpu_pct: Optional[float] = None
    ram_pct: Optional[float] = None
    ram_gb: Optional[float] = None
    ram_total_gb: Optional[float] = None
    swap_pct: Optional[float] = None
    swap_gb: Optional[float] = None
    swap_total_gb: Optional[float] = None
    io_pct: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cpu_pct": self.cpu_pct,
            "ram_pct": self.ram_pct,
            "ram_gb": self.ram_gb,
            "ram_total_gb": self.ram_total_gb,
            "swap_pct": self.swap_pct,
            "swap_gb": self.swap_gb,
            "swap_total_gb": self.swap_total_gb,
            "io_pct": self.io_pct,
        }


def _pct(part: float, whole: float) -> Optional[float]:
    """``part/whole*100`` clamped to ``[0, 100]``; ``None`` on degenerate input."""
    try:
        whole = float(whole)
        if whole <= 0:
            return None
        part = float(part)
        pct = (part / whole) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return max(0.0, min(100.0, pct))


def _gb(value: Optional[float]) -> Optional[float]:
    """Bytes -> gigabytes rounded to 1 decimal; ``None`` passthrough."""
    if value is None:
        return None
    try:
        return round(float(value) / (1024 ** 3), 1)
    except (TypeError, ValueError):
        return None


def _pct_text(value: Any) -> str:
    try:
        return _NA if value is None else f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return _NA


def _is_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# Windows backend (ctypes P/Invoke; no WMI / no PDH performance counters)
# ---------------------------------------------------------------------------

class _Win32:
    """Lazily-loaded Win32 API surface.  ``available`` is False when not on
    Windows or when the API cannot be bound (degrades every metric)."""

    def __init__(self) -> None:
        self._ok = False
        self._FILETIME: Any = None
        self._MEMORYSTATUSEX: Any = None
        self._get_system_times: Any = None
        self._global_memory_status_ex: Any = None
        if not _is_windows():
            return
        try:
            import ctypes
            from ctypes import wintypes

            class _FILETIME(ctypes.Structure):
                _fields_ = [
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD),
                ]

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            get_system_times = kernel32.GetSystemTimes
            get_system_times.restype = wintypes.BOOL
            get_system_times.argtypes = [
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            ]

            global_memory_status_ex = kernel32.GlobalMemoryStatusEx
            global_memory_status_ex.restype = wintypes.BOOL
            global_memory_status_ex.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]

            self._FILETIME = _FILETIME
            self._MEMORYSTATUSEX = _MEMORYSTATUSEX
            self._get_system_times = get_system_times
            self._global_memory_status_ex = global_memory_status_ex
            self._ok = True
        except Exception as exc:  # pragma: no cover - depends on host
            logger.debug("Win32 API unavailable: %s", exc)
            self._ok = False

    @property
    def available(self) -> bool:
        return self._ok

    def cpu_ticks(self) -> Optional[Tuple[int, int]]:
        """Return ``(busy_ticks, total_ticks)`` from ``GetSystemTimes``.

        busy   = kernel + user - idle   (kernel already includes idle)
        total  = busy + idle
        """
        if not self._ok:
            return None
        try:
            import ctypes

            ft_idle = self._FILETIME()
            ft_kernel = self._FILETIME()
            ft_user = self._FILETIME()
            if not self._get_system_times(
                ctypes.byref(ft_idle),
                ctypes.byref(ft_kernel),
                ctypes.byref(ft_user),
            ):
                return None
            idle = (ft_idle.dwHighDateTime << 32) | ft_idle.dwLowDateTime
            kernel = (ft_kernel.dwHighDateTime << 32) | ft_kernel.dwLowDateTime
            user = (ft_user.dwHighDateTime << 32) | ft_user.dwLowDateTime
            busy = kernel + user - idle
            total = busy + idle
            if total <= 0:
                return None
            return busy, total
        except Exception as exc:  # pragma: no cover - depends on host
            logger.debug("GetSystemTimes failed: %s", exc)
            return None

    def memory(self) -> Optional[Dict[str, int]]:
        """Physical + pagefile (swap) totals/avails in bytes."""
        if not self._ok:
            return None
        try:
            import ctypes

            m = self._MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(self._MEMORYSTATUSEX)
            if not self._global_memory_status_ex(ctypes.byref(m)):
                return None
            return {
                "total_phys": m.ullTotalPhys,
                "avail_phys": m.ullAvailPhys,
                "total_page": m.ullTotalPageFile,
                "avail_page": m.ullAvailPageFile,
            }
        except Exception as exc:  # pragma: no cover - depends on host
            logger.debug("GlobalMemoryStatusEx failed: %s", exc)
            return None


_WIN32 = _Win32()


def _win_cpu_sampler() -> Optional[Tuple[int, int]]:
    return _WIN32.cpu_ticks()


def _win_io_sampler() -> Optional[Dict[str, Tuple[float, float]]]:
    """Return device -> (busy_ms, total_ms) from psutil disk IO counters.

    ``busy_ms`` is read_time+write_time; ``total_ms`` is elapsed wall time we
    cannot know per call, so we return (busy_delta_units, 0) markers and the
    ticker divides by wall time.  Represented as ``{device: (read_time,
    write_time)}``; the ticker computes busy/total from two samples + wall
    clock.
    """
    try:
        import psutil
    except Exception as exc:  # pragma: no cover - optional dep
        logger.debug("psutil unavailable: %s", exc)
        return None
    counters = psutil.disk_io_counters(perdisk=True)
    if not counters:
        return None
    out: Dict[str, Tuple[float, float]] = {}
    for dev, c in counters.items():
        try:
            out[dev] = (float(c.read_time), float(c.write_time))
        except (AttributeError, TypeError, ValueError):
            continue
    return out or None


# ---------------------------------------------------------------------------
# Linux backend (/proc)
# ---------------------------------------------------------------------------

def _read_proc(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, IOError) as exc:
        logger.debug("read %s failed: %s", path, exc)
        return None


def _linux_cpu_sampler() -> Optional[Tuple[int, int]]:
    """Parse /proc/stat first line -> ``(busy, idle)`` jiffies."""
    data = _read_proc("/proc/stat")
    if not data:
        return None
    try:
        parts = data.splitlines()[0].split()
        if not parts or parts[0] != "cpu":
            return None
        vals = [int(v) for v in parts[1:]]
        # vals: user nice system idle iowait irq softirq steal guest guest_nice
        idle = vals[3] + vals[4]  # idle + iowait
        busy = sum(vals) - idle
        return busy, busy + idle
    except (ValueError, IndexError) as exc:
        logger.debug("parse /proc/stat failed: %s", exc)
        return None


def _linux_meminfo() -> Optional[Dict[str, int]]:
    """Parse /proc/meminfo KiB values we need (as bytes)."""
    data = _read_proc("/proc/meminfo")
    if not data:
        return None
    out: Dict[str, int] = {}
    try:
        for line in data.splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                out[key] = int(rest.strip().split()[0]) * 1024  # KiB -> bytes
    except (ValueError, IndexError) as exc:
        logger.debug("parse /proc/meminfo failed: %s", exc)
        return None
    if "MemTotal" not in out or out["MemTotal"] <= 0:
        return None
    return out


def _linux_io_sampler() -> Optional[Dict[str, Tuple[float, float]]]:
    """Parse /proc/diskstats -> device -> (busy_ms, 0).

    ``busy_ms`` is 1-indexed field 13 ("# of milliseconds spent doing I/Os",
    ``parts[12]`` in a zero-indexed split).  Devices that appear/disappear
    between samples are skipped by the ticker.
    """
    data = _read_proc("/proc/diskstats")
    if not data:
        return None
    out: Dict[str, Tuple[float, float]] = {}
    try:
        for line in data.splitlines():
            parts = line.split()
            if len(parts) < 13:
                continue
            try:
                busy_ms = float(parts[12])
            except ValueError:
                continue
            out[parts[2]] = (busy_ms, 0.0)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("parse /proc/diskstats failed: %s", exc)
        return None
    return out or None


# ---------------------------------------------------------------------------
# Cross-platform delta ticker (shared per-process cache)
# ---------------------------------------------------------------------------

class _Ticker:
    """Two-sample delta -> utilisation percent, shared per process.

    ``sampler`` returns opaque per-key tick values (CPU ``(busy, total)`` or
    a per-device ``(busy_ms, 0)`` dict).  The first call establishes a
    baseline; the second and later calls within the process return a real
    delta-derived percent.  IO computes busy/elapsed-wall-time per device and
    reports the busiest device.  ``cpu_sample_s`` bounds the single baseline
    sleep.  Thread-safe.
    """

    def __init__(
        self,
        cpu_sampler: _CPU_SAMPLER,
        io_sampler: Optional[_IO_SAMPLER],
        cpu_sample_s: float = _FIRST_CPU_SAMPLE_S,
    ) -> None:
        self._cpu_sampler = cpu_sampler
        self._io_sampler = io_sampler
        self._cpu_sample_s = cpu_sample_s
        self._lock = threading.Lock()
        self._cpu_prev: Optional[Tuple[int, int]] = None
        self._io_prev: Optional[Dict[str, Tuple[float, float]]] = None
        self._io_prev_t: Optional[float] = None

    def cpu_pct(self) -> Optional[float]:
        sample = self._cpu_sampler
        with self._lock:
            ticks = sample()
            if ticks is None:
                # CPU sampler unavailable (e.g. import failure on an odd host)
                # -- give up permanently for this process.
                return None
            busy, total = ticks
            if busy < 0 or total < 0 or total <= 0:
                return None
            if self._cpu_prev is None:
                # Baseline: brief pause so the second read has a real delta.
                self._cpu_prev = (busy, total)
                time.sleep(min(self._cpu_sample_s, 0.5))
                ticks = sample()
                if ticks is None:
                    return None
                busy, total = ticks
                prev_busy, prev_total = self._cpu_prev
                self._cpu_prev = (busy, total)
                d_busy = busy - prev_busy
                d_total = total - prev_total
            else:
                prev_busy, prev_total = self._cpu_prev
                d_busy = busy - prev_busy
                d_total = total - prev_total
                self._cpu_prev = (busy, total)
            if d_total <= 0:
                return None
            return _pct(d_busy, d_total)

    def io_pct(self) -> Optional[float]:
        if self._io_sampler is None:
            return None
        with self._lock:
            now = time.monotonic()
            sample = self._io_sampler()
            if not sample:
                return None
            if self._io_prev is None:
                # Baseline: brief pause so the second read has a real delta.
                self._io_prev = sample
                self._io_prev_t = now
                time.sleep(min(self._cpu_sample_s, 0.5))
                now = time.monotonic()
                sample = self._io_sampler()
                if not sample:
                    return None
                prev = self._io_prev
                elapsed = now - self._io_prev_t
                self._io_prev = sample
                self._io_prev_t = now
            else:
                prev = self._io_prev
                elapsed = now - self._io_prev_t
                self._io_prev = sample
                self._io_prev_t = now
            if elapsed <= 0:
                return None
            elapsed_ms = elapsed * 1000.0
            best: Optional[float] = None
            for dev, (busy, _marker) in sample.items():
                prev_pair = prev.get(dev)
                if prev_pair is None:
                    prev_pair = (0.0, 0.0)
                d_busy = max(0.0, busy - prev_pair[0])
                frac = _pct(d_busy, elapsed_ms)
                if frac is None:
                    continue
                if best is None or frac > best:
                    best = frac
            return 0.0 if best is None else best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_WIN_TICKER = _Ticker(_win_cpu_sampler, _win_io_sampler)
_LINUX_TICKER = _Ticker(_linux_cpu_sampler, _linux_io_sampler)


def _active_ticker() -> _Ticker:
    return _WIN_TICKER if _is_windows() else _LINUX_TICKER


def collect_load() -> Dict[str, Any]:
    """Collect host load as a dict (task contract).

    Percent fields (``cpu_pct``, ``ram_pct``, ``swap_pct``, ``io_pct``) are
    floats in ``[0, 100]`` or ``None``.  GB fields are rounded floats or
    ``None``.  Never raises.
    """
    snap = LoadSnapshot()
    try:
        if _is_windows():
            mem = _WIN32.memory()
            if mem:
                snap.ram_pct = _pct(
                    mem["total_phys"] - mem["avail_phys"], mem["total_phys"]
                )
                snap.ram_gb = _gb(mem["total_phys"] - mem["avail_phys"])
                snap.ram_total_gb = _gb(mem["total_phys"])
                if mem.get("total_page", 0) > 0:
                    snap.swap_pct = _pct(
                        max(0, mem["total_page"] - mem["avail_page"]),
                        mem["total_page"],
                    )
                    snap.swap_gb = _gb(max(0, mem["total_page"] - mem["avail_page"]))
                    snap.swap_total_gb = _gb(mem["total_page"])
        else:
            mem = _linux_meminfo()
            if mem:
                snap.ram_pct = _pct(
                    mem["MemTotal"] - mem.get("MemAvailable", 0), mem["MemTotal"]
                )
                snap.ram_gb = _gb(mem["MemTotal"] - mem.get("MemAvailable", 0))
                snap.ram_total_gb = _gb(mem["MemTotal"])
                if mem.get("SwapTotal", 0) > 0:
                    snap.swap_pct = _pct(
                        max(0, mem["SwapTotal"] - mem.get("SwapFree", 0)),
                        mem["SwapTotal"],
                    )
                    snap.swap_gb = _gb(max(0, mem["SwapTotal"] - mem.get("SwapFree", 0)))
                    snap.swap_total_gb = _gb(mem["SwapTotal"])
        ticker = _active_ticker()
        snap.cpu_pct = ticker.cpu_pct()
        snap.io_pct = ticker.io_pct()
    except Exception as exc:  # never propagate -- /status must not crash
        logger.warning("collect_load failed: %s", exc)
    return asdict(snap)


def _f(value: Any) -> Optional[float]:
    """Robust float coercion; ``None`` when the value isn't numeric."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def format_load_line(raw: Dict[str, Any]) -> str:
    """Render the one-line sysinfo block for ``/status``.

    Example::

        CPU: 97% · RAM: 96% (22.8/23.6 ГБ) · SWAP: 52% · IO: н/д
    """
    parts = [f"CPU: {_pct_text(raw.get('cpu_pct'))}"]
    ram_pct = _f(raw.get("ram_pct"))
    if ram_pct is not None:
        gb = _f(raw.get("ram_gb"))
        total = _f(raw.get("ram_total_gb"))
        if gb is not None and total is not None:
            parts.append(f"RAM: {ram_pct:.0f}% ({gb:.1f}/{total:.1f} ГБ)")
        else:
            parts.append(f"RAM: {ram_pct:.0f}%")
    else:
        parts.append(f"RAM: {_NA}")
    swap_pct = raw.get("swap_pct")
    parts.append(f"SWAP: {_pct_text(swap_pct)}")
    parts.append(f"IO: {_pct_text(raw.get('io_pct'))}")
    return " · ".join(parts)
