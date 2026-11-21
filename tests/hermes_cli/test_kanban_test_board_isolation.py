"""Test isolation of the kanban board: a pytest session must never touch the live board.

Field incident (t_e0c9e1ce): ad-hoc probes in a profile's scratch dir set
``HERMES_HOME`` to a bare tempdir and called ``kanban_db.init_db()`` /
``create_task()``. ``kanban_home()`` deliberately ignores ``HERMES_HOME``
(boards are shared across profiles), and ``_hermes_root_of`` folds any home
under the native ``~/.hermes`` back onto the live root — which includes the
profile scratch dir Hermes exports as ``TMPDIR``. Those writes landed in the
operator's live ``~/.hermes/kanban.db``; two "scratch card" / "worktree card"
rows then sat in the dispatcher queue and paged the owner.

Two layers cover this now: the hermetic fixture isolates the board root per
test, and the session sandbox (conftest import time) pins ``HERMES_KANBAN_HOME``
for collection-time imports / child processes and drops a live
``HERMES_KANBAN_DB``/``HERMES_KANBAN_BOARD`` injection.
"""

from __future__ import annotations

import os
import re
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kb_connect

from tests import conftest as _conftest


def _live_row_count() -> int:
    """Rows on the OPERATOR's real board, read-only; -1 when the file is absent."""
    path = _conftest._REAL_KANBAN_ROOT / "kanban.db"
    if not path.exists():
        return -1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    except sqlite3.Error:
        return -1
    finally:
        conn.close()


def test_board_resolves_away_from_the_live_root():
    """Inside a test the board root is a tempdir, not the operator's ~/.hermes."""
    home = kb.kanban_home().resolve()
    real = _conftest._REAL_KANBAN_ROOT.resolve()
    assert home != real, f"kanban_home() resolved to the live root {home}"
    assert not home.is_relative_to(real)
    assert kb.kanban_db_path().resolve() == (home / "kanban.db").resolve()


@pytest.mark.allow_real_home_io  # reads the live board READ-ONLY to prove nothing was written
def test_creating_a_card_never_writes_to_the_live_board():
    """Creating a card in a test leaves the operator's board row count untouched."""
    before = _live_row_count()
    home = kb.kanban_home().resolve()
    conn = kb_connect.connect_closing()
    with conn as c:
        tid = kb.create_task(c, title="isolation probe card", assignee="default")
        assert kb.get_task(c, tid) is not None
    assert kb.kanban_db_path().resolve().is_relative_to(home)
    assert _live_row_count() == before, (
        "a test created a card on the OPERATOR's live board"
    )


def test_production_kanban_home_is_recognised_and_redirected():
    """A pre-set production board root is detected, so the sandbox replaces it."""
    from hermes_state_guard import _real_platform_state_root

    real_root = _real_platform_state_root()
    assert real_root is not None
    assert _conftest._kanban_home_points_at_production("")
    assert _conftest._kanban_home_points_at_production(str(real_root))
    assert _conftest._kanban_home_points_at_production(
        str(real_root / "kanban" / "boards")
    )
    # A board root under the real home is still production, whatever pin named it.
    assert _conftest._kanban_home_points_at_production(
        str(real_root / "profiles" / "some-profile")
    )
    # A genuinely custom root is honored, not replaced.
    assert not _conftest._kanban_home_points_at_production("/tmp/some-custom-board-root")


def test_kanban_pins_are_listed_among_blanked_behavioral_vars():
    """The fixture blanks the dispatcher's board pins, so a worker shell cannot leak one in."""
    from tests._fixtures.env_filter import _HERMES_BEHAVIORAL_VARS

    for name in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        assert name in _HERMES_BEHAVIORAL_VARS


def test_the_isolation_rule_is_documented_where_the_tests_live():
    """The rule lives next to the tests, not only in a report."""
    agents_md = os.path.join(os.path.dirname(os.path.abspath(_conftest.__file__)), "AGENTS.md")
    with open(agents_md, encoding="utf-8") as fh:
        text = fh.read()
    assert re.search(r"HERMES_KANBAN_HOME", text), (
        "tests/AGENTS.md must state the kanban board isolation rule"
    )
