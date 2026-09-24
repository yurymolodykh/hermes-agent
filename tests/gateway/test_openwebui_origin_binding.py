"""Open WebUI origin proof against a server-owned chat registry, not client headers."""
import json
import sqlite3

import pytest

from gateway.openwebui_origin import resolve_origin, acknowledge_visible
from gateway.origin_outbox import enqueue, pending, status


@pytest.fixture
def chat_db(tmp_path):
    path = tmp_path / "webui.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, chat JSON)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (
            "98d69f26-8263-4e1c-84a5-6d9fcdbf593d", "owner-A", json.dumps({"history": {}}),
        ))
    return path


def test_legacy_cron_origin_is_rebound_only_to_exact_server_owned_chat(chat_db):
    origin = resolve_origin(chat_db, "01-orchestrator", {
        "platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "user_id": None, "thread_id": None,
    })
    assert origin == {
        "platform": "api_server", "profile": "01-orchestrator",
        "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "session_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "owner_id": "owner-A", "thread_id": None,
    }


@pytest.mark.parametrize("chat_id", [
    "owui-00000000-0000-4000-8000-000000000000",
    "98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
    "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d/../other",
])
def test_missing_or_malformed_chat_never_falls_back(chat_db, chat_id):
    assert resolve_origin(chat_db, "01-orchestrator", {"platform": "api_server", "chat_id": chat_id}) is None


def test_visible_receipt_requires_exact_owned_current_branch_and_content(chat_db, tmp_path):
    origin = resolve_origin(chat_db, "01-orchestrator", {
        "platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
    })
    outbox = tmp_path / "outbox.sqlite"
    event = enqueue(outbox, "cron:3f0173195624:2026-09-24_08-30-58", origin, "expected")
    assert not acknowledge_visible(outbox, chat_db, origin, event)
    assert status(outbox, event) == "pending"
    chat_id = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"

    def set_chat(messages, current):
        with sqlite3.connect(chat_db) as db:
            db.execute("UPDATE chat SET chat=? WHERE id=?", (
                json.dumps({"history": {"messages": messages, "currentId": current}}), chat_id,
            ))

    msg = {"id": event, "parentId": "user-1", "role": "assistant",
           "content": "expected", "done": True}
    set_chat({"user-1": {"id": "user-1", "parentId": None}, event: msg}, "user-1")
    assert not acknowledge_visible(outbox, chat_db, origin, event)  # off branch
    set_chat({"user-1": {"id": "user-1", "parentId": None}, event: {**msg, "content": "wrong"}}, event)
    assert not acknowledge_visible(outbox, chat_db, origin, event)
    set_chat({"user-1": {"id": "user-1", "parentId": None}, event: {**msg, "done": False}}, event)
    assert not acknowledge_visible(outbox, chat_db, origin, event)
    set_chat({"user-1": {"id": "user-1", "parentId": None}, event: msg}, event)
    assert not acknowledge_visible(outbox, chat_db, {**origin, "owner_id": "owner-B"}, event)
    assert acknowledge_visible(outbox, chat_db, origin, event)
    assert pending(outbox, origin) == []
    assert status(outbox, event) == "acknowledged"


@pytest.mark.parametrize("broken", [
    {"id": "WRONG", "parentId": None},
    {"id": "user-1"},
    {"id": "user-1", "parentId": "missing"},
    {"id": "user-1", "parentId": "user-1"},
])
def test_receipt_refuses_broken_active_ancestor(chat_db, tmp_path, broken):
    origin = resolve_origin(chat_db, "01-orchestrator", {
        "platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
    })
    outbox = tmp_path / "outbox.sqlite"
    event = enqueue(outbox, "cron:broken-ancestor", origin, "expected")
    with sqlite3.connect(chat_db) as db:
        db.execute("UPDATE chat SET chat=?", (json.dumps({"history": {
            "messages": {"user-1": broken, event: {
                "id": event, "parentId": "user-1", "role": "assistant",
                "content": "expected", "done": True,
            }}, "currentId": event,
        }}),))
    assert not acknowledge_visible(outbox, chat_db, origin, event)
    assert status(outbox, event) == "pending"


def test_client_claimed_owner_or_thread_cannot_rebind_another_user(chat_db):
    assert resolve_origin(chat_db, "01-orchestrator", {
        "platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "user_id": "owner-B",
    }) is None
    assert resolve_origin(chat_db, "01-orchestrator", {
        "platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "user_id": "owner-A", "owner_id": "owner-B",
    }) is None
    assert resolve_origin(chat_db, "01-orchestrator", {
        "platform": "api_server", "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
        "thread_id": "other-topic",
    }) is None
