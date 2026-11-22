"""Per-key busy-time accounting for the card key pool.

Fairness between card keys is measured in **occupied time**, not in hand-outs.
A key that served three one-minute cards was not "more used" than a key that
served one three-hour card, so counting hand-outs would keep punishing the key
that happens to draw the long jobs. This sibling owns that bookkeeping:

``state["usage"][key] = {"busy_seconds": float, "cards": int, "window": "YYYY-MM-DD"}``

accumulated for the current **Moscow** calendar day, and reset when the day
rolls over. It is charged exactly once per slot, from the three places that
remove a slot (stale sweep, named task release, dead-pid release), which is
what makes double charging impossible: a slot lives in one list, and the caller
removes it in the same branch that accounts for it.

Split out of ``agent/kanban_key_pool.py`` on purpose — that facade is over its
line ratchet, and this is a self-contained topic with no dependency on the
reservation machinery beyond a plain state dict.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Dict
from typing import Optional

#: Moscow is UTC+3 with no DST, so a fixed offset is exact rather than a
#: shortcut. A host-local date would make the bucket boundary depend on where
#: the dispatcher runs, and the owner asked for Moscow days specifically.
USAGE_TZ = timezone(timedelta(hours=3))

#: Two keys whose busy time differs by no more than this are treated as equally
#: loaded, so the round-robin keeps turning instead of alternating on rounding
#: noise between reservations that were seconds apart.
USAGE_TOLERANCE_SECONDS = 60.0


def usage_today(now: Optional[float] = None) -> str:
    """The Moscow calendar date (``YYYY-MM-DD``) a usage bucket belongs to."""
    stamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(stamp, USAGE_TZ).strftime("%Y-%m-%d")


def usage_bucket(state: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Today's ``{key_id: {busy_seconds, cards, window}}``, rolling over in place.

    Rollover is *reset*, not "carry yesterday forward": the owner's metric is
    "how busy was each key today", so yesterday's imbalance must not keep
    steering today's rotation.
    """
    today = usage_today(now)
    usage = state.get("usage")
    if not isinstance(usage, dict):
        usage = {}
        state["usage"] = usage
    for key in list(usage.keys()):
        record = usage.get(key)
        if not isinstance(record, dict) or record.get("window") != today:
            usage.pop(key, None)
    return usage


def account_slot(
    state: Dict[str, Any],
    key_id: Any,
    slot: Dict[str, Any],
    *,
    now: float,
    ttl_seconds: float,
) -> None:
    """Charge one DROPPED slot's busy time to its key.

    Called from every place that removes a slot — sweep, task release, pid
    release — and only there. ``seconds`` is ``now - reserved_at`` clamped to
    ``[0, ttl_seconds]`` so a clock jump or a corrupt ``reserved_at`` cannot
    invent hours of usage.
    """
    try:
        kid = int(key_id)
    except (TypeError, ValueError):
        return
    try:
        reserved_at = float(slot.get("reserved_at") or 0)
    except (TypeError, ValueError):
        reserved_at = 0.0
    seconds = 0.0
    if reserved_at > 0:
        seconds = min(max(float(now) - reserved_at, 0.0), float(ttl_seconds))
    bucket = usage_bucket(state, now)
    record = bucket.get(str(kid))
    if not isinstance(record, dict):
        record = {"busy_seconds": 0.0, "cards": 0, "window": usage_today(now)}
        bucket[str(kid)] = record
    try:
        record["busy_seconds"] = float(record.get("busy_seconds") or 0.0) + seconds
    except (TypeError, ValueError):
        record["busy_seconds"] = seconds
    try:
        record["cards"] = int(record.get("cards") or 0) + 1
    except (TypeError, ValueError):
        record["cards"] = 1


def busy_seconds(state: Dict[str, Any], key_id: int, now: Optional[float] = None) -> float:
    """Today's accumulated busy seconds for ``key_id`` (``0.0`` when it has none)."""
    bucket = usage_bucket(state, now)
    record = bucket.get(str(int(key_id)))
    if not isinstance(record, dict):
        return 0.0
    try:
        return float(record.get("busy_seconds") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def lookup(usage: Dict[str, Any], key_id: int, today: str) -> float:
    """Read-only busy seconds in an already-rolled-over bucket.

    The read-only twin of :func:`busy_seconds`: the picker must not mutate
    state while it is only *choosing* a key — the write happens under the lock
    right after, with the reservation it picked.
    """
    record = usage.get(str(int(key_id)))
    if not isinstance(record, dict) or record.get("window") != today:
        return 0.0
    try:
        return float(record.get("busy_seconds") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def filter_today(raw: Any) -> Dict[str, Any]:
    """The ``usage`` map a state file may carry, keeping only today's buckets.

    Buckets from an earlier day are dropped on read rather than at pick time,
    so every reader (the status CLI included) sees one consistent window.
    """
    today = usage_today()
    out: Dict[str, Any] = {}
    if isinstance(raw, dict):
        for key, record in raw.items():
            if isinstance(record, dict) and record.get("window") == today:
                out[str(key)] = dict(record)
    return out


def decorate_status_rows(
    rows: list, usage: Dict[str, Any], now: Optional[float] = None
) -> None:
    """Add ``busy_seconds`` / ``cards`` / ``usage_window`` to status rows.

    ``hermes kanban keypool status`` must show the number the picker balances
    on; an even-looking ``used`` column alone is not evidence of even load.
    """
    today = usage_today(now)
    for row in rows:
        record = usage.get(str(row.get("key_id")))
        if not isinstance(record, dict):
            record = {}
        try:
            row["busy_seconds"] = round(float(record.get("busy_seconds") or 0.0), 1)
        except (TypeError, ValueError):
            row["busy_seconds"] = 0.0
        try:
            row["cards"] = int(record.get("cards") or 0)
        except (TypeError, ValueError):
            row["cards"] = 0
        row["usage_window"] = today
