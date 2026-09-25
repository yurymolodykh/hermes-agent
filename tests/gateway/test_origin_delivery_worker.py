"""Bounded, observable retry on server-side OWUI outbox entries."""
import json
import sqlite3

from gateway.origin_outbox import enqueue, pending, status
from gateway.origin_delivery_worker import drain_openwebui
from gateway.openwebui_origin import resolve_origin

RAW = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"

def setup(tmp_path):
    registry, outbox = tmp_path / "webui.db", tmp_path / "outbox.db"
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (RAW, "owner-A", json.dumps({"history": {
            "messages": {"u": {"id": "u", "role": "user", "parentId": None}}, "currentId": "u"}})))
    origin = resolve_origin(registry, "01-orchestrator", {"platform": "api_server", "chat_id": "owui-" + RAW})
    event = enqueue(outbox, "cron:3f0173195624:run", origin, "status")
    return registry, outbox, origin, event

def test_drain_exact_chat_and_receipt_after_restart(tmp_path):
    registry, outbox, origin, event = setup(tmp_path)
    assert drain_openwebui(outbox, registry) == {"acknowledged": 1, "retrying": 0, "failed": 0}
    assert status(outbox, event) == "acknowledged"
    assert pending(outbox, origin) == []
    assert drain_openwebui(outbox, registry) == {"acknowledged": 0, "retrying": 0, "failed": 0}

def test_bad_owner_retries_are_bounded_and_visible(tmp_path):
    registry, outbox, origin, event = setup(tmp_path)
    with sqlite3.connect(registry) as db:
        db.execute("UPDATE chat SET user_id='owner-B'")
    assert drain_openwebui(outbox, registry, max_attempts=2, base_delay=0)["retrying"] == 1
    assert status(outbox, event) == "pending"
    assert drain_openwebui(outbox, registry, max_attempts=2, base_delay=0)["failed"] == 1
    assert status(outbox, event) == "failed"
    assert drain_openwebui(outbox, registry, max_attempts=2, base_delay=0) == {"acknowledged": 0, "retrying": 0, "failed": 0}
    with sqlite3.connect(outbox) as db:
        assert db.execute("SELECT attempts, last_error FROM origin_delivery WHERE id=?", (event,)).fetchone() == (2, "exact owner/chat write or readback failed")
