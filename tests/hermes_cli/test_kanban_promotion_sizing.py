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

import json
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


def test_cli_created_card_lands_ready_with_the_advisory(kanban_home: Path) -> None:
    """The CLI hole: ``hermes kanban create`` defaults to ``initial_status="running"``,
    which ``initial_task_state`` resolves to ``ready`` with no open parents — such a card
    never crosses a promotion, so creation is its only chance to be sized."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="cli big card", body=BIG, assignee="03-software-engineer",
            created_by="operator-01",
        )

        assert kb.get_task(conn, tid).status == "ready"
        comments = _sizing_comments(conn, tid)
        assert len(comments) == 1
        assert comments[0].author == "operator-01"


def test_created_card_that_fits_gets_no_advisory(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="cli small card", body=SMALL, assignee="03-software-engineer",
            created_by="operator-01",
        )
        assert kb.get_task(conn, tid).status == "ready"
        assert _sizing_comments(conn, tid) == []


def test_unknown_budget_stays_silent(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No readable budget means no guess: the rule stays silent rather than inventing a cap."""
    monkeypatch.setattr(
        "agent.kanban_card_sizing.assignee_iteration_budget", lambda assignee: None,
    )
    with kbc.connect() as conn:
        tid = _promotable(conn, BIG)
        assert kb.get_task(conn, tid).status == "ready"
        assert _sizing_comments(conn, tid) == []


def test_create_writes_the_default_hard_wall_into_the_row(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A card created without ``max_runtime_seconds`` carries the board's default wall
    in its own row: the wall belongs to the card, and the dispatcher has no global
    default.

    The expected figure comes from ``DEFAULT_MAX_RUNTIME_SECONDS`` — the single source of
    the policy — so the owner moving the limit is not itself a red test. The constant is
    then *substituted* (below the live value): if ``create_task`` ever stopped reading it
    and wrote a literal instead, the substituted card would keep the real number and this
    check would catch it, which a comparison against the constant alone cannot do.
    """
    from hermes_cli.kanban_db import DEFAULT_MAX_RUNTIME_SECONDS

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unstated wall", assignee="03-software-engineer")
        assert kb.get_task(conn, tid).max_runtime_seconds == DEFAULT_MAX_RUNTIME_SECONDS
        row = conn.execute(
            "SELECT max_runtime_seconds FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row[0] == DEFAULT_MAX_RUNTIME_SECONDS, (
            "the value must be in the row, not a read-time default"
        )

        # Substitute the policy with a figure no implementation literal can contain: the
        # created row must follow the constant, not a copy of yesterday's number.
        monkeypatch.setattr(kb, "DEFAULT_MAX_RUNTIME_SECONDS", 5_555)
        moved = kb.create_task(
            conn, title="unstated wall, moved policy", assignee="03-software-engineer",
        )
        assert kb.get_task(conn, moved).max_runtime_seconds == 5_555


def test_explicit_max_runtime_beats_the_default(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="stated wall", assignee="03-software-engineer",
            max_runtime_seconds=900,
        )
        assert kb.get_task(conn, tid).max_runtime_seconds == 900


def test_preexisting_null_row_stays_uncapped(kanban_home: Path) -> None:
    """Cards created before this default keep NULL (= no wall): the default is
    written at creation only, so nothing rescales an existing board."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="03-software-engineer")
        conn.execute("UPDATE tasks SET max_runtime_seconds = NULL WHERE id = ?", (tid,))
        conn.commit()
        assert kb.get_task(conn, tid).max_runtime_seconds is None


def test_create_tool_response_reports_minutes_and_the_written_wall(monkeypatch, tmp_path):
    """The ``kanban_create`` answer carries the minute estimate always and the wall the
    row actually got — read from the policy constant, never a literal.

    Two claims are kept apart on purpose. The default wall is wider than the 3-item
    estimate (81 min), so under the default the answer must carry the estimate and NO
    advisory; the advisory for the minute axis is checked on an explicitly *narrow* wall.
    Bundling both under one 3600 s default is exactly what made this check a copy of the
    policy instead of a check of it.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "agent.kanban_card_sizing.assignee_iteration_budget", lambda assignee: 300,
    )
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    from tools import kanban_tools as kt
    from hermes_cli.kanban_db import DEFAULT_MAX_RUNTIME_SECONDS

    body = "## Приёмка\n" + "".join(
        f"- критерий {i}\n" for i in range(3)
    )
    d = json.loads(kt._handle_create({
        "title": "three criteria", "assignee": "03-software-engineer",
        "body": body, "workspace_kind": "scratch",
    }))

    assert d["ok"] is True
    # 3 items -> the measured p80 of that bucket (81 min), not the retired 30-minute line.
    assert d["estimated_minutes"] == 81
    assert d["max_runtime_seconds"] == DEFAULT_MAX_RUNTIME_SECONDS
    # Default wall wider than the estimate and the iteration budget (130 <= 300) not
    # exceeded: no advisory, but the minute figure is still reported.
    assert "sizing_warning" not in d, d.get("sizing_warning")

    # An explicitly narrow wall (3600 s = 60 min) is under the 81-minute estimate: the
    # advisory must name both numbers, and the row keeps the wall the creator named.
    narrow = json.loads(kt._handle_create({
        "title": "narrow wall", "assignee": "03-software-engineer",
        "body": body, "workspace_kind": "scratch", "max_runtime_seconds": 3600,
    }))
    assert narrow["max_runtime_seconds"] == 3600
    assert "3600" in narrow["sizing_warning"] and "81 мин" in narrow["sizing_warning"]

    explicit = json.loads(kt._handle_create({
        "title": "narrower wall", "assignee": "03-software-engineer",
        "body": body, "workspace_kind": "scratch", "max_runtime_seconds": 900,
    }))
    assert explicit["max_runtime_seconds"] == 900
    assert "900" in explicit["sizing_warning"] and "81 мин" in explicit["sizing_warning"]


def test_create_response_is_silent_for_a_card_that_fits_both_axes(monkeypatch, tmp_path):
    """A card with no declared criteria gets the minute figure but no advisory, and a
    wide explicit wall clears the advisory the default wall would have raised."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "agent.kanban_card_sizing.assignee_iteration_budget", lambda assignee: 400,
    )
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    from tools import kanban_tools as kt

    quiet = json.loads(kt._handle_create({
        "title": "no criteria", "assignee": "03-software-engineer",
        "body": "just make it work\n", "workspace_kind": "scratch",
    }))
    assert quiet["ok"] is True and "sizing_warning" not in quiet
    assert quiet["estimated_minutes"] == 61

    body = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(4))
    wide = json.loads(kt._handle_create({
        "title": "wide wall", "assignee": "03-software-engineer",
        "body": body, "workspace_kind": "scratch", "max_runtime_seconds": 14_400,
    }))
    assert wide["max_runtime_seconds"] == 14_400
    assert "sizing_warning" not in wide, "160 iterations ≤ 400 and 79 min ≤ 240 min"
    assert wide["estimated_minutes"] == 79
