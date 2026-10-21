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


def active_chain(messages: dict, current: str) -> list[str] | None:
    """Validate the complete active parent chain, refusing broken/cyclic histories."""
    if not isinstance(messages, dict) or not isinstance(current, str):
        return None
    chain: list[str] = []
    visited: set[str] = set()
    while current not in visited:
        message = messages.get(current)
        if not isinstance(message, dict) or message.get("id") != current:
            return None
        visited.add(current)
        chain.append(current)
        if "parentId" not in message:
            return None
        parent = message["parentId"]
        if parent is None:
            return chain
        if not isinstance(parent, str) or parent not in messages:
            return None
        current = parent
    return None


def append_target_ok(messages: dict, current: str) -> bool:
    """True when a new assistant message may be appended under ``current``.

    The full ancestor chain is NOT required to be intact: Open WebUI prunes /
    trims old turns as chat history grows (a broken chain above the current
    tip is legitimate and must not block the terminal итог from landing).
    We only require that the append target ``current`` itself exists as a
    well-formed message so the new message has a real parent:

    - ``current`` must be a key of ``messages`` whose dict ``id`` matches it
      and whose ``role`` is user/assistant/system;
    - the ``parentId`` key must be present (OWUI messages always carry it);
    - a ``parentId`` that is missing from ``messages`` (broken/trimmed
      ancestor) is ALLOWED — that is exactly the A1 scenario;
    - a self-loop ``parentId == current`` is structural corruption, not
      history trimming, and fails closed.

    The exact appended message is validated separately on readback.
    """
    if not isinstance(messages, dict) or not isinstance(current, str):
        return False
    message = messages.get(current)
    if (not isinstance(message, dict) or message.get("id") != current):
        return False
    if "parentId" not in message:
        return False
    parent = message["parentId"]
    if parent is None:
        return True
    if parent == current:
        # Self-loop is never produced by OWUI trimming — reject it.
        return False
    return isinstance(parent, str)


def current_tip_ok(messages: dict, current: str, event_id: str,
                   expected_content: str) -> bool:
    """Validate an appended delivery is the CURRENT tip with exact content.

    This is the A1 readback gate: the event must sit at ``currentId`` and be
    an exact assistant receipt for ``expected_content``.  The ancestor chain
    ABOVE the receipt's parent may be pruned/broken (legitimate OWUI history
    trimming) — the integrity property that matters is that THIS receipt is
    the current tip, owned by this chat, with the exact content we staged.
    The receipt's PARENT must still be a structurally valid append target
    (a self-loop / wrong-id / missing-parentId parent is corruption, not
    trimming, and fails closed), while that parent's own ancestor may be
    missing.  Anything that breaks the receipt at the tip (wrong id/role/
    done/content, or the event is no longer the tip because a human
    re-branched) also fails closed.
    """
    if not isinstance(messages, dict) or not isinstance(current, str):
        return False
    if current != event_id:
        return False
    message = messages.get(event_id)
    if (not isinstance(message, dict) or message.get("id") != event_id
            or message.get("role") != "assistant"
            or message.get("done") is not True
            or message.get("content") != expected_content):
        return False
    parent = message.get("parentId")
    if parent is None:
        return True
    # The receipt's parent must be a valid append target (allows THAT
    # parent's own ancestor chain to be trimmed/broken).
    if not append_target_ok(messages, parent):
        return False
    return True


def resolve_origin(db_path: str | Path, profile: str, source: Mapping[str, object]) -> dict | None:
    """Resolve exact OWUI chat ownership; fail closed for missing DB/owner."""
    if not isinstance(source, Mapping) or source.get("platform") != "api_server":
        return None
    if not isinstance(profile, str) or not profile.strip():
        return None
    if source.get("profile") is not None and source.get("profile") != profile:
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
        # A1 readback gate: the receipt must be the CURRENT tip with exact
        # staged content.  The ancestor chain above it may be pruned by OWUI
        # history trimming — that is legitimate and no longer blocks ACK (it
        # previously forced the channel into 12-failures → drop).
        if not current_tip_ok(messages, current, event_id, pending["content"]):
            return False
        return ack(outbox_path, event_id, resolved, f"openwebui:{event_id}")
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return False
    return False
