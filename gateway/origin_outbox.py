"""Profile-local durable delivery outbox; an enqueue is never a user-visible ACK.

Only a server-authorized caller may supply an origin to pending()/ack(). This
module deliberately does not infer a destination from a home chat or worker
session and cannot authenticate a browser by its claimed chat/session headers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
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


def _open(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS origin_delivery (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL, destination TEXT NOT NULL,
        content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        receipt TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ack_at TEXT, UNIQUE(source_id, destination)
    )""")
    return db


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
        row = db.execute("SELECT content FROM origin_delivery WHERE id = ?", (event_id,)).fetchone()
        if row is None or row[0] != content:
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


def status(path: str | Path, event_id: str) -> str | None:
    with _open(path) as db:
        row = db.execute("SELECT status FROM origin_delivery WHERE id = ?", (event_id,)).fetchone()
    return row[0] if row else None
