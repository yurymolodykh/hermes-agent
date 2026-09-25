"""Durable, origin-bound delivery claims are not user-visible ACKs."""

import sqlite3

import pytest

from gateway import origin_outbox as outbox


ORIGIN = {
    "platform": "api_server", "profile": "01-orchestrator",
    "chat_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
    "session_id": "owui-98d69f26-8263-4e1c-84a5-6d9fcdbf593d",
    "owner_id": "server-authorized-owner", "thread_id": None,
}


def test_enqueue_duplicate_and_exact_owner_ack_survive_reopen(tmp_path):
    path = tmp_path / "delivery.sqlite"
    event = "cron:3f0173195624:2026-09-24_08-30-58"
    a = outbox.enqueue(path, event, ORIGIN, "СТАТУС БЭКАПОВ HERMES")
    b = outbox.enqueue(path, event, ORIGIN, "СТАТУС БЭКАПОВ HERMES")
    assert a == b
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM origin_delivery").fetchone()[0] == 1
    assert outbox.pending(path, ORIGIN) == [{"id": a, "source_id": event, "content": "СТАТУС БЭКАПОВ HERMES"}]
    assert not outbox.ack(path, a, {**ORIGIN, "owner_id": "other-owner"}, "client-receipt-1")
    assert not outbox.ack(path, a, {**ORIGIN, "chat_id": "other-chat"}, "client-receipt-1")
    assert not outbox.ack(path, a, {**ORIGIN, "profile": "other-profile"}, "client-receipt-1")
    assert not outbox.ack(path, a, {**ORIGIN, "session_id": "other-session"}, "client-receipt-1")
    assert outbox.ack(path, a, ORIGIN, "client-receipt-1")
    assert outbox.pending(path, ORIGIN) == []
    assert outbox.status(path, a) == "acknowledged"
    assert outbox.enqueue(path, event, ORIGIN, "СТАТУС БЭКАПОВ HERMES") == a
    assert outbox.pending(path, ORIGIN) == []


def test_legacy_outbox_schema_migrates_without_losing_pending(tmp_path):
    path = tmp_path / "delivery.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE origin_delivery (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL, destination TEXT NOT NULL,
          content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          receipt TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          ack_at TEXT, UNIQUE(source_id, destination))""")
    event = outbox.enqueue(path, "run-1", ORIGIN, "output")
    assert outbox.due(path, now=0)[0]["id"] == event
    assert outbox.counts(path) == {"pending": 1}


def test_missing_origin_and_pwa_owner_fail_closed(tmp_path):
    for origin in ({}, {**ORIGIN, "chat_id": ""}, {**ORIGIN, "owner_id": ""},
                   {**ORIGIN, "platform": "pwa", "owner_id": ""}):
        with pytest.raises(ValueError):
            outbox.enqueue(tmp_path / "delivery.sqlite", "event", origin, "secret")
    assert not (tmp_path / "delivery.sqlite").exists()
