"""Opt-in server-side Open WebUI chat writer for an already authorized outbox item.

Requires local privileged access to the server's SQLite chat registry. It is NOT
an HTTP endpoint, a client-claimed identity, a WebSocket notification, or proof
that an open browser has refreshed. The DB path is supplied by an operator.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Mapping

from gateway.openwebui_origin import _CHAT_ID, active_chain, acknowledge_visible, resolve_origin
from gateway.origin_outbox import get_pending

logger = logging.getLogger(__name__)


def persist_pending(outbox_path: str | Path, db_path: str | Path,
                    origin: Mapping[str, object], event_id: str) -> bool:
    """Write exactly once to the active branch and ACK only after exact readback.

    Crash after the write but before ACK is recovered via the existing id.
    Malformed history, mismatched owner or divergent id fail closed. Concurrent
    Open WebUI changes are serialized by SQLite BEGIN IMMEDIATE.
    """
    if not isinstance(origin, Mapping):
        return False
    profile = origin.get("profile")
    if not isinstance(profile, str) or not isinstance(event_id, str):
        return False
    resolved = resolve_origin(db_path, profile, origin)
    if not resolved or resolved != origin:
        return False
    pending = get_pending(outbox_path, event_id, resolved)
    if pending is None:
        return False
    match = _CHAT_ID.fullmatch(resolved["chat_id"])
    if not match:
        return False
    try:
        # mode=rw refuses to create a new/empty registry on a typo.
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=10)) as db, db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT chat FROM chat WHERE id=? AND user_id=?",
                             (match.group(1), resolved["owner_id"])).fetchone()
            if not row:
                return False
            chat = json.loads(row[0])
            if not isinstance(chat, dict):
                return False
            history = chat.get("history")
            if not isinstance(history, dict):
                return False
            messages = history.get("messages")
            current = history.get("currentId")
            if active_chain(messages, current) is None:
                return False
            if event_id not in messages:
                message = {"id": event_id, "parentId": current, "role": "assistant",
                           "content": pending["content"], "done": True}
                messages[event_id] = message
                history["currentId"] = event_id
                changed = db.execute("UPDATE chat SET chat=? WHERE id=? AND user_id=? AND chat=?",
                                     (json.dumps(chat, ensure_ascii=False), match.group(1),
                                      resolved["owner_id"], row[0])).rowcount
                if changed != 1:
                    raise sqlite3.IntegrityError("chat changed during exact-owner update")
            else:
                existing = messages[event_id]
                if (not isinstance(existing, dict) or existing.get("id") != event_id
                        or existing.get("role") != "assistant" or existing.get("done") is not True
                        or existing.get("content") != pending["content"]):
                    return False
        # This is a persisted-current-branch receipt, not proof of browser display.
        return acknowledge_visible(outbox_path, db_path, resolved, event_id)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        logger.warning("Open WebUI exact-owner persistence failed for event %s: %s", event_id, exc)
        return False
