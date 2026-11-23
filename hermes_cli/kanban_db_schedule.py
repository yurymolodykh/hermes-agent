"""Wake-up by scheduled time for parked cards.

A card in ``scheduled`` is "waiting on time, not a human" — but until this module
existed nothing woke it: the only record of the intended time was prose in a
comment (``SCHEDULED_UNTIL=<ISO8601>``) or in the title, and the dispatcher never
looked at it. A parked card therefore lived forever until an operator unblocked it
by hand.

This module owns the whole mechanism and nothing else:

* ``scheduled_until`` (epoch seconds, NULL = no deadline) on the ``tasks`` row —
  the deadline is data, so the dispatcher can query it instead of parsing prose;
* :func:`parse_until` for the public surfaces (CLI ``--until``, the
  ``kanban_schedule`` tool) — ISO-8601 or epoch, naive input read in the
  profile's timezone (``hermes_time``), never the host's;
* :func:`wake_due_tasks` — the dispatcher-tick poll that promotes every card whose
  deadline has passed. Catch-up is inherent: the query is ``<= now``, so a tick
  that arrives late (gateway down for hours) wakes the card exactly once on the
  next tick instead of replaying a backlog, because waking CLEARS the column;
* :func:`legacy_deadline_from_comments` — the compatibility read for cards parked
  before the column existed (one transition period; the first wake writes down
  what it parsed).

Nothing here changes status semantics: ``scheduled`` still means "parked", the
wake only moves a card back through the SAME re-gate operator ``unblock`` uses.

Split module: late-imports ``hermes_cli.kanban_db`` as ``_kb`` (import cycle) and
keeps new behaviour out of the facade, which may only shrink.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# ``SCHEDULED_UNTIL=2026-11-25T04:40:00+03:00`` (also bare, no key, in parentheses).
_UNTIL_RE = re.compile(
    r"SCHEDULED_UNTIL\s*[=:]\s*(?P<value>[0-9T:+\-. Z/]{8,40})", re.IGNORECASE
)
# ``25.11 04:40`` / ``25.11.2026 04:40 МСК`` in a title or note — date-time in MSK.
_HUMAN_RE = re.compile(
    r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})(?:\.(?P<y>\d{4}))?\s+(?P<hh>\d{1,2}):(?P<mm>\d{2})"
)

# How many trailing comments to scan for a legacy marker. A card's own note is
# written at park time, so a small window is enough and keeps the tick cheap.
_LEGACY_SCAN_COMMENTS = 6


def _hermes_time():
    import hermes_time

    return hermes_time


def profile_tz():
    """The active profile's timezone (``None`` = server-local, per ``hermes_time``)."""
    return _hermes_time().get_timezone()


def format_local(epoch: Optional[int]) -> str:
    """``2026-11-25 04:40 MSK`` — the profile-local rendering used in comments/events."""
    if not epoch:
        return ""
    ht = _hermes_time()
    tz = profile_tz()
    dt = datetime.fromtimestamp(int(epoch), tz) if tz is not None else datetime.fromtimestamp(int(epoch))
    return ht.safe_strftime(dt, "%Y-%m-%d %H:%M %Z").strip()


def parse_until(value: Any, *, now: Optional[int] = None) -> Optional[int]:
    """Epoch seconds from an ISO-8601 string or an epoch number; ``None`` when unparseable.

    A value WITHOUT an offset is read as wall-clock time in the profile's timezone
    (``Europe/Moscow`` on this host) — the operational time every card, journal and
    comment uses — never as UTC and never as the host zone. Relative forms are
    deliberately not accepted: a deadline that depends on when you typed it cannot
    be read back off the board.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,}", text):
        return int(text)
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        tz = profile_tz()
        dt = dt.replace(tzinfo=tz if tz is not None else datetime.now().astimezone().tzinfo)
    return int(dt.timestamp())


def legacy_deadline_from_comments(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
) -> Optional[int]:
    """Deadline parsed out of the card's own notes, for cards parked before the column.

    Compatibility only: an explicit ``SCHEDULED_UNTIL=`` marker wins, then a
    ``DD.MM HH:MM`` (optionally ``.YYYY``) date written in the profile timezone.
    A date without a year is read in the current year, or the next one when that
    would put it in the past by more than half a year — a card parked on 31.12 for
    "05.01" is waiting for the coming January, not for ten months ago.
    """
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT ?",
        (task_id, _LEGACY_SCAN_COMMENTS),
    ).fetchall()
    for row in rows:
        text = row["body"] or ""
        hit = _UNTIL_RE.search(text)
        if hit:
            epoch = parse_until(hit.group("value").strip().rstrip(".,;"), now=now)
            if epoch is not None:
                return epoch
    current = int(now if now is not None else time.time())
    tz = profile_tz()
    for row in rows:
        hit = _HUMAN_RE.search(row["body"] or "")
        if not hit:
            continue
        year = int(hit.group("y")) if hit.group("y") else datetime.fromtimestamp(
            current, tz if tz is not None else timezone.utc
        ).year
        try:
            dt = datetime(
                year, int(hit.group("m")), int(hit.group("d")),
                int(hit.group("hh")), int(hit.group("mm")),
                tzinfo=tz if tz is not None else datetime.now().astimezone().tzinfo,
            )
        except ValueError:
            continue
        epoch = int(dt.timestamp())
        if not hit.group("y") and epoch < current - 180 * 86400:
            try:
                epoch = int(dt.replace(year=year + 1).timestamp())
            except ValueError:
                continue
        return epoch
    return None


def resolve_deadline(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
) -> Optional[int]:
    """The card's deadline: the column when set, else the legacy note marker."""
    row = conn.execute(
        "SELECT scheduled_until FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is not None and _kb_row_value(row, "scheduled_until"):
        return int(_kb()._to_epoch(row["scheduled_until"]) or 0) or None
    return legacy_deadline_from_comments(conn, task_id, now=now)


def _kb_row_value(row: Any, col: str) -> Any:
    try:
        return row[col]
    except (IndexError, KeyError):
        return None


def _kb():
    from hermes_cli import kanban_db

    return kanban_db


def set_scheduled_until(conn: sqlite3.Connection, task_id: str, until: Optional[int]) -> bool:
    """Store the deadline on the card row; False for an unknown id.

    Runs inside its own txn; safe to call from a surface that just parked the card
    (``schedule_task`` already committed) or standalone.
    """
    kb = _kb()
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET scheduled_until = ? WHERE id = ?",
            (int(until) if until else None, task_id),
        )
        return cur.rowcount == 1


def clear_scheduled_until(conn: sqlite3.Connection, task_id: str) -> None:
    """Drop the deadline (the card left ``scheduled``: unblock, wake, completion)."""
    conn.execute("UPDATE tasks SET scheduled_until = NULL WHERE id = ?", (task_id,))


def due_tasks(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[tuple[str, int]]:
    """``[(task_id, scheduled_until)]`` for parked cards whose deadline has passed.

    Two sources, one list: the ``scheduled_until`` column, then — for cards parked
    before it existed — the legacy marker in their own notes. The legacy read is
    capped by ``_LEGACY_SCAN_COMMENTS`` and only runs for deadline-less parked
    cards, so the tick stays cheap on a board with no legacy rows.
    """
    stamp = int(now if now is not None else time.time())
    rows = conn.execute(
        "SELECT id, scheduled_until FROM tasks "
        "WHERE status = 'scheduled' "
        "ORDER BY scheduled_until ASC"
    ).fetchall()
    due: list[tuple[str, int]] = []
    legacy: list[str] = []
    for row in rows:
        raw = row["scheduled_until"]
        if raw is None:
            legacy.append(row["id"])
            continue
        epoch = _kb()._to_epoch(raw)
        if epoch is not None and int(epoch) <= stamp:
            due.append((row["id"], int(epoch)))
    for task_id in legacy:
        epoch = legacy_deadline_from_comments(conn, task_id, now=stamp)
        if epoch is not None and epoch <= stamp:
            due.append((task_id, epoch))
    return due


def wake_due_tasks(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[str]:
    """Promote every parked card whose deadline has passed; returns the woken ids.

    One tick, one write txn. Each card is woken at most once: the deadline column
    is cleared in the same statement that moves the status, so a second tick — or
    the catch-up tick after hours of downtime — finds nothing left to wake (no
    duplicate wake-ups, no replay storm). Landing goes through the operator
    re-gate (``_landing_status_after_parents``) so a card whose upstream is still
    open returns to ``todo``, exactly like ``unblock``.

    The ``woken_by_timer`` event carries epoch AND the Moscow rendering, and is
    NOT booked as a failure: this is a scheduled transition, not a retry.
    """
    due = due_tasks(conn, now=now)
    if not due:
        return []
    kb = _kb()
    stamp = int(now if now is not None else time.time())
    woken: list[str] = []
    notes: list[tuple[str, str]] = []
    with kb.write_txn(conn):
        for task_id, epoch in due:
            row = conn.execute(
                "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None or row["status"] != "scheduled":
                continue
            kb._reclaim_dangling_run(
                conn, task_id, statuses=("scheduled",), now=stamp,
                note="invariant recovery on timed wake",
            )
            landing = kb._landing_status_after_parents(conn, task_id)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, scheduled_until = NULL, current_run_id = NULL "
                "WHERE id = ? AND status = 'scheduled'",
                (landing, task_id),
            )
            if cur.rowcount != 1:
                continue
            late_by = max(0, stamp - epoch)
            payload = {
                "scheduled_until": epoch,
                "scheduled_until_local": format_local(epoch),
                "woken_at": stamp,
                "woken_at_local": format_local(stamp),
                "late_by_seconds": late_by,
                "status": landing,
            }
            kb._append_event(conn, task_id, "woken_by_timer", payload)
            notes.append((task_id, (
                f"WAKE (timer): срок {format_local(epoch)} наступил, карточка → {landing}"
                + (f", опоздание {late_by} с (тик пришёл позже срока)" if late_by > 60 else "")
            )))
            woken.append(task_id)
    # Comments carry the notification side effect (event row + subscription fan-out),
    # so they are written after the status txn commits, never inside it.
    for task_id, body in notes:
        try:
            kb.add_comment(conn, task_id, "dispatcher", body)
        except Exception:  # noqa: BLE001 - notification only; the wake already committed
            kb._log.exception("timed wake: comment failed for %s", task_id)
    return woken


def pending_wakeups(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """``[{id, title, scheduled_until, scheduled_until_local, overdue_seconds}]`` for the board summary.

    Deadline-less parked cards are included when their own notes carry a legacy
    marker (that is a real deadline, just stored the old way), so the summary
    shows everything the tick will wake.
    """
    stamp = int(time.time())
    rows = conn.execute(
        "SELECT id, title, scheduled_until FROM tasks WHERE status = 'scheduled' "
        "ORDER BY scheduled_until ASC LIMIT ?", (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        epoch = _kb()._to_epoch(row["scheduled_until"])
        if epoch is None:
            epoch = legacy_deadline_from_comments(conn, row["id"], now=stamp)
        if epoch is None:
            continue
        out.append({
            "id": row["id"],
            "title": row["title"],
            "scheduled_until": int(epoch),
            "scheduled_until_local": format_local(int(epoch)),
            "overdue_seconds": max(0, stamp - int(epoch)),
        })
    out.sort(key=lambda c: c["scheduled_until"])
    return out


def describe_schedule(counts: dict[str, Any], *, limit: int = 5) -> str:
    """One line for ``hermes kanban stats``: what is parked and when it wakes."""
    if not counts.get("scheduled_total"):
        return ""
    parts = [f"scheduled={counts['scheduled_total']}"]
    if counts.get("scheduled_no_deadline"):
        parts.append(f"no-deadline={counts['scheduled_no_deadline']}")
    if counts.get("scheduled_overdue"):
        parts.append(f"overdue={counts['scheduled_overdue']}")
    upcoming: Iterable[dict[str, Any]] = counts.get("next_wakeups") or []
    listed = list(upcoming)[:limit]
    head = ", ".join(f"{c['id']}@{c['scheduled_until_local']}" for c in listed)
    line = "Scheduled: " + ", ".join(parts)
    if head:
        line += f" — next: {head}"
    return line


__all__ = [
    "clear_scheduled_until",
    "describe_schedule",
    "due_tasks",
    "format_local",
    "legacy_deadline_from_comments",
    "parse_until",
    "pending_wakeups",
    "profile_tz",
    "resolve_deadline",
    "set_scheduled_until",
    "wake_due_tasks",
]
