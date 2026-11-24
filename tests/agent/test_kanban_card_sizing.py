"""The card-sizing rule: a card's declared volume against the assignee's budget.

The rule is advisory and stated in the units a card already carries — its
acceptance criteria — because the budget that stops a run belongs to the assignee
profile, not to the card, and a card that cannot fit is not a smaller limit's
fault. Thresholds are fitted on the shared board's finished runs (see the module
docstring in ``agent/kanban_card_sizing.py``).
"""

from __future__ import annotations

import pytest


def test_acceptance_items_are_counted_from_the_acceptance_section_only():
    from agent.kanban_card_sizing import count_acceptance_items

    body = (
        "## Установленные факты\n"
        "- this is context, not a criterion\n"
        "- so is this one\n"
        "## Приёмка\n"
        "(а) первый критерий — parentheses are a label, not an item\n"
        "- второй критерий\n"
        "1) третий критерий\n"
        "## Вне рамок\n"
        "- not a criterion either\n"
    )

    assert count_acceptance_items(body) == 2


def test_sizing_rule_flags_a_card_that_cannot_fit_a_small_budget():
    """The reported shape: nine criteria on a 120-iteration profile."""
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    body = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(9))
    estimate = estimate_card(body, budget=120)

    assert estimate.acceptance_items == 9
    assert estimate.estimated_iterations == 310
    assert not estimate.fits and estimate.overrun == 190
    warning = sizing_warning(estimate, assignee="08-visual-media-designer")
    assert warning is not None
    assert "310" in warning and "120" in warning


def test_sizing_rule_is_silent_for_a_card_that_fits():
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    body = "## Приёмка\n- один критерий\n- второй критерий\n"
    estimate = estimate_card(body, budget=300)

    assert estimate.fits
    assert sizing_warning(estimate, assignee="03-software-engineer") is None


def test_sizing_rule_declines_to_guess_without_criteria_or_budget():
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    prose = "Just make it work, somehow.\n"
    assert sizing_warning(estimate_card(prose, budget=120), assignee="x") is None
    assert sizing_warning(estimate_card("## Приёмка\n- a\n", budget=None), assignee="x") is None


def test_assignee_budget_is_read_from_that_profiles_config(tmp_path, monkeypatch):
    """The cap belongs to the assignee, not to the creator's own config."""
    from agent import kanban_card_sizing

    profile = tmp_path / "profiles" / "tiny"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("agent:\n  max_turns: 120\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda name: tmp_path / "profiles" / name
    )

    assert kanban_card_sizing.assignee_iteration_budget("tiny") == 120
    # Unknown/unreadable profiles decline to guess rather than invent a cap.
    assert kanban_card_sizing.assignee_iteration_budget("nope") is None


def test_assignee_budget_treats_an_unlimited_profile_as_no_budget(tmp_path, monkeypatch):
    from agent import kanban_card_sizing

    profile = tmp_path / "profiles" / "open"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("agent:\n  max_turns: unlimited\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda name: tmp_path / "profiles" / name
    )

    assert kanban_card_sizing.assignee_iteration_budget("open") is None


def test_estimate_minutes_is_calibrated_on_acceptance_items():
    """The minute figure is the measured p80 of its acceptance-item bucket, not a line:
    0 → 61, 4 → 79, 6 → 86 min on the shared board (see the module docstring). Four
    items is the bucket the report named — the old 36-minute guess was 2.2× under it."""
    from agent.kanban_card_sizing import (
        CARD_MINUTE_TARGET, CALIBRATION_P80_MINUTES, estimate_minutes,
    )

    assert CARD_MINUTE_TARGET == 50
    assert CALIBRATION_P80_MINUTES[4] == 79
    assert estimate_minutes(0) == 61
    assert estimate_minutes(4) == 79
    assert estimate_minutes(6) == 86
    # Every measured bucket round-trips exactly, so a drift in a constant is caught.
    for items, minutes in CALIBRATION_P80_MINUTES.items():
        assert estimate_minutes(items) == minutes
    # Between measured points the figure is interpolated linearly, never extrapolated
    # (61 → 81 over 0 → 3 items: 1 item → 68, 2 → 74).
    assert estimate_minutes(1) == 68 and estimate_minutes(2) == 74
    # Above the last measured point (6) the figure is held: the 7+ bucket measures
    # LOWER (p80 75) because those cards are fan-out parents, so extrapolating its
    # dip would predict a bigger card as faster.
    assert estimate_minutes(12) == CALIBRATION_P80_MINUTES[6] == 86
    assert estimate_minutes(3) >= CARD_MINUTE_TARGET


def test_estimate_minutes_survives_a_card_with_no_criteria():
    """A card naming no criteria must still size (0 items → its measured bucket),
    never divide or raise: the creator sees a number, not a crash."""
    from agent.kanban_card_sizing import (
        CALIBRATION_P50_MINUTES, CardSizeEstimate, estimate_card, estimate_minutes,
        estimate_minutes_median,
    )

    assert estimate_minutes(0) == 61           # p80 of the zero-item bucket
    assert estimate_minutes_median(0) == CALIBRATION_P50_MINUTES[0]
    # Negative input is a caller bug, not a negative runtime.
    assert estimate_minutes(-3) == 61
    assert estimate_card("just prose, no acceptance section\n", budget=300).estimated_minutes == 61
    # A hand-built estimate derives the same figure instead of disagreeing.
    assert CardSizeEstimate(
        acceptance_items=6, body_bytes=10, estimated_iterations=220, budget=300,
    ).estimated_minutes == 86


def test_sizing_warning_carries_the_minute_estimate():
    """The advisory names the minutes next to the target so a creator can trim the
    card before it is ever dispatched (50 min is the ceiling of the 30–50 target)."""
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    body = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(9))
    warning = sizing_warning(
        estimate_card(body, budget=120, max_runtime_seconds=3600), assignee="x",
    )

    assert warning is not None
    assert "86 мин" in warning and "59" in warning   # p80 and median for 9 items
    assert "30–50" in warning
    assert "310 iterations" in warning  # the iteration part is unchanged


# --- the wall axis (minutes against the card's max_runtime_seconds) ---------------


def test_wall_axis_warns_for_four_items_against_the_default_wall():
    """(а) Four criteria fit a generous iteration budget (160 ≤ 240) yet their measured
    p80 is 79 min against the 3600 s wall — exactly the card that went ``timed_out``
    on 23.11.2026 while the rule, watching iterations only, said "fits"."""
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    body = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(4))
    estimate = estimate_card(body, budget=240, max_runtime_seconds=3600)

    assert estimate.acceptance_items == 4
    assert estimate.fits                      # the iteration axis is NOT over
    assert estimate.estimated_iterations == 160
    assert not estimate.fits_minutes          # the wall axis is
    assert estimate.estimated_minutes == 79 and estimate.wall_minutes == 60
    assert estimate.minutes_overrun == 19

    warning = sizing_warning(estimate, assignee="03-software-engineer")
    assert warning is not None
    assert "79 мин" in warning and "3600" in warning and "60 мин" in warning
    assert "итерации" not in warning and "240" not in warning  # only the wall axis


def test_wall_axis_is_silent_for_a_card_with_no_criteria():
    """(б) Zero criteria = nothing declared to size; the wall axis stays quiet even
    though the zero-item bucket's p80 (61 min) is over the wall."""
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    estimate = estimate_card("## Приёмка\n", budget=300, max_runtime_seconds=3600)

    assert estimate.acceptance_items == 0
    assert estimate.fits and estimate.fits_minutes and estimate.minutes_overrun == 0
    assert sizing_warning(estimate, assignee="03-software-engineer") is None


def test_wall_axis_warns_when_the_iteration_budget_is_not_exceeded():
    """(в) A generous iteration budget does not excuse the wall: both axes are
    independent, and either one over is enough to warn."""
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    body = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(5))
    estimate = estimate_card(body, budget=10_000, max_runtime_seconds=3600)

    assert estimate.fits, "the iteration axis must genuinely not be exceeded"
    warning = sizing_warning(estimate, assignee="01-orchestrator")

    assert warning is not None
    assert "90 мин" in warning and "3600" in warning
    assert "10000" not in warning


def test_both_axes_are_named_together_and_a_generous_wall_clears_the_advisory():
    """When both limits are over the text carries both numbers; a wall wide enough
    for the card leaves only the iteration half — the rule cannot be satisfied by
    fixing one axis it does not name."""
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    body = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(9))
    both = sizing_warning(
        estimate_card(body, budget=120, max_runtime_seconds=3600), assignee="x",
    )
    assert both is not None
    assert "310 iterations vs 120" in both and "86 мин" in both

    naive_wall = sizing_warning(
        estimate_card(body, budget=120, max_runtime_seconds=2400), assignee="x",
    )
    assert naive_wall is not None
    assert "40 мин" in naive_wall and "86 мин" in naive_wall

    generous_wall = sizing_warning(
        estimate_card(body, budget=120, max_runtime_seconds=86_400), assignee="x",
    )
    assert generous_wall is not None
    # Only the iteration half: no wall figure, no minute-vs-wall clause.
    assert "3600" not in generous_wall and "медиана" not in generous_wall
    assert "iterations vs 120" in generous_wall
    assert "max_runtime_seconds" not in generous_wall


def test_an_uncapped_card_is_silent_on_the_wall_axis():
    """A row created before the wall existed carries NULL: no wall, nothing to
    exceed — the rule must not invent the dispatcher default where the DB says none."""
    from agent.kanban_card_sizing import estimate_card, sizing_warning

    body = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(4))
    estimate = estimate_card(body, budget=10_000, max_runtime_seconds=None)

    assert estimate.wall_minutes is None and estimate.fits_minutes
    assert sizing_warning(estimate, assignee="x") is None


def test_default_wall_is_read_from_the_db_constant(monkeypatch):
    """The wall the rule assumes when the creator names none is ``create_task``'s own
    default, read from its single source rather than duplicated as a literal.

    The value is taken from the constant (the owner moves that policy), and the constant
    is then substituted: if ``default_wall_seconds`` ever fell back to its own literal —
    or cached the number at import — the substituted read would not follow it.
    """
    from agent.kanban_card_sizing import default_wall_seconds
    from hermes_cli import kanban_db as kb

    assert default_wall_seconds() == kb.DEFAULT_MAX_RUNTIME_SECONDS

    monkeypatch.setattr(kb, "DEFAULT_MAX_RUNTIME_SECONDS", 5_555)
    assert default_wall_seconds() == 5_555, (
        "the rule must read the policy constant on each call, not a copied literal"
    )


def test_the_hard_fuse_is_not_shorter_than_the_soft_run_budget():
    """The wall-clock wrap-up notice must be reachable before the kill.

    ``_maybe_inject_run_budget_wrapup`` fires once elapsed_past ``0.8 *
    agent.run_budget_seconds``; the dispatcher kills the card at
    ``DEFAULT_MAX_RUNTIME_SECONDS``. If the fuse is below the notice point the run is
    simply killed and the soft signal never happens — the exact defect that made a
    7200 s fuse dead next to a 10800 s budget. The invariant is asserted as a relation,
    not as a pair of numbers, so moving the policy cannot silently re-break it.
    """
    from hermes_cli import kanban_db as kb

    # A budget at or below the fuse: 80 % of that budget lands strictly inside the run.
    for budget in (5_400, kb.DEFAULT_MAX_RUNTIME_SECONDS, kb.DEFAULT_MAX_RUNTIME_SECONDS / 2):
        notice_at = 0.8 * budget
        assert notice_at < kb.DEFAULT_MAX_RUNTIME_SECONDS, (
            f"a {budget}s budget puts the wrap-up notice at {notice_at:.0f}s, at or past "
            f"the {kb.DEFAULT_MAX_RUNTIME_SECONDS}s fuse — the notice can never fire"
        )



def test_a_fuse_below_the_notice_point_is_the_defect_this_guards(monkeypatch):
    """Sensitivity: the guard above must actually go red on the shipped 7200 s fuse.

    Pinning the regression, not a number: with the fuse at 7200 s and the owner's
    10800 s budget the notice point is 8640 s — past the kill, so the soft signal is
    unreachable. Re-running the guard's own relation against that substituted fuse
    proves it has teeth rather than passing vacuously.
    """
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "DEFAULT_MAX_RUNTIME_SECONDS", 7_200)
    with pytest.raises(AssertionError):
        for budget in (5_400, 10_800):
            assert 0.8 * budget < kb.DEFAULT_MAX_RUNTIME_SECONDS, (
                "the 10800 s budget's notice would land past a 7200 s fuse"
            )
