"""FAST_RESTART stats — durable one-line stats marker for the fast-restart path.

Path A1 (``/restart fast`` / ``hermes gateway restart --fast`` /
``HERMES_GATEWAY_FAST_RESTART=1`` / the fast-restart marker file) stops the
gateway in-band and pre-marks sessions ``resume_pending``.  The
``FAST_RESTART`` marker logged at stop time carries the *downtime target*;
this module supplies the *observer* side that the next gateway boot emits
once recovery is done:

    FAST_RESTART_STATS {"total_downtime": 12.4, "resumed_cron": 1,
                        "reclaimed_cards": 1, "shutdown_ts": "...",
                        "ready_ts": "..."}

The three named counters answer the operator questions A3 asks:

* ``total_downtime``  — wall-clock between the previous gateway's fast stop
  and this process reporting ready (seconds, float).
* ``resumed_cron``    — cron jobs with a durable ``resume_token`` that were
  re-armed to continue their interrupted session on this boot
  (``rearm_resumable_cron_jobs`` staged count, plus the execution-ledger
  recovery count from ``recover_interrupted_executions``).
* ``reclaimed_cards`` — kanban cards whose claim belonged to the *previous*
  gateway generation and were returned to ``ready`` by this boot's first
  dispatch pass (``generation_reclaimed``).

Mechanics
---------
* On a fast stop, :func:`record_shutdown` persists a small JSON stamp to
  ``<HERMES_HOME>/.fast_restart_shutdown.json`` (idempotent — a second full
  stop overwrites it).  The stamp is removed when the next boot emits stats,
  so a boot that did NOT follow a fast stop is silent.
* During boot, the cron recovery pass and the kanban dispatcher tick call
  :func:`record_cron_recovered` / :func:`record_cards_reclaimed` (thread
  safe — the dispatcher tick and cron thread are separate threads).
* Once the gateway is up, :func:`emit_stats_if_fast_restart` logs the single
  JSON line and clears the stamp.  All counters are monotonic-across-boots by
  counting *this boot* only; the stamp file scopes the window to the restart
  that precedes it.

The module is import-safe (stdlib + hermes_constants only) so cron/ and
gateway/ workers can call it without circular imports.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

_SHUTDOWN_STAMP_FILENAME = ".fast_restart_shutdown.json"

_lock = threading.RLock()
# Counters accumulated during the CURRENT process (reset at import; values
# are only meaningful when a fast-restart stamp exists).
_cron_recovered = 0
_cards_reclaimed = 0
_shutdown_stamp: Optional[Dict[str, Any]] = None


def _stamp_path() -> str:
    return str(get_hermes_home() / _SHUTDOWN_STAMP_FILENAME)


def record_shutdown(
    *,
    ts_utc_iso: str,
    active_agents: int = 0,
    active_cron: int = 0,
    active_api_runs: int = 0,
) -> None:
    """Persist the fast-stop stamp so the next boot can measure downtime.

    Only call on the fast path (stop(fast=True)); a regular stop must not
    leave a stamp lying around for the next boot.
    """
    global _shutdown_stamp
    stamp = {
        "ts": ts_utc_iso,
        "active_agents": int(active_agents),
        "active_cron": int(active_cron),
        "active_api_runs": int(active_api_runs),
    }
    with _lock:
        _shutdown_stamp = stamp
    try:
        path = _stamp_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stamp, f)
        os.replace(tmp, path)
    except Exception:
        # Stats are best-effort — a failed stamp must never crash a stop().
        pass


def _read_stamp() -> Optional[Dict[str, Any]]:
    try:
        with open(_stamp_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def record_cron_recovered(count: int) -> None:
    """Accumulate interrupted-cron recoveries staged by this boot."""
    if count <= 0:
        return
    with _lock:
        global _cron_recovered
        _cron_recovered += int(count)


def record_cards_reclaimed(count: int) -> None:
    """Accumulate dead-generation kanban cards reclaimed by this boot."""
    if count <= 0:
        return
    with _lock:
        global _cards_reclaimed
        _cards_reclaimed += int(count)


def _iso_to_epoch(iso_val: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(iso_val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _emit_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clear_stamp() -> None:
    """Remove any pending fast-restart stamp (used on non-fast boots/fixtures)."""
    with _lock:
        _shutdown_stamp = None
    try:
        path = _stamp_path()
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


def emit_stats_if_fast_restart() -> Dict[str, Any] | None:
    """Log the single FAST_RESTART_STATS JSON line if this boot follows a fast stop.

    Returns the emitted dict (or ``None`` when there is no pending stamp —
    i.e. this boot did not follow a fast restart, so we stay silent).
    """
    stamp = _read_stamp()
    if not stamp:
        return None

    with _lock:
        global _cron_recovered, _cards_reclaimed, _shutdown_stamp
        cron_n = _cron_recovered
        cards_n = _cards_reclaimed
        _cron_recovered = 0
        _cards_reclaimed = 0
        _shutdown_stamp = None

    shutdown_ts = stamp.get("ts") or ""
    shutdown_epoch = _iso_to_epoch(shutdown_ts)
    ready_ts = _emit_now()
    total_downtime = 0.0
    if shutdown_epoch is not None:
        total_downtime = round(max(0.0, datetime.now(timezone.utc).timestamp() - shutdown_epoch), 2)

    stats = {
        "total_downtime": total_downtime,
        "resumed_cron": cron_n,
        "reclaimed_cards": cards_n,
        "shutdown_ts": shutdown_ts,
        "ready_ts": ready_ts,
    }
    try:
        import logging

        logging.getLogger("gateway").info("FAST_RESTART_STATS %s", json.dumps(stats, ensure_ascii=False))
    except Exception:
        pass

    # Clear the stamp so we never double-emit on a later boot.
    try:
        path = _stamp_path()
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass

    return stats
