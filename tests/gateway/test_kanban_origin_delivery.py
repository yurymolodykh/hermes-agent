"""Kanban terminal outbox binding across root/child/review."""
import json
import sqlite3
from gateway.kanban_origin_delivery import stage_terminal
from gateway.origin_delivery_worker import drain_openwebui
from gateway.origin_outbox import status

RAW = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"

def test_root_child_review_event_ack_cursor_gate(tmp_path):
    registry = tmp_path / "webui.db"
    outbox = tmp_path / "outbox.db"
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (RAW, "owner-A", json.dumps({"history": {
            "messages": {"u": {"id": "u", "role": "user", "parentId": None}}, "currentId": "u"}})))
    sub = {"platform": "api_server", "chat_id": "owui-" + RAW, "thread_id": "", "notifier_profile": "01-orchestrator", "user_id": "owner-A"}
    for task_id, kind, event_num in (("root", "completed", 100), ("child", "review_requested", 101), ("child", "completed", 102)):
        msg = f"Kanban {task_id} {kind}"
        state, event = stage_terminal(outbox, registry, sub, "01-orchestrator", "board-A", event_num, msg)
        assert state == "pending"
        assert status(outbox, event) == "pending"
        assert drain_openwebui(outbox, registry)["acknowledged"] == 1
        state2, event2 = stage_terminal(outbox, registry, sub, "01-orchestrator", "board-A", event_num, msg)
        assert (state2, event2) == ("acknowledged", event)
    with sqlite3.connect(registry) as db:
        history = json.loads(db.execute("SELECT chat FROM chat WHERE id=?", (RAW,)).fetchone()[0])["history"]
    assert len(history["messages"]) == 4

def test_untrusted_binding_never_enqueues(tmp_path):
    sub = {"platform": "api_server", "chat_id": "owui-" + RAW, "thread_id": "", "notifier_profile": "01-orchestrator", "user_id": "owner-A"}
    assert stage_terminal(tmp_path / "outbox", tmp_path / "missing.db", sub, "01-orchestrator", "board", 1, "message")[0] == "unbound"
    assert not (tmp_path / "outbox").exists()
