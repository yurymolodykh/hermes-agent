"""Unit tests for the medium<->review LANE pinner (adaptive-reasoning-effort,
card t_716762fb — design/medium-review-scheme-v1.md).

The new scheme replaces the class-based card default with an explicit graph
signal: a writing card that owns a direct child review card (assignee
09-independent-reviewer / 15-sre-qa) pins ``medium``; the review card itself
pins ``max``; everything else stays unpinned (NULL → profile default high).
Ж1: uncertainty degrades to ``high``, never ``medium`` on a gated/review card.
The whole layer stays inert while ``agent.reasoning_class_routing`` is off.
"""

from unittest.mock import MagicMock, patch

from agent.reasoning_effort_l1 import (
    REVIEWER_PROFILES,
    apply_card_lane_effort,
    resolve_card_lane_effort,
)


# ---------------------------------------------------------------------------
# Thin resolver: no DB — pure logic branches
# ---------------------------------------------------------------------------


class TestResolveLaneEffortWiredAndGate:
    def test_wired_effort_wins_and_clamps(self):
        cfg = {"agent": {"reasoning_class_routing": True}}
        # explicit operator pin always wins, clamped to the letovo domain
        assert resolve_card_lane_effort(None, "t_x", cfg, wired_effort="ultra") == "max"
        assert resolve_card_lane_effort(None, "t_x", cfg, wired_effort="none") == "none"

    def test_inert_when_routing_off(self):
        assert resolve_card_lane_effort(None, "t_x", {"agent": {}}) is None

    def test_uncertainty_degrades_to_high(self):
        # Ж1: no conn / no task -> never medium below a gated card.
        cfg = {"agent": {"reasoning_class_routing": True}}
        assert resolve_card_lane_effort(None, None, cfg) == "high"
        assert resolve_card_lane_effort(None, "", cfg) == "high"


class TestReviewerProfiles:
    def test_reviewer_set_holds_both_gates(self):
        assert "09-independent-reviewer" in REVIEWER_PROFILES
        assert "15-sre-qa" in REVIEWER_PROFILES


class _FakeTask:
    def __init__(self, tid, assignee, reasoning_effort=None):
        self.id = tid
        self.assignee = assignee
        self.reasoning_effort = reasoning_effort


# ---------------------------------------------------------------------------
# Graph-aware resolver + applier — the implementation reaches kanban_db
# through an in-function import ("from hermes_cli import kanban_db as _kb"),
# so we patch the real kanban_db.get_task / child_ids / set_reasoning_effort.
# ---------------------------------------------------------------------------


class TestGraphResolver:
    def test_review_card_pins_max(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.child_ids"
        ) as _kid:
            _gt.side_effect = lambda c, tid: _FakeTask(tid, "09-independent-reviewer")
            _kid.return_value = []
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, "t_rev", cfg) == "max"

    def test_container_card_pins_high(self):
        # A card whose ONLY child is a review card is a container: it used to
        # resolve to "medium", which the letovo backend rejects with HTTP 400.
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.child_ids"
        ) as _kid:
            _gt.side_effect = lambda c, tid: (
                _FakeTask("t_writer", "03-software-engineer")
                if tid == "t_writer"
                else _FakeTask("t_rev", "09-independent-reviewer")
            )
            _kid.side_effect = lambda c, tid: ["t_rev"] if tid == "t_writer" else []
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, "t_writer", cfg) == "high"

    def test_author_with_work_and_review_child_pins_high(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.child_ids"
        ) as _kid:
            _gt.side_effect = lambda c, tid: {
                "t_author": _FakeTask("t_author", "03-software-engineer"),
                "t_work": _FakeTask("t_work", "07-document-producer"),
                "t_rev": _FakeTask("t_rev", "15-sre-qa"),
            }[tid]
            _kid.side_effect = lambda c, tid: (
                ["t_work", "t_rev"] if tid == "t_author" else []
            )
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, "t_author", cfg) == "high"

    def test_card_without_review_child_stays_none(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.child_ids"
        ) as _kid:
            _gt.side_effect = lambda c, tid: _FakeTask(tid, "07-document-producer")
            _kid.return_value = []
            cfg = {"agent": {"reasoning_class_routing": True}}
            assert resolve_card_lane_effort(conn, "t_routine", cfg) is None


# ---------------------------------------------------------------------------
# Applier: seeds, never clobbers, gated no-op, best-effort on failures
# ---------------------------------------------------------------------------


class TestApplyLaneEffort:
    def test_seeds_high_on_container_with_review_child(self):
        conn = MagicMock()
        existing = _FakeTask("t_writer", "03-software-engineer", reasoning_effort=None)
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.child_ids"
        ) as _kid, patch(
            "hermes_cli.kanban_db.set_reasoning_effort"
        ) as mock_set, patch(
            "agent.reasoning_effort_l1.reasoning_class_routing_enabled", return_value=True
        ):
            _gt.side_effect = lambda c, tid: (
                existing if tid == "t_writer" else _FakeTask("t_rev", "09-independent-reviewer")
            )
            _kid.side_effect = lambda c, tid: ["t_rev"] if tid == "t_writer" else []
            mock_set.return_value = True
            ok = apply_card_lane_effort(conn, "t_writer")
        assert ok is True
        # Never "medium" — that value 400s the worker on the letovo backend.
        mock_set.assert_called_once_with(conn, "t_writer", "high")

    def test_seeds_max_on_review_card(self):
        conn = MagicMock()
        existing = _FakeTask("t_rev", "15-sre-qa", reasoning_effort=None)
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.child_ids"
        ) as _kid, patch(
            "hermes_cli.kanban_db.set_reasoning_effort"
        ) as mock_set, patch(
            "agent.reasoning_effort_l1.reasoning_class_routing_enabled", return_value=True
        ):
            _gt.return_value = existing
            _kid.return_value = []
            mock_set.return_value = True
            ok = apply_card_lane_effort(conn, "t_rev")
        assert ok is True
        mock_set.assert_called_once_with(conn, "t_rev", "max")

    def test_never_clobbers_existing_pin(self):
        conn = MagicMock()
        existing = _FakeTask("t_writer", "03-software-engineer", reasoning_effort="high")
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.set_reasoning_effort"
        ) as mock_set, patch(
            "agent.reasoning_effort_l1.reasoning_class_routing_enabled", return_value=True
        ):
            _gt.return_value = existing
            ok = apply_card_lane_effort(conn, "t_writer")
        assert ok is False
        mock_set.assert_not_called()

    def test_inert_when_routing_off(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "hermes_cli.kanban_db.set_reasoning_effort"
        ) as mock_set:
            ok = apply_card_lane_effort(conn, "t_x", {"agent": {}})
        assert ok is False
        _gt.assert_not_called()
        mock_set.assert_not_called()

    def test_best_effort_swallows_db_failure(self):
        conn = MagicMock()
        with patch("hermes_cli.kanban_db.get_task") as _gt, patch(
            "agent.reasoning_effort_l1.reasoning_class_routing_enabled", return_value=True
        ):
            _gt.side_effect = RuntimeError("db down")
            ok = apply_card_lane_effort(conn, "t_x")
        assert ok is False
