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
