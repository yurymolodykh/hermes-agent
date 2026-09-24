"""Regression for the archived api_server cron loss: no adapter.send or fake ACK."""
import sqlite3
from cron import scheduler
from gateway import origin_outbox


def test_archived_api_cron_persists_pending_without_claiming_delivery(tmp_path, monkeypatch):
    origin = {
        "platform": "api_server", "profile": "01-orchestrator",
        "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "session_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "owner_id": "server-authorized-owner", "thread_id": None,
    }
    job = {"id": "3f0173195624", "name": "morning-backup-status-check", "deliver": "origin", "origin": origin}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = tmp_path / "webui.db"
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?)",
                   ("98d69f26-8263-4e1c-84a5-6d9fcdbf593d", "server-authorized-owner"))
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(registry))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "01-orchestrator")
    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda job: [{
        "platform": "api_server", "chat_id": origin["chat_id"], "thread_id": None,
    }])
    sends = []
    import tools.send_message_tool as sender
    monkeypatch.setattr(sender, "_send_to_platform", lambda *args, **kw: sends.append(kw))
    content = "СТАТУС БЭКАПОВ HERMES (проверка утром)"
    error = scheduler._deliver_result(job, content, source_id="cron:3f0173195624:2026-09-24_08-30-58")
    assert error and "pending client ACK" in error
    assert sends == []
    rows = origin_outbox.pending(tmp_path / "delivery-outbox.sqlite", origin)
    assert len(rows) == 1
    assert content in rows[0]["content"]


def test_archived_cron_legacy_origin_requires_operator_registry_to_rebind(tmp_path, monkeypatch):
    raw = {"platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
           "chat_name": None, "thread_id": None, "user_id": None, "scope_id": None}
    job = {"id": "3f0173195624", "deliver": "origin", "origin": raw}
    registry = tmp_path / "webui.db"
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?)",
                   ("98d69f26-8263-4e1c-84a5-6d9fcdbf593d", "owner-A"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(registry))
    monkeypatch.setenv("HERMES_OPENWEBUI_LEGACY_CRON_ALLOWLIST", "3f0173195624:owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "01-orchestrator")
    error = scheduler._deliver_result(job, "СТАТУС БЭКАПОВ HERMES", source_id="2026-09-24_08-30-58")
    assert error and "pending client ACK" in error
    resolved = {"platform": "api_server", "profile": "01-orchestrator", "chat_id": raw["chat_id"],
                "session_id": raw["chat_id"], "owner_id": "owner-A", "thread_id": None}
    assert len(origin_outbox.pending(tmp_path / "delivery-outbox.sqlite", resolved)) == 1
    assert origin_outbox.pending(tmp_path / "delivery-outbox.sqlite", {**resolved, "owner_id": "owner-B"}) == []


def test_api_cron_missing_owner_does_not_use_home_or_send(tmp_path, monkeypatch):
    origin = {"platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d"}
    job = {"id": "3f0173195624", "deliver": "origin", "origin": origin}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    error = scheduler._deliver_result(job, "backup output", source_id="run-1")
    assert error and "origin" in error.lower()
    assert not (tmp_path / "delivery-outbox.sqlite").exists()


def test_legacy_cron_existing_foreign_chat_without_operator_allowlist_fails_closed(tmp_path, monkeypatch):
    registry = tmp_path / "webui.db"
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?)",
                   ("98d69f26-8263-4e1c-84a5-6d9fcdbf593d", "other-owner"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(registry))
    monkeypatch.delenv("HERMES_OPENWEBUI_LEGACY_CRON_ALLOWLIST", raising=False)
    job = {"id": "3f0173195624", "deliver": "origin", "origin": {
        "platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d"}}
    error = scheduler._deliver_result(job, "secret", source_id="run-1")
    assert error and "binding" in error
    assert not (tmp_path / "delivery-outbox.sqlite").exists()


def test_origin_without_binding_never_resolves_home(monkeypatch):
    monkeypatch.setattr(scheduler, "_iter_home_target_platforms", lambda: ["telegram"])
    monkeypatch.setattr(scheduler, "_get_home_target_chat_id", lambda platform: "home-chat")
    job = {"id": "3f0173195624", "deliver": "origin"}
    assert scheduler._resolve_single_delivery_target(job, "origin") is None
    # A successfully executed job without its destination is NOT delivered.
    error = scheduler._deliver_result(job, "backup output", source_id="run-1")
    assert error and "origin" in error.lower()
