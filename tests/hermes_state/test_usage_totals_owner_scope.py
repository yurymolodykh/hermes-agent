"""``usage_totals`` must apply the owner scope it advertises.

The desktop sidebar builds its Telegram/messaging slice in
``/api/profiles/sessions/sidebar``; that handler passes the SAME ``owner_scope``
to ``list_sessions_rich`` and to ``usage_totals``. The scope arm comes from
``_owner_scope_clause``, which is written against the table alias ``s``
(``COALESCE(s.user_id, '') IN (...)``). The aggregate query must therefore carry
that alias: without it the whole per-profile slice raises
``no such column: s.user_id``, the handler records the error and the section
renders empty for everybody — including the owner with chats.

Rows are seeded with SQL rather than ``append_message`` on purpose: this test is
about the aggregate's SQL shape, and a message append drags the agent import
chain in.
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db, sid, *, user_id, tokens=0, cost=0.0):
    """One root session row with an owner stamp and a token/cost total."""
    db.create_session(sid, "telegram", user_id=user_id)
    db._write_sql(
        "UPDATE sessions SET message_count = 1, input_tokens = ?, "
        "estimated_cost_usd = ? WHERE id = ?",
        (tokens, cost, sid),
    )


def test_scoped_usage_totals_raise_nothing_and_count_only_the_owner(db):
    """Without the ``s`` alias this raises OperationalError: no such column: s.user_id."""
    _seed(db, "own-a", user_id="uid:1001", tokens=10, cost=0.5)
    _seed(db, "own-b", user_id="uid:1001", tokens=5, cost=0.25)
    _seed(db, "foreign", user_id="person/maria.safronova", tokens=1000, cost=99.0)

    scoped = db.usage_totals(user_id="uid:1001")

    assert scoped["tokens"] == 15
    assert scoped["cost_usd"] == pytest.approx(0.75)


def test_scoped_usage_totals_accept_several_owner_stamps(db):
    """A subject carries several stored stamps; the aggregate totals all of them, no one else."""
    _seed(db, "tg-bare", user_id="125805788", tokens=7)
    _seed(db, "tg-prefixed", user_id="tg:125805788", tokens=3)
    _seed(db, "other", user_id="999", tokens=100)

    assert db.usage_totals(user_id=["125805788", "tg:125805788"])["tokens"] == 10


def test_empty_scope_totals_nothing(db):
    """Fail closed: an identity that could not be established has no total at all."""
    _seed(db, "own", user_id="uid:1001", tokens=10)

    assert db.usage_totals(user_id=[])["tokens"] == 0


def test_unscoped_usage_totals_still_sum_the_whole_store(db):
    """``None`` (acl off) keeps the previous query: same numbers, no scope arm."""
    _seed(db, "own", user_id="uid:1001", tokens=10)
    _seed(db, "foreign", user_id="999", tokens=90)

    assert db.usage_totals()["tokens"] == 100
