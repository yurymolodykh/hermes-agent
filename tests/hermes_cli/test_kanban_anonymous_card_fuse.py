"""Fuse: an authorless placeholder card never enters the LIVE queue.

Field incident (t_e0c9e1ce): probe scripts created cards with
``assignee="default"`` and no ``created_by``. They entered the live queue,
crash-looped on a missing install environment and paged the owner with
"failed, restarted automatically" three times before an operator archived them
by hand.

Rule: on the OPERATOR's live board a card with **no author** AND a placeholder
assignee (none at all, or ``default``) is refused at creation and, if one
already exists, never spawned by the dispatcher (bucketed as
``skipped_anonymous``). Legitimate cards — a real ``created_by`` — are
untouched, and every throwaway board is exempt.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """A throwaway board whose file this fixture can also declare 'live'."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield home, conn
    conn.close()


@pytest.fixture()
def declare_live(board, monkeypatch):
    """Make the board fixture's own file stand in for the operator's live board."""
    home, _ = board
    monkeypatch.setattr(
        kb, "_real_platform_kanban_roots",
        lambda: (home / "kanban.db", home / "kanban" / "boards"),
    )
    return home


def test_anonymous_default_card_is_refused_on_the_live_board(board, declare_live):
    _, conn = board
    with pytest.raises(ValueError, match="authorless|live board"):
        kb.create_task(conn, title="scratch card", assignee="default")


def test_anonymous_unassigned_card_is_refused_on_the_live_board(board, declare_live):
    _, conn = board
    with pytest.raises(ValueError, match="authorless|live board"):
        kb.create_task(conn, title="no owner")


def test_authored_card_still_lands_on_the_live_board(board, declare_live):
    _, conn = board
    tid = kb.create_task(conn, title="real work", assignee="default",
                         created_by="01-orchestrator")
    assert kb.get_task(conn, tid) is not None


def test_named_assignee_without_author_is_allowed_on_the_live_board(board, declare_live):
    """The owner column is the risk, not the author column: a named profile is a real route."""
    _, conn = board
    tid = kb.create_task(conn, title="routed work", assignee="05-rag-knowledge-engineer")
    assert kb.get_task(conn, tid) is not None


def test_anonymous_card_is_allowed_on_a_throwaway_board(board):
    """Every test/temp board is exempt — the suite's own fixtures rely on this."""
    _, conn = board
    tid = kb.create_task(conn, title="fixture card", assignee="default")
    assert kb.get_task(conn, tid) is not None


def test_dispatcher_skips_a_pre_existing_anonymous_card_on_the_live_board(board, declare_live):
    """A row that predates the fuse must not consume a worker slot."""
    _, conn = board
    tid = kb.create_task(conn, title="legacy junk", assignee="02-hermes-platform")
    conn.execute("UPDATE tasks SET created_by = NULL, assignee = 'default', status = 'ready' "
                 "WHERE id = ?", (tid,))
    conn.commit()
    spawned = []
    res = kbd.dispatch_once(
        conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
    )
    assert spawned == []
    assert tid in res.skipped_anonymous
    # The refusal is visible on the card, once.
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
    assert kinds.count("skipped_anonymous") == 1


def test_dispatcher_still_spawns_an_authored_card_on_the_live_board(board, declare_live):
    _, conn = board
    tid = kb.create_task(conn, title="real work", assignee="default",
                         created_by="01-orchestrator")
    spawned = []
    kbd.dispatch_once(
        conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
    )
    assert spawned == [tid]


def test_dispatcher_leaves_a_throwaway_board_alone(board):
    """Off the live board the bucket stays empty and the card spawns as before."""
    _, conn = board
    tid = kb.create_task(conn, title="fixture card", assignee="default")
    spawned = []
    res = kbd.dispatch_once(
        conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
    )
    assert spawned == [tid]
    assert res.skipped_anonymous == []
