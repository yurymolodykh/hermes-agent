"""Profile-local durable delivery outbox; an enqueue is never a user-visible ACK.

Only a server-authorized caller may supply an origin to pending()/ack(). This
module deliberately does not infer a destination from a home chat or worker
session and cannot authenticate a browser by its claimed chat/session headers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping


_BINDING = ("platform", "profile", "chat_id", "session_id", "owner_id")


def _destination(origin: Mapping[str, object]) -> str:
    if not isinstance(origin, Mapping):
        raise ValueError("delivery origin requires a server-authorized binding")
    binding = {key: origin.get(key) for key in _BINDING}
    if any(not isinstance(v, str) or not v.strip() for v in binding.values()):
        raise ValueError("delivery origin requires platform/profile/chat/session/owner")
    if binding["platform"] not in ("api_server", "pwa", "telegram"):
        raise ValueError("unsupported delivery platform")
    thread = origin.get("thread_id")
    if thread is not None and not isinstance(thread, str):
        raise ValueError("invalid origin thread")
    binding["thread_id"] = thread
    return json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _open(path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS origin_delivery (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, destination TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                receipt TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ack_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                UNIQUE(source_id, destination)
            )""")
            # Older candidate DBs already exist: retain pending entries during upgrade.
            columns = {row[1] for row in db.execute("PRAGMA table_info(origin_delivery)")}
            for name, ddl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                              ("next_attempt_at", "INTEGER NOT NULL DEFAULT 0"),
                              ("last_error", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE origin_delivery ADD COLUMN {name} {ddl}")
            yield db
    finally:
        db.close()


def enqueue(path: str | Path, source_id: str, origin: Mapping[str, object], content: str) -> str:
    """Commit exactly one message per source event and exact destination."""
    destination = _destination(origin)  # fail before opening/writing any file
    if not source_id or not isinstance(content, str) or not content or len(content) > 1_000_000:
        raise ValueError("invalid source event or delivery content")
    event_id = hashlib.sha256(f"{source_id}\0{destination}".encode()).hexdigest()
    with _open(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""INSERT OR IGNORE INTO origin_delivery(id, source_id, destination, content)
                      VALUES(?, ?, ?, ?)""", (event_id, source_id, destination, content))
        row = db.execute(
            "SELECT content, status FROM origin_delivery WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise ValueError("source event enqueue lost its row")
        existing_content, existing_status = row[0], row[1]
        # An acknowledged row is terminal: the source event was already
        # delivered and read back, so re-staging it (e.g. the notifier
        # recomputing a terminal summary that grew since delivery) must be
        # an idempotent no-op returning the existing event id — NOT a
        # hard conflict that would make the notifier rewind its cursor and
        # re-claim the same event forever ("delivery failed; cursor
        # retained" spam, incident 2026-10-08/09). Only a still-pending
        # (undelivered) row with changed content is a genuine conflict.
        if existing_content != content and existing_status != "acknowledged":
            raise ValueError("source event already bound to different content")
    return event_id


def pending(path: str | Path, origin: Mapping[str, object]) -> list[dict[str, str]]:
    """Return entries only after the caller has independently authenticated origin."""
    destination = _destination(origin)
    with _open(path) as db:
        rows = db.execute("""SELECT id, source_id, content FROM origin_delivery
                             WHERE destination = ? AND status = 'pending'
                             ORDER BY created_at, id LIMIT 100""", (destination,)).fetchall()
    return [dict(zip(("id", "source_id", "content"), row)) for row in rows]


def get_pending(path: str | Path, event_id: str, origin: Mapping[str, object]) -> dict[str, str] | None:
    """Read a single pending item for this exact authorized destination."""
    destination = _destination(origin)
    with _open(path) as db:
        row = db.execute("""SELECT id, source_id, content FROM origin_delivery
                            WHERE id = ? AND destination = ? AND status = 'pending'""",
                         (event_id, destination)).fetchone()
    return dict(zip(("id", "source_id", "content"), row)) if row else None


def ack(path: str | Path, event_id: str, origin: Mapping[str, object], receipt: str) -> bool:
    """Commit client receipt against the exact authorized destination."""
    destination = _destination(origin)
    if not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 256:
        return False
    with _open(path) as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("""UPDATE origin_delivery
            SET status = 'acknowledged', receipt = ?, ack_at = CURRENT_TIMESTAMP
            WHERE id = ? AND destination = ? AND status = 'pending'""",
            (receipt, event_id, destination)).rowcount
    return bool(changed)


def due(path: str | Path, *, now: int, limit: int = 100) -> list[dict]:
    """List persisted retryable entries, including their exact binding."""
    with _open(path) as db:
        rows = db.execute("""SELECT id, destination, attempts FROM origin_delivery
                             WHERE status = 'pending' AND next_attempt_at <= ?
                             ORDER BY created_at, id LIMIT ?""", (now, min(max(limit, 1), 100))).fetchall()
    return [{"id": row[0], "origin": json.loads(row[1]), "attempts": row[2]} for row in rows]


def fail_attempt(path: str | Path, event_id: str, *, now: int, max_attempts: int,
                 base_delay: int, error: str) -> None:
    """Bound retries and keep exhausted items inspectable rather than deleting."""
    with _open(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT attempts FROM origin_delivery WHERE id=? AND status='pending'",
                         (event_id,)).fetchone()
        if row is None:
            return
        count = row[0] + 1
        db.execute("""UPDATE origin_delivery SET attempts=?, status=?, next_attempt_at=?, last_error=?
                      WHERE id=? AND status='pending'""",
                   (count, "failed" if count >= max_attempts else "pending",
                    now + min(base_delay * (2 ** min(count - 1, 10)), 3600), error[:300], event_id))


def counts(path: str | Path) -> dict[str, int]:
    """Operator-facing delivery status; execution success is a separate field."""
    with _open(path) as db:
        rows = db.execute("SELECT status, count(*) FROM origin_delivery GROUP BY status").fetchall()
    return dict(rows)


def status(path: str | Path, event_id: str) -> str | None:
    with _open(path) as db:
        row = db.execute("SELECT status FROM origin_delivery WHERE id = ?", (event_id,)).fetchone()
    return row[0] if row else None
