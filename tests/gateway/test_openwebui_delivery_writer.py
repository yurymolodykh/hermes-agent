"""Trusted server-side Open WebUI persistence; never infer UI visibility from a row."""
import json
import sqlite3

import pytest

from gateway.openwebui_origin import resolve_origin
from gateway.origin_outbox import enqueue, status
from gateway.openwebui_delivery import persist_pending

CHAT = "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d"
RAW = CHAT.removeprefix("owui-")

@pytest.fixture
def fixture(tmp_path):
    db_path, outbox = tmp_path / "webui.db", tmp_path / "outbox.db"
    chat = {"title": "existing", "history": {"messages": {
        "user-1": {"id": "user-1", "role": "user", "content": "hello", "parentId": None}
    }, "currentId": "user-1"}}
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (RAW, "owner-A", json.dumps(chat)))
    origin = resolve_origin(db_path, "01-orchestrator", {"platform": "api_server", "chat_id": CHAT})
    event = enqueue(outbox, "cron:3f0173195624:2026-09-24_08-30-58", origin, "morning status")
    return db_path, outbox, origin, event

def read_chat(db_path):
    with sqlite3.connect(db_path) as db:
        return json.loads(db.execute("SELECT chat FROM chat WHERE id=?", (RAW,)).fetchone()[0])

def test_exact_owner_persistence_readback_ack_duplicate_and_restart(fixture):
    db_path, outbox, origin, event = fixture
    assert persist_pending(outbox, db_path, origin, event)
    chat = read_chat(db_path)
    assert chat["title"] == "existing"
    assert chat["history"]["currentId"] == event
    assert chat["history"]["messages"][event]["parentId"] == "user-1"
    assert status(outbox, event) == "acknowledged"
    assert not persist_pending(outbox, db_path, origin, event)  # restart/replay: no second message
    assert read_chat(db_path) == chat

def test_wrong_owner_and_chat_never_write(fixture):
    db_path, outbox, origin, event = fixture
    before = read_chat(db_path)
    assert not persist_pending(outbox, db_path, {**origin, "owner_id": "owner-B"}, event)
    assert not persist_pending(outbox, db_path, {**origin, "chat_id": "owui-00000000-0000-4000-8000-000000000000"}, event)
    assert read_chat(db_path) == before
    assert status(outbox, event) == "pending"

def test_crash_after_write_before_ack_is_recovered_without_duplicate(fixture, monkeypatch):
    db_path, outbox, origin, event = fixture
    import gateway.openwebui_delivery as delivery
    monkeypatch.setattr(delivery, "acknowledge_visible", lambda *a: False)
    assert not persist_pending(outbox, db_path, origin, event)
    written = read_chat(db_path)
    assert status(outbox, event) == "pending"
    monkeypatch.undo()
    assert persist_pending(outbox, db_path, origin, event)
    assert read_chat(db_path) == written

def test_corrupt_or_divergent_history_fails_closed(fixture):
    db_path, outbox, origin, event = fixture
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps({"history": {"messages": {event: {"content": "foreign"}}, "currentId": event}}),))
    assert not persist_pending(outbox, db_path, origin, event)
    assert status(outbox, event) == "pending"
