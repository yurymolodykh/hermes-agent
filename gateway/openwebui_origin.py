"""Resolve a legacy Open WebUI origin against its server-owned chat registry.

This read-only seam is for a trusted local delivery worker, never a public API
endpoint or an authorization decision based only on client-supplied headers.
It does not write Open WebUI chats or imply that an outbox entry is visible.
"""

from __future__ import annotations

import re
import json
import sqlite3
from pathlib import Path
from typing import Mapping


_CHAT_ID = re.compile(r"owui-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")


def resolve_origin(db_path: str | Path, profile: str, source: Mapping[str, object]) -> dict | None:
    """Resolve exact OWUI chat ownership; fail closed for missing DB/owner."""
    if not isinstance(source, Mapping) or source.get("platform") != "api_server":
        return None
    if not isinstance(profile, str) or not profile.strip():
        return None
    chat_id = source.get("chat_id")
    match = _CHAT_ID.fullmatch(chat_id) if isinstance(chat_id, str) else None
    if not match or source.get("thread_id") is not None:
        return None
    session_id = source.get("session_id")
    if session_id is not None and session_id != chat_id:
        return None
    try:
        # URI mode=ro prevents a typo from creating an empty registry; immutable
        # mode would miss WAL changes and must not be used on a running WebUI.
        db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        with db:
            row = db.execute("SELECT user_id FROM chat WHERE id = ?", (match.group(1),)).fetchone()
        db.close()
    except (OSError, sqlite3.Error, ValueError):
        return None
    owner = row[0] if row else None
    if not isinstance(owner, str) or not owner.strip():
        return None
    for owner_claim in (source.get("user_id"), source.get("owner_id")):
        if owner_claim is not None and owner_claim != owner:
            return None
    return {"platform": "api_server", "profile": profile, "chat_id": chat_id,
            "session_id": chat_id, "owner_id": owner, "thread_id": None}


def acknowledge_visible(outbox_path: str | Path, db_path: str | Path,
                        origin: Mapping[str, object], event_id: str) -> bool:
    """ACK only after an OWUI persisted message on the active chat branch is read back.

    The caller must be a trusted worker; this function does not expose a public
    endpoint or accept an HTTP 200/self-post as proof of UI availability.
    """
    from gateway.origin_outbox import ack, get_pending

    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{64}", event_id):
        return False
    if not isinstance(origin, Mapping):
        return False
    profile = origin.get("profile")
    if not isinstance(profile, str):
        return False
    resolved = resolve_origin(db_path, profile, origin)
    if not resolved or resolved != origin:
        return False
    try:
        pending = get_pending(outbox_path, event_id, resolved)
        if pending is None:
            return False
        raw_id = _CHAT_ID.fullmatch(resolved["chat_id"]).group(1)
        db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT chat FROM chat WHERE id = ? AND user_id = ?",
                             (raw_id, resolved["owner_id"])).fetchone()
        finally:
            db.close()
        if not row:
            return False
        history = json.loads(row[0]).get("history", {})
        messages = history.get("messages")
        if not isinstance(messages, dict):
            return False
        current = history.get("currentId")
        visited = set()
        while isinstance(current, str) and current in messages and current not in visited:
            visited.add(current)
            message = messages[current]
            if not isinstance(message, dict):
                return False
            if current == event_id:
                if (message.get("id") != event_id or message.get("role") != "assistant"
                        or message.get("done") is not True
                        or message.get("content") != pending["content"]):
                    return False
                return ack(outbox_path, event_id, resolved, f"openwebui:{event_id}")
            current = message.get("parentId")
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return False
    return False
