"""A4 (t_5d3bc199): durable create-path subscriptions carry the verified
Open WebUI owner user_id so ``stage_terminal`` treats them as bound.

Before this change the durable marker route wrote ``kanban_notify_subs`` with
``user_id=None``; ``stage_terminal`` rejects those as ``"unbound"`` and the
terminal итог was silently dropped (analysis A §5 A4 — live durable board had
9/9 rows with user_id=None).  This test locks the fix: with a server-verified
origin binding the sub row carries the real owner; without one it stays
fail-closed (None), never fabricating a user id.
"""
import json
import sqlite3
from unittest.mock import patch

import pytest

from gateway.durable_chat_tasks import (
    BOARD, execute_durable_chat_command, parse_durable_chat_command,
)
from gateway.kanban_origin_delivery import stage_terminal
from hermes_cli import kanban_db as kb

RAW = "b5b7fc5b-1111-4222-8333-444455556666"
OWNER = "maria-user-id"


@pytest.fixture
def durable_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + a mini OWUI registry owned by a NON-owner."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(tmp_path / "webui.db"))
    db_path = tmp_path / "webui.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (
            RAW, OWNER, json.dumps({
                "history": {"messages": {"u": {"id": "u", "role": "user", "parentId": None}},
                            "currentId": "u"},
            })))
    # Route the durable board to an isolated board slug in the temp home.
    import gateway.durable_chat_tasks as m
    original = m.BOARD
    m.BOARD = "a4-test-board"
    yield tmp_path, db_path
    m.BOARD = original


def _create_with_binding(tmp_path, db_path, *, binding, prompt="ДЛИННАЯ ЗАДАЧА\nОтчёт"):
    cmd = parse_durable_chat_command(prompt)
    assert cmd is not None and cmd.action == "create"
    return execute_durable_chat_command(
        cmd,
        session_id="owui-" + RAW,
        assignee="default",
        notifier_profile="default",
        origin_binding=binding,
    )


def test_durable_create_with_verified_origin_writes_owner_user_id(durable_env):
    tmp_path, db_path = durable_env
    binding = {
        "chat_id": "owui-" + RAW,
        "session_id": "owui-" + RAW,
        "user_id": OWNER,
        "profile": "default",
    }
    result = _create_with_binding(tmp_path, db_path, binding=binding)
    assert result.ok, result.message
    conn = sqlite3.connect(kb.kanban_db_path("a4-test-board"))
    try:
        row = conn.execute(
            "SELECT user_id, chat_id, delivery_mode FROM kanban_notify_subs"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    user_id, chat_id, mode = row
    assert user_id == OWNER, "durable sub must carry the verified owner user_id"
    assert chat_id == "owui-" + RAW
    assert mode == "wake"


def test_durable_create_with_verified_origin_stage_is_bound(durable_env):
    """The sub row produced by the durable create with a binding must stage as
    pending/acknowledged (NOT 'unbound') when a terminal event arrives."""
    tmp_path, db_path = durable_env
    binding = {
        "chat_id": "owui-" + RAW,
        "session_id": "owui-" + RAW,
        "user_id": OWNER,
        "profile": "default",
    }
    result = _create_with_binding(tmp_path, db_path, binding=binding)
    conn = sqlite3.connect(kb.kanban_db_path("a4-test-board"))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM kanban_notify_subs").fetchone()
        sub = {k: row[k] for k in row.keys()}
    finally:
        conn.close()
    state, delivery_id = stage_terminal(
        tmp_path / "outbox.sqlite", db_path, sub,
        "default", "a4-test-board", 1, "итог длинной задачи",
    )
    assert state in ("pending", "acknowledged"), f"expected bound, got {state}"


def test_durable_create_without_binding_stays_fail_closed(durable_env):
    """No verified origin → the sub row keeps user_id=None (fail-closed): the
    durable create must NOT fabricate a user id from client-side data."""
    tmp_path, db_path = durable_env
    result = _create_with_binding(tmp_path, db_path, binding=None)
    assert result.ok, result.message
    conn = sqlite3.connect(kb.kanban_db_path("a4-test-board"))
    try:
        user_id = conn.execute(
            "SELECT user_id FROM kanban_notify_subs"
        ).fetchone()[0]
    finally:
        conn.close()
    assert user_id is None, "no verified origin must stay fail-closed None"


def test_foreign_origin_binding_is_rejected(durable_env):
    """A binding whose user_id does NOT own the chat in the registry must not
    leak into the subscription (server-side re-verification fails closed)."""
    tmp_path, db_path = durable_env
    binding = {
        "chat_id": "owui-" + RAW,
        "session_id": "owui-" + RAW,
        "user_id": "someone-else",  # does not own RAW chat
        "profile": "default",
    }
    result = _create_with_binding(tmp_path, db_path, binding=binding)
    assert result.ok, result.message
    conn = sqlite3.connect(kb.kanban_db_path("a4-test-board"))
    try:
        user_id = conn.execute(
            "SELECT user_id FROM kanban_notify_subs"
        ).fetchone()[0]
    finally:
        conn.close()
    assert user_id is None, "foreign owner must fail closed (user_id=None)"
