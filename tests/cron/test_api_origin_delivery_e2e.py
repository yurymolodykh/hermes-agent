"""Cron producer -> gateway worker -> persisted chat receipt."""
import json
import sqlite3

from cron import scheduler
from gateway.origin_delivery_worker import drain_openwebui
from gateway.origin_outbox import counts

RAW = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"


def test_cron_run_replays_to_exact_chat_once(tmp_path, monkeypatch):
    db_path = tmp_path / "webui.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, chat JSON)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (RAW, "owner-A", json.dumps({
            "history": {"messages": {"u": {"id": "u", "parentId": None, "role": "user"}},
                        "currentId": "u"}})))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_OPENWEBUI_LEGACY_CRON_ALLOWLIST", "job-1:owui-" + RAW)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "01-orchestrator")
    job = {"id": "job-1", "deliver": "origin", "origin": {"platform": "api_server", "chat_id": "owui-" + RAW}}
    error = scheduler._deliver_result(job, "real backup status", source_id="run-1")
    assert error and "pending client ACK" in error
    outbox = tmp_path / "delivery-outbox.sqlite"
    assert counts(outbox) == {"pending": 1}
    assert drain_openwebui(outbox, db_path) == {"acknowledged": 1, "retrying": 0, "failed": 0}
    assert counts(outbox) == {"acknowledged": 1}
    assert drain_openwebui(outbox, db_path) == {"acknowledged": 0, "retrying": 0, "failed": 0}
    assert scheduler._deliver_result(job, "real backup status", source_id="run-1") is None
    assert counts(outbox) == {"acknowledged": 1}
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT chat FROM chat WHERE id=? AND user_id=?", (RAW, "owner-A")).fetchone()
    history = json.loads(row[0])["history"]
    assert len(history["messages"]) == 2
    assert "real backup status" in history["messages"][history["currentId"]]["content"]
