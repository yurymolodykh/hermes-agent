"""Exact server-owned binding and durable outbox for Kanban terminal events."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from gateway.openwebui_origin import resolve_origin
from gateway.origin_outbox import enqueue, status


def stage_terminal(outbox_path: str | Path, registry_path: str | Path,
                   sub: Mapping[str, object], profile: str, board: str,
                   event_id: int, message: str) -> tuple[str, str | None]:
    """Return (pending|acknowledged|failed|unbound, delivery event id).

    Only a trusted notifier may invoke this; the subscription is a persisted
    server-side record. The registry independently verifies ownership, while
    a client-supplied chat/owner pair alone never grants outbox access.
    """
    if (sub.get("platform") != "api_server" or not profile
            or sub.get("notifier_profile") not in (profile, None, "")
            or sub.get("thread_id") not in (None, "")
            or not isinstance(event_id, int) or event_id < 1 or not board):
        return "unbound", None
    if not isinstance(sub.get("user_id"), str) or not sub["user_id"].strip():
        return "unbound", None
    origin = resolve_origin(registry_path, profile, {
        "platform": "api_server", "chat_id": sub.get("chat_id"), "thread_id": None,
        "user_id": sub["user_id"],
    })
    if origin is None:
        return "unbound", None
    source_id = f"kanban:{board}:event:{event_id}"
    try:
        delivery_id = enqueue(outbox_path, source_id, origin, message)
    except ValueError:
        return "failed", None
    return status(outbox_path, delivery_id) or "failed", delivery_id
