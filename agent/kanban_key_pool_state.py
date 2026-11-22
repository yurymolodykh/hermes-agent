"""Reading and writing the card-key pool state file.

Two on-disk shapes must stay readable, and a rollback must not lose every card
its key, so the format is deliberately backward compatible in both directions:

* **legacy (v1)** — ``reservations[str(key_id)] = {task_id, pid, ...}``, one flat
  record per key, i.e. the key was exclusive. It is exactly one slot, so it is
  read as a one-element ``slots`` list.
* **slots (v2)** — ``reservations[str(key_id)] = {key_id, key_label, slots: [...]}``.

Writing takes the same care: a key holding one card is still written as the flat
v1 record, so a worker started by the previous build during a rolling restart
keeps reading the file.

Split out of ``agent/kanban_key_pool.py`` — that facade is over its line ratchet
and this is a self-contained topic (no reservation logic, only the file shape).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from agent import kanban_key_pool_usage as _usage


def read_state(path: Path) -> Dict[str, Any]:
    """Read the state file and fold it into the *slot* form (see module doc)."""
    empty = {
        "version": 2,
        "reservations": {},
        "failed_keys": {},
        "rr_cursor": None,
        "usage": {},
    }
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A torn state file must not block new work: the keys themselves live
        # in a different file and every reservation is recoverable.
        return dict(empty)
    if not isinstance(raw, dict):
        return dict(empty)
    stored = raw.get("reservations")
    if not isinstance(stored, dict):
        stored = {}
    reservations: Dict[str, Any] = {}
    for key, entry in stored.items():
        folded = fold_reservation(key, entry)
        if folded is not None:
            reservations[str(key)] = folded
    failed_keys = raw.get("failed_keys")
    if not isinstance(failed_keys, dict):
        failed_keys = {}
    # ``rr_cursor`` is the rotation cursor: the id of the last key handed out.
    # It lives here, in the same file as ``reservations`` and ``failed_keys``,
    # so a restarted process continues the same ring instead of starting over
    # at the lowest id (a second state file would drift out of sync with this
    # one on any partial write). Absent in a v1 file: ``None`` means "no turn
    # taken yet" and the ring starts at the lowest free id.
    cursor_raw = raw.get("rr_cursor")
    try:
        rr_cursor: Optional[int] = None if cursor_raw is None else int(cursor_raw)
    except (TypeError, ValueError):
        rr_cursor = None
    # ``usage`` (per-key busy time for the current Moscow day) is optional: a
    # state file written before the fairness change has none, and it must read
    # exactly as before — no reservations lost, no bucket invented.
    return {
        "version": 2,
        "reservations": reservations,
        "failed_keys": failed_keys,
        "rr_cursor": rr_cursor,
        "usage": _usage.filter_today(raw.get("usage")),
    }


def fold_reservation(key: Any, entry: Any) -> Optional[Dict[str, Any]]:
    """Normalise one stored key record to ``{key_id, key_label, slots}``."""
    if not isinstance(entry, dict):
        return None
    try:
        key_id = int(entry.get("key_id") or key)
    except (TypeError, ValueError):
        return None
    label = str(entry.get("key_label") or f"key-{key_id}")
    raw_slots = entry.get("slots")
    if isinstance(raw_slots, list):
        slots = [dict(s) for s in raw_slots if isinstance(s, dict)]
    else:
        # Legacy flat record: every field except the key's own two belongs to
        # the single slot the key used to hold exclusively.
        slots = [
            {k: v for k, v in entry.items() if k not in ("key_id", "key_label")}
        ]
    return {"key_id": key_id, "key_label": label, "slots": slots}


def key_slots(entry: Any) -> List[Dict[str, Any]]:
    """The live slots of one key record (empty for anything unusable)."""
    if not isinstance(entry, dict):
        return []
    slots = entry.get("slots")
    if not isinstance(slots, list):
        return []
    return [s for s in slots if isinstance(s, dict)]


def serialize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Render the in-memory slot state for disk, keeping v1 readable.

    One slot → the flat v1 record (a previous build can still read it, and so
    can every test that inspects the file directly). Two or more slots → the v2
    ``slots`` list. A key with no slots is dropped entirely instead of being
    written as a husk.
    """
    out_reservations: Dict[str, Any] = {}
    for key, entry in (state.get("reservations") or {}).items():
        if not isinstance(entry, dict):
            continue
        slots = key_slots(entry)
        if not slots:
            continue
        key_id = entry.get("key_id")
        label = entry.get("key_label") or f"key-{key_id}"
        if len(slots) == 1:
            flat = dict(slots[0])
            flat["key_id"] = key_id
            flat["key_label"] = label
            out_reservations[str(key)] = flat
        else:
            out_reservations[str(key)] = {
                "key_id": key_id,
                "key_label": label,
                "slots": [dict(s) for s in slots],
            }
    out: Dict[str, Any] = {
        "version": 2,
        "reservations": out_reservations,
        "failed_keys": dict(state.get("failed_keys") or {}),
        "rr_cursor": state.get("rr_cursor"),
    }
    # Only written when there is something to say: an install that never
    # accumulated a bucket keeps the exact state file it had before this change.
    usage = state.get("usage")
    if isinstance(usage, dict) and usage:
        out["usage"] = {str(k): dict(v) for k, v in usage.items() if isinstance(v, dict)}
    return out


def write_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomic replace so a crash never leaves half a reservation behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(serialize_state(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:  # keys and state are both sensitive-ish; keep them owner-only
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs
        pass
