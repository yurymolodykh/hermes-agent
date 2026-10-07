"""Memory status rollup for ``/api/status`` (NS-656).

The gateway already *produces* every memory-pressure signal a user would
want to know about, but all of it dies in log files:

* :func:`gateway.shutdown_watchdog.write_loop_heartbeat` embeds a
  :func:`gateway.lifecycle_ledger.sample_memory` snapshot (gateway RSS +
  system MemAvailable/MemTotal + swap) in ``state/gateway.heartbeat``
  every 30 seconds.
* :func:`gateway.lifecycle_ledger.record_startup` detects an unclean
  previous death and flags ``suspected_oom`` — but only into
  ``gateway-exit-diag.log`` and a WARNING line.
* ``gateway/agent_cache_pressure.py`` evicts transcripts under pressure,
  again log-only.

So a hosted agent can be OOM-killed hourly (the BlueAtlas incident,
NS-608) while its dashboard and the NAS agent card both look perfectly
healthy.  This module is the read side that closes the gap: it distills
the *already-persisted* heartbeat + lifecycle sentinel into a compact,
public-safe block that ``/api/status`` can serve to the dashboard SPA
and the NAS availability sweep — no new sampling, no IPC with the
gateway process, just two small file reads.

Public-safety note: ``/api/status`` is an unauthenticated liveness probe
(``PUBLIC_API_PATHS``), which is exactly why NAS can consume it.  This
block therefore carries only coarse numbers (MB granularity), enums, and
booleans — the same disclosure class as the existing ``active_agents``
count and ``nous_session_valid`` field (which was added for the same
NAS-sweep audience).

Everything here is best-effort and read-only: a missing/corrupt file
degrades to ``pressure="unknown"`` rather than raising into the status
endpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Pressure thresholds on system MemAvailable.  The absolute KiB floors
# deliberately mirror the lifecycle ledger's OOM-suspicion heuristics
# (:data:`gateway.lifecycle_ledger._LOW_MEM_AVAILABLE_KIB` /
# ``_LOW_MEM_AVAILABLE_FRACTION``): if a memory level would make a
# subsequent unclean death "suspected OOM", the user should already have
# been warned at that level while the process was still alive.
# The fraction thresholds are unified with the worker-limit dashboard
# (M1): critical below 10% of MemTotal, elevated below 20%.
_CRITICAL_AVAILABLE_KIB = 64 * 1024  # < 64 MiB available
_CRITICAL_AVAILABLE_FRACTION = 0.10  # < 10% of MemTotal
_ELEVATED_AVAILABLE_KIB = 128 * 1024  # < 128 MiB available
_ELEVATED_AVAILABLE_FRACTION = 0.20  # < 20% of MemTotal

# Swap-pressure thresholds (M1, worker-limit mission): swap is a second
# independent pressure signal.  A host can sit at 75% RAM available yet
# thrash on a swap file that is nearly full, so swap can raise the pressure
# class on its own.  Strictly-greater comparisons: 40% used is still ok,
# 60% used is elevated (not yet critical).
_ELEVATED_SWAP_FRACTION = 0.40  # > 40% of swap used
_CRITICAL_SWAP_FRACTION = 0.60  # > 60% of swap used

# A heartbeat older than this no longer describes the present.  The writer
# cadence is 30s (DEFAULT_HEARTBEAT_INTERVAL_S); 150s of slack tolerates a
# briefly stalled loop without letting a long-dead gateway's last sample
# masquerade as current pressure.
_HEARTBEAT_FRESH_TTL_S = 150.0

_KIB_PER_MB = 1024


def _mb(kib: Any) -> Optional[int]:
    if isinstance(kib, bool) or not isinstance(kib, int) or kib < 0:
        return None
    return kib // _KIB_PER_MB


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_pressure(
    available_kib: Any,
    total_kib: Any,
    swap_used_kib: Any = None,
    swap_total_kib: Any = None,
) -> str:
    """Map memory + swap to ``ok``/``elevated``/``critical``.

    ``unknown`` when the sample is missing or malformed — the caller must
    not treat "we could not read it" as "memory is fine".

    Available-memory thresholds (absolute KiB floors + fractions, aligned
    with the worker-limit dashboard at 10% critical / 20% elevated) are the
    primary pressure signal; ``swap_used_kib``/``swap_total_kib`` add a
    second, independent pressure signal (M1): swap usage strictly above 40%
    forces ``elevated``, strictly above 60% forces ``critical``, regardless
    of how much RAM is free.  Swap contributes nothing when its total is
    missing or zero.
    """
    if (
        isinstance(available_kib, bool)
        or not isinstance(available_kib, int)
        or available_kib < 0
    ):
        return "unknown"
    fraction: Optional[float] = None
    if (
        not isinstance(total_kib, bool)
        and isinstance(total_kib, int)
        and total_kib > 0
    ):
        fraction = available_kib / total_kib
    swap_fraction: Optional[float] = None
    if (
        not isinstance(swap_used_kib, bool)
        and isinstance(swap_used_kib, int)
        and swap_used_kib >= 0
        and not isinstance(swap_total_kib, bool)
        and isinstance(swap_total_kib, int)
        and swap_total_kib > 0
    ):
        swap_fraction = swap_used_kib / swap_total_kib
    if (
        available_kib < _CRITICAL_AVAILABLE_KIB
        or (fraction is not None and fraction < _CRITICAL_AVAILABLE_FRACTION)
        or (
            swap_fraction is not None
            and swap_fraction > _CRITICAL_SWAP_FRACTION
        )
    ):
        return "critical"
    if (
        available_kib < _ELEVATED_AVAILABLE_KIB
        or (fraction is not None and fraction < _ELEVATED_AVAILABLE_FRACTION)
        or (
            swap_fraction is not None
            and swap_fraction > _ELEVATED_SWAP_FRACTION
        )
    ):
        return "elevated"
    return "ok"


def _read_heartbeat(home: Optional[Path]) -> Optional[Dict[str, Any]]:
    try:
        from gateway.lifecycle_ledger import _read_json
        from gateway.shutdown_watchdog import get_loop_heartbeat_path

        return _read_json(get_loop_heartbeat_path(home))
    except Exception:
        return None


def _read_sentinel(home: Optional[Path]) -> Optional[Dict[str, Any]]:
    try:
        from gateway.lifecycle_ledger import (
            _read_json,
            get_lifecycle_sentinel_path,
        )

        return _read_json(get_lifecycle_sentinel_path(home))
    except Exception:
        return None


def collect_memory_status(
    home: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the ``memory`` block for ``/api/status``.

    ``home`` scopes the read to a profile's HERMES_HOME (the status
    endpoint's ``?profile=`` handling passes it through); ``None`` means
    the active profile.  ``now`` is injectable for tests.

    Always returns a dict — a gateway that is down, has never written a
    heartbeat, or whose files are corrupt yields
    ``{"pressure": "unknown", ...}`` with whatever fields could still be
    recovered.  Never raises.
    """
    moment = now or datetime.now(timezone.utc)
    status: Dict[str, Any] = {
        "pressure": "unknown",
        "gateway_rss_mb": None,
        "system_total_mb": None,
        "system_available_mb": None,
        "swap_used_mb": None,
        "sampled_at": None,
        "last_boot_unclean": False,
        "last_boot_suspected_oom": False,
        # Identity of the CURRENT gateway life (the sentinel's started_at).
        # A suspected-OOM restart writes a fresh sentinel, so this changes on
        # every boot — the dashboard keys banner dismissal on it so that
        # acknowledging one OOM restart does not mute reports of the NEXT
        # one (the hourly-restart-loop case is exactly the one that matters).
        "boot_id": None,
    }

    heartbeat = _read_heartbeat(home)
    if heartbeat:
        sampled_at = _parse_iso(heartbeat.get("updated_at"))
        mem = heartbeat.get("mem")
        if isinstance(mem, dict):
            status["gateway_rss_mb"] = _mb(mem.get("rss_kib"))
            status["system_total_mb"] = _mb(mem.get("mem_total_kib"))
            status["system_available_mb"] = _mb(mem.get("mem_available_kib"))
            status["swap_used_mb"] = _mb(mem.get("swap_used_kib"))
            if sampled_at is not None:
                status["sampled_at"] = sampled_at.isoformat()
                age_s = (moment - sampled_at).total_seconds()
                if 0 <= age_s <= _HEARTBEAT_FRESH_TTL_S:
                    status["pressure"] = classify_pressure(
                        mem.get("mem_available_kib"),
                        mem.get("mem_total_kib"),
                    )
                # else: stale sample — numbers are still reported (they are
                # honest about *when* via sampled_at) but pressure stays
                # "unknown" so a dead gateway's final gasp cannot render a
                # live "critical" banner forever.

    sentinel = _read_sentinel(home)
    if sentinel:
        status["last_boot_unclean"] = bool(sentinel.get("prior_unclean_exit"))
        status["last_boot_suspected_oom"] = bool(
            sentinel.get("prior_suspected_oom")
        )
        started_at = sentinel.get("started_at")
        if isinstance(started_at, str) and started_at:
            status["boot_id"] = started_at

    return status
