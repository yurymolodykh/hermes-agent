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

def test_persistence_on_active_branch_preserves_sibling_branch(fixture):
    db_path, outbox, origin, event = fixture
    chat = read_chat(db_path)
    sibling = {"id": "sibling", "parentId": "user-1", "role": "assistant",
               "content": "other branch", "done": True}
    chat["history"]["messages"]["sibling"] = sibling
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps(chat),))
    assert persist_pending(outbox, db_path, origin, event)
    updated = read_chat(db_path)
    assert updated["history"]["messages"]["sibling"] == sibling
    assert updated["history"]["messages"][event]["parentId"] == "user-1"
    assert updated["history"]["currentId"] == event
    assert status(outbox, event) == "acknowledged"


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

@pytest.mark.parametrize("invalid_parent", [
    {"id": "DIFFERENT", "parentId": None, "role": "user"},
    {"id": "user-1", "role": "user"},
    {"id": "user-1", "parentId": "user-1", "role": "user"},
])
def test_malformed_active_parent_is_never_written_or_acknowledged(fixture, invalid_parent):
    """Structural corruption of the append target still fails closed (A1)."""
    db_path, outbox, origin, event = fixture
    with sqlite3.connect(db_path) as db:
        chat = read_chat(db_path)
        chat["history"]["messages"]["user-1"] = invalid_parent
        db.execute("UPDATE chat SET chat=?", (json.dumps(chat),))
    assert not persist_pending(outbox, db_path, origin, event)
    assert read_chat(db_path) == chat
    assert status(outbox, event) == "pending"


def test_trimmed_broken_ancestor_does_not_block_append(fixture):
    """A1: a parentId pointing at a missing (pruned) ancestor above the tip is
    legitimate OWUI history trimming — the terminal итог is still written and
    ACKed (previously this blocked the write → channel dropped after 12 fails).
    """
    db_path, outbox, origin, event = fixture
    with sqlite3.connect(db_path) as db:
        chat = read_chat(db_path)
        chat["history"]["messages"]["user-1"]["parentId"] = "pruned-turn"
        db.execute("UPDATE chat SET chat=?", (json.dumps(chat),))
    assert persist_pending(outbox, db_path, origin, event)
    updated = read_chat(db_path)
    assert updated["history"]["currentId"] == event
    assert updated["history"]["messages"][event]["parentId"] == "user-1"
    assert status(outbox, event) == "acknowledged"


def test_corrupt_or_divergent_history_fails_closed(fixture):
    db_path, outbox, origin, event = fixture
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps({"history": {"messages": {event: {"content": "foreign"}}, "currentId": event}}),))
    assert not persist_pending(outbox, db_path, origin, event)
    assert status(outbox, event) == "pending"


def _write_and_keep_pending(fixture, monkeypatch):
    """Land the exact-owner write but force readback to fail once (crash
    simulation) so the outbox row stays pending with the receipt in the chat —
    the exact state the D2 retry loop sees after a write/readback race."""
    import gateway.openwebui_delivery as delivery
    db_path, outbox, origin, event = fixture
    monkeypatch.setattr(delivery, "acknowledge_visible", lambda *a: False)
    assert not persist_pending(outbox, db_path, origin, event)
    assert status(outbox, event) == "pending"
    monkeypatch.undo()
    return db_path, outbox, origin, event


def test_active_branch_receipt_acks_when_owner_rebranched_after_write(fixture, monkeypatch):
    """D2 (t_8980d76e): a live owner re-branching after the write must not park
    the persisted receipt as failed.  The write lands under the old tip; the
    owner then advances currentId with a follow-up message, so the receipt is
    no longer the tip but IS still on the active branch with exact content —
    the readback must still ACK it."""
    db_path, outbox, origin, event = _write_and_keep_pending(fixture, monkeypatch)
    chat = read_chat(db_path)
    # Owner continues AFTER our receipt: a new user+assistant turn advances
    # currentId past event.
    chat["history"]["messages"]["owner-echo-user"] = {
        "id": "owner-echo-user", "parentId": event, "role": "user",
        "content": "continue", "done": True}
    chat["history"]["messages"]["owner-echo-assistant"] = {
        "id": "owner-echo-assistant", "parentId": "owner-echo-user",
        "role": "assistant", "content": "thanks", "done": True}
    chat["history"]["currentId"] = "owner-echo-assistant"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps(chat),))
    # Real readback: receipt no longer at tip but on the active branch.
    assert persist_pending(outbox, db_path, origin, event)
    assert status(outbox, event) == "acknowledged"
    updated = read_chat(db_path)
    assert updated["history"]["messages"][event]["content"] == "morning status"
    assert updated["history"]["currentId"] == "owner-echo-assistant"


def test_active_branch_off_branch_receipt_is_not_acked(fixture, monkeypatch):
    """A receipt that is NOT on the active branch (an orphaned sibling the
    owner switched away from) still fails closed — the fallback must not ACK
    a message the owner does not see on the current chain."""
    db_path, outbox, origin, event = _write_and_keep_pending(fixture, monkeypatch)
    chat = read_chat(db_path)
    receipt_parent = chat["history"]["messages"][event]["parentId"]
    # Owner switches to a SIBLING branch that does not contain our receipt.
    chat["history"]["messages"]["branch-alt-user"] = {
        "id": "branch-alt-user", "parentId": receipt_parent, "role": "user",
        "content": "alt", "done": True}
    chat["history"]["messages"]["branch-alt-assistant"] = {
        "id": "branch-alt-assistant", "parentId": "branch-alt-user",
        "role": "assistant", "content": "alt", "done": True}
    chat["history"]["currentId"] = "branch-alt-assistant"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps(chat),))
    assert not persist_pending(outbox, db_path, origin, event)
    assert status(outbox, event) == "pending"


def test_active_branch_receipt_with_mismatched_content_fails_closed(fixture, monkeypatch):
    """Even on the active branch, a receipt whose content does not match the
    staged content must never ACK (exact-content integrity preserved)."""
    db_path, outbox, origin, event = _write_and_keep_pending(fixture, monkeypatch)
    chat = read_chat(db_path)
    # Owner continues, but the persisted receipt content diverges.
    chat["history"]["messages"][event]["content"] = "TAMPERED"
    chat["history"]["messages"]["owner-echo-user"] = {
        "id": "owner-echo-user", "parentId": event, "role": "user",
        "content": "continue", "done": True}
    chat["history"]["messages"]["owner-echo-assistant"] = {
        "id": "owner-echo-assistant", "parentId": "owner-echo-user",
        "role": "assistant", "content": "thanks", "done": True}
    chat["history"]["currentId"] = "owner-echo-assistant"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps(chat),))
    assert not persist_pending(outbox, db_path, origin, event)
    assert status(outbox, event) == "pending"


def test_active_branch_cycle_in_chain_fails_closed(fixture, monkeypatch):
    """A cycle in the active chain (structural corruption) must terminate the
    bounded walk without ACKing — no unbounded loop, fail closed."""
    db_path, outbox, origin, event = _write_and_keep_pending(fixture, monkeypatch)
    chat = read_chat(db_path)
    # Create a cycle: a -> b -> a (event off-cycle) as the current state.
    chat["history"]["messages"]["a"] = {"id": "a", "parentId": "b", "role": "user", "content": "a", "done": True}
    chat["history"]["messages"]["b"] = {"id": "b", "parentId": "a", "role": "user", "content": "b", "done": True}
    chat["history"]["currentId"] = "a"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps(chat),))
    assert not persist_pending(outbox, db_path, origin, event)
    assert status(outbox, event) == "pending"
