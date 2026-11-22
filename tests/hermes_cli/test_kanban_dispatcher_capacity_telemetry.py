"""Health-telemetry proof: a capacity-bound tick is saturation, not a stall.

Live defect (21.11.2026): the embedded gateway dispatcher logged

    kanban dispatcher stuck: ready queue non-empty for 36 consecutive ticks
    but 0 workers spawned. Check profile health (venv, PATH, credentials)

while the card key pool was fully leased (``keypool_capped=10``) and seven
workers were running. The hint sent an investigation into a venv that had no
defect and hid the real reason (pool capacity). ``capacity_bound`` is the
classifier both telemetry surfaces (gateway watcher and CLI daemon) consult so
only operator-actionable holds keep raising the warning.
"""
from __future__ import annotations

from hermes_cli.kanban_db_dispatch import DispatchResult, capacity_bound, describe_suppression


def test_keypool_cap_is_saturation_not_stall():
    res = DispatchResult(skipped_keypool_capped=["t_a", "t_b"])
    assert capacity_bound([res]) is True


def test_per_profile_cap_is_saturation_not_stall():
    res = DispatchResult(skipped_per_profile_capped=[("t_a", "02-hermes-platform", 3)])
    assert capacity_bound([res]) is True


def test_mixed_capacity_holds_are_saturation():
    pooled = DispatchResult(skipped_keypool_capped=["t_a"])
    capped = DispatchResult(skipped_per_profile_capped=[("t_b", "15-sre-qa", 3)])
    assert capacity_bound([pooled, capped]) is True


def test_env_unready_keeps_the_warning():
    res = DispatchResult(
        skipped_keypool_capped=["t_a"],
        skipped_env_unready=[("t_b", "no committed dependency environment")],
    )
    assert capacity_bound([res]) is False


def test_rate_limited_keeps_the_warning():
    res = DispatchResult(rate_limited=["t_a"], skipped_keypool_capped=["t_b"])
    assert capacity_bound([res]) is False


def test_respawn_guard_keeps_the_warning():
    res = DispatchResult(respawn_guarded=[("t_a", "blocker_auth")],
                         skipped_keypool_capped=["t_b"])
    assert capacity_bound([res]) is False


def test_memory_pressure_keeps_the_warning():
    assert capacity_bound([DispatchResult(memory_pressure="critical")]) is False
    assert capacity_bound([DispatchResult(memory_pressure="elevated",
                                          skipped_keypool_capped=["t_a"])]) is False


def test_locked_tick_keeps_the_warning():
    assert capacity_bound([DispatchResult(skipped_locked=True)]) is False


def test_unrouted_card_keeps_the_warning():
    assert capacity_bound([DispatchResult(skipped_unassigned=["t_a"])]) is False


def test_idle_tick_is_not_saturation():
    # Nothing was held back at all: no ready work, no spawn.
    assert capacity_bound([DispatchResult()]) is False
    assert capacity_bound([None]) is False
    assert capacity_bound([]) is False


def test_host_cap_hold_is_saturation_not_stall():
    """A tick stopped by the host cap returns with every bucket empty."""
    res = DispatchResult(budget_hold="max_in_progress")
    assert capacity_bound([res]) is True
    assert "budget_hold:max_in_progress=1" in describe_suppression([res])


def test_board_cap_hold_is_saturation_not_stall():
    assert capacity_bound([DispatchResult(budget_hold="max_spawn")]) is True


def test_host_cap_hold_survives_a_reclaim_in_the_same_tick():
    """The cap gate runs after the reclaim phase, so one tick can carry both.

    Live 22.11: 1 of 22 saturated ticks also requeued a crashed worker
    (``crashed=[...]``) and re-armed the "check profile health" warning even
    though the cap — not a broken profile — was why nothing spawned. A
    ``budget_hold`` means the lane loops never ran; the reclaim buckets are
    lifecycle bookkeeping, not evidence about the host.
    """
    res = DispatchResult(budget_hold="max_in_progress", crashed=["t_dead"])
    assert capacity_bound([res]) is True
    assert "budget_hold:max_in_progress=1" in describe_suppression([res])


def test_cap_hold_does_not_mask_an_operator_actionable_hold():
    # A broken environment / quota wall still warns, cap or no cap.
    assert capacity_bound([DispatchResult(budget_hold="max_in_progress",
                                          skipped_env_unready=[("t_b", "no deps")])]) is False
    assert capacity_bound([DispatchResult(budget_hold="max_in_progress",
                                          rate_limited=["t_c"])]) is False


def test_suppression_line_names_the_profile_cap():
    res = DispatchResult(skipped_per_profile_capped=[("t_a", "02-hermes-platform", 3)])
    assert "per_profile_capped=1" in describe_suppression([res])
