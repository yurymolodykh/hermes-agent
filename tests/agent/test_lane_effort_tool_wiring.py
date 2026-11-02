"""End-to-end wiring test for the medium<->review lane pinner (t_716762fb).

Drives the REAL tool handlers (kanban_tools._handle_create / _handle_link)
against an isolated temp kanban DB and asserts the reasoning_effort pins land
per design/medium-review-scheme-v1.md §3:

  (a) writer card that owns a review child (+09/15) -> high (medium 400s on letovo)
  (b) review card (assignee 09) -> max
  (c) card without a review child -> NULL
  (д) uncertainty -> high
  (е) routing off -> inert (no pins)

Requires an orchestrator-shaped environment (HERMES_KANBAN_BOARD telemetry is
patched as in test_kanban_origin_binding).
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    """Point kanban env + profile registry at a temp board (isolated)."""
    db_path = tmp_path / "tool_board" / "kanban.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    kb.init_db()

    def fake_profile_exists(name):
        return name in {
            "02-hermes-platform", "03-software-engineer", "07-document-producer",
            "09-independent-reviewer", "15-sre-qa", "peer",
        }

    monkeypatch.setattr(profiles, "profile_exists", fake_profile_exists)
    monkeypatch.setattr(kb, "_provider_route_blocker", lambda _p, _m: None)
    # The board's master switch: 01-orchestrator enables the lane pinner.
    monkeypatch.setattr(
        "tools.kanban_tools.load_config",
        lambda: {"agent": {"reasoning_class_routing": True}},
    )
    return db_path


def _handle_create_routed(board_env, **kwargs):
    """Call the real _handle_create with the isolated board + env baked in."""
    from tools import kanban_tools as kt

    with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": str(board_env)}):
        out = kt._handle_create({"title": kwargs.pop("_title", "t"), **kwargs})
    return out


def test_tool_create_links_review_child_and_parent(tmp_path, monkeypatch, isolated_board):
    """kanban_create of a review card seeds max on it + high on its writer parent."""
    import tools.kanban_tools as kt
    from hermes_cli import kanban_db as kb

    # Writer created first (unpinned yet — no review child at creation).
    w = _handle_create_routed(isolated_board, _title="writer", assignee="03-software-engineer")
    w = json.loads(w)

    # Review card as a child of the writer.
    r = _handle_create_routed(
        isolated_board,
        _title="review", assignee="09-independent-reviewer", parents=[w["task_id"]],
    )
    r = json.loads(r)

    conn = kb.connect(db_path=isolated_board)
    try:
        tw = kb.get_task(conn, w["task_id"])
        tr = kb.get_task(conn, r["task_id"])
    finally:
        conn.close()

    # (b) review card itself pins max.
    assert tr.reasoning_effort == "max", tr.reasoning_effort
    # (a) writer owner of the review child pins high — medium is rejected by the letovo wire (HTTP 400).
    assert tw.reasoning_effort == "high", tw.reasoning_effort


def test_tool_create_routine_card_owns_a_default(tmp_path, monkeypatch, isolated_board):
    """kanban_create without a review child still stores an explicit level.

    This used to assert NULL ("the profile decides"). t_f2dd1fb5 removed that
    inheritance: such a card now carries its own ``high`` default, marked as a
    default rather than an operator pin.
    """
    from hermes_cli import kanban_db as kb

    c = _handle_create_routed(isolated_board, _title="routine", assignee="07-document-producer")
    c = json.loads(c)

    conn = kb.connect(db_path=isolated_board)
    try:
        t = kb.get_task(conn, c["task_id"])
        assert t.reasoning_effort == "high"
        assert t.reasoning_effort_source == "default"
    finally:
        conn.close()


def test_tool_link_promotes_existing_writer_to_high(tmp_path, monkeypatch, isolated_board):
    """kanban_link of a review child under an existing writer promotes to high."""
    import tools.kanban_tools as kt
    from hermes_cli import kanban_db as kb

    w = _handle_create_routed(isolated_board, _title="writer", assignee="03-software-engineer")
    w = json.loads(w)
    r = _handle_create_routed(isolated_board, _title="review", assignee="15-sre-qa")
    r = json.loads(r)

    out = kt._handle_link({"parent_id": w["task_id"], "child_id": r["task_id"]})
    out = json.loads(out)

    conn = kb.connect(db_path=isolated_board)
    try:
        tw = kb.get_task(conn, w["task_id"])
        tr = kb.get_task(conn, r["task_id"])
    finally:
        conn.close()

    assert tw.reasoning_effort == "high", tw.reasoning_effort
    assert tr.reasoning_effort == "max", tr.reasoning_effort
