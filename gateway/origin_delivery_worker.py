"""Opt-in local worker for exact-origin Open WebUI persisted deliveries.

No HTTP self-post, no adapter.send and no fallback. The gateway polls this
worker only when a privileged local WebUI registry path is explicitly configured.
"""
from __future__ import annotations

import time
from pathlib import Path

from gateway.openwebui_delivery import persist_pending
from gateway.origin_outbox import due, fail_attempt, status


def drain_openwebui(outbox_path: str | Path, db_path: str | Path, *,
                    max_attempts: int = 5, base_delay: int = 30,
                    now: int | None = None) -> dict[str, int]:
    if max_attempts < 1 or base_delay < 0:
        raise ValueError("invalid bounded retry policy")
    current = int(time.time()) if now is None else now
    results = {"acknowledged": 0, "retrying": 0, "failed": 0}
    for item in due(outbox_path, now=current):
        origin = item["origin"]
        event_id = item["id"]
        success = origin.get("platform") == "api_server" and persist_pending(
            outbox_path, db_path, origin, event_id)
        if success:
            results["acknowledged"] += 1
        elif status(outbox_path, event_id) == "pending":
            fail_attempt(outbox_path, event_id, now=current, max_attempts=max_attempts,
                         base_delay=base_delay, error="exact owner/chat write or readback failed")
            results["failed" if status(outbox_path, event_id) == "failed" else "retrying"] += 1
    return results
