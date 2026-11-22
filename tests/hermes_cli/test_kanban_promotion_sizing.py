"""Card-sizing advisory on ``todo -> ready`` promotion (both routes).

The rule reached only ``kanban_create``'s tool response, so a CLI-created card and
every dispatcher promotion were silent. These tests pin the hole closed:

* an over-budget card promoted to ``ready`` gets one comment, and the promotion stands;
* a card that fits gets none;
* a second promotion adds no second comment (the tag is the idempotency key);
* both promotion routes write it (the dispatcher's ``recompute_ready`` and an
  operator's ``promote_task``).

The budget is a fixture (``assignee_iteration_budget`` patched), because it reads the
assignee's real ``config.yaml`` — a test must not depend on the host's profiles.

Note on the fixture: ``complete_task`` promotes the released child itself
(``parent_ids`` — "re-evaluate promotion eligibility for the child immediately"), so a
child is already ``ready`` when ``_promotable`` returns. Tests that need the tick path
put the card back in ``todo`` first, exactly as a re-gate does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sizing as kbs

BUDGET = 120

BIG = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(9))
SMALL = "## Приёмка\n- один критерий\n- второй критерий\n"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB and a fixed assignee budget."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "agent.kanban_card_sizing.assignee_iteration_budget", lambda assignee: BUDGET,
    )
    kb.init_db()
    return home


def _promotable(conn, body: str) -> str:
    """A card released to ``ready`` by its parent's completion (the promotion path)."""
    parent = kb.create_task(conn, title="parent", assignee="03-software-engineer")
    child = kb.create_task(
        conn, title="child", body=body, assignee="03-software-engineer", parents=[parent],
    )
    assert kb.get_task(conn, child).status == "todo"
    kb.claim_task(conn, parent)
    assert kb.complete_task(conn, parent, summary="done")
    return child


def _sizing_comments(conn, task_id: str) -> list:
    return [c for c in kb.list_comments(conn, task_id) if kbs.SIZING_COMMENT_TAG in c.body]


def _back_to_todo(conn, task_id: str) -> None:
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    conn.commit()


def test_over_budget_card_gets_one_comment_and_promotion_stands(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _promotable(conn, BIG)

        task = kb.get_task(conn, tid)
        assert task.status == "ready", "the advisory must never block the promotion"
        comments = _sizing_comments(conn, tid)
        assert len(comments) == 1
        body = comments[0].body
        assert "9" in body and str(BUDGET) in body and "Рекомендация" in body


def test_dispatcher_tick_promotion_writes_the_comment(kanban_home: Path) -> None:
    """The re-gate path (card back in ``todo``, parents done) through ``recompute_ready``."""
    with kbc.connect() as conn:
        tid = _promotable(conn, BIG)
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (tid,))
        _back_to_todo(conn, tid)

        assert kb.recompute_ready(conn) == 1

        assert kb.get_task(conn, tid).status == "ready"
        assert len(_sizing_comments(conn, tid)) == 1


def test_card_that_fits_gets_no_comment(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _promotable(conn, SMALL)

        assert kb.get_task(conn, tid).status == "ready"
        assert _sizing_comments(conn, tid) == []


def test_second_promotion_adds_no_second_comment(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _promotable(conn, BIG)
        assert len(_sizing_comments(conn, tid)) == 1

        _back_to_todo(conn, tid)
        assert kb.recompute_ready(conn) == 1

        assert kb.get_task(conn, tid).status == "ready"
        assert len(_sizing_comments(conn, tid)) == 1


def test_operator_promote_writes_the_same_advisory(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="cli card", body=BIG, assignee="03-software-engineer",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        ok, err = kb.promote_task(conn, tid, actor="operator")

        assert ok, err
        assert kb.get_task(conn, tid).status == "ready"
        assert len(_sizing_comments(conn, tid)) == 1

        # Idempotent across the two routes as well.
        _back_to_todo(conn, tid)
        ok, err = kb.promote_task(conn, tid, actor="operator")
        assert ok, err
        assert len(_sizing_comments(conn, tid)) == 1


def test_unknown_budget_stays_silent(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No readable budget means no guess: the rule stays silent rather than inventing a cap."""
    monkeypatch.setattr(
        "agent.kanban_card_sizing.assignee_iteration_budget", lambda assignee: None,
    )
    with kbc.connect() as conn:
        tid = _promotable(conn, BIG)
        assert kb.get_task(conn, tid).status == "ready"
        assert _sizing_comments(conn, tid) == []
