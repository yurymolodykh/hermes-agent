"""Weighted slots for goal-mode controllers in the kanban dispatcher (M3).

Tests :mod:`hermes_cli.kanban_db`'s ``kanban.weighted_slots`` feature
(task t_2eb04816): with the flag ON, occupied worker slots are a WEIGHTED sum
(normal card = 1.0, ``goal_mode=1`` controller = 0.5) instead of a plain card
count, for both the ``max_in_progress`` host cap and the daytime worker
reserve. A goal-mode card ALWAYS counts as *background* for the daytime
reserve regardless of its priority (closes the prior bypass where a
goal-mode controller pinned to priority 20 escaped the reserve). The reserve
limit must consume the M2 adaptive effective cap (``bg_limit =
effective_max - reserve_slots``).

Accepted rules / acceptance criteria (task t_2eb04816):

* weighted count: 2 normal + 2 goal-mode controllers = 2*1.0 + 2*0.5 = 3.0
  occupied slots.
* goal-mode controller with priority 20 during the reserve window counts as
  background and is deferred (does not bypass the reserve).
* ``kanban.weighted_slots`` defaults to FALSE -> dispatch is bit-for-bit
  identical to today (plain card counts everywhere, goal_mode never treated
  as background).
* combination with the K2 adaptive cap: reserve bg_limit =
  adaptive_effective_max - reserve_slots (not the static base).
* live shape: 8 running = 6*1.0 + 2*0.5 = 7.0 < cap 8 -> one free slot that
  a normal ready card can occupy immediately.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_cli import kanban_db as kb

# Synthetic assignees ("alice", "bob", "a", "b", "x", …) resolve to real
# profiles under the shared hermetic assignee registry, so create_task's
# profile-exists preflight and the dispatcher's spawnable gate both pass
# without touching the host's real profile list.
pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same shape as the
    memory-guard / adaptive suites' fixture)."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _patch_config(monkeypatch, kanban_cfg: dict):
    """Stub ``load_config_readonly`` so dispatcher/config reads see the given
    ``kanban`` block (weighted flag, reserve block, static cap…)."""
    import hermes_cli.config as hcfg

    monkeypatch.setattr(hcfg, "load_config_readonly", lambda: {"kanban": kanban_cfg})


def _patch_clock(monkeypatch, now):
    """Pin the daytime-reserve clock (MSK) to ``now``."""
    monkeypatch.setattr(kb, "_daytime_reserve_now", lambda tz: now)


@pytest.fixture
def bypass_model_route(monkeypatch):
    """Reach the dispatch spawn path under the sandboxed test config.

    Same hermetic pattern as the other dispatch suites: the runtime
    MODEL_LOCK gate in ``resolve_task_root_route`` and the respawn cooldown
    are unrelated to weighted slots, so stub them out.
    """
    monkeypatch.setattr(
        kb, "resolve_task_root_route",
        lambda conn, tid, persist=False, allow_unpinned=False: (
            (tid,), "prof", "prof-model"
        ),
    )
    monkeypatch.setattr(kb, "check_respawn_guard", lambda conn, tid: None)


RESERVE_BLOCK = {
    "daytime_reserve_slots": 2,
    "daytime_reserve_window": ["08:00", "20:00"],
    "daytime_reserve_tz": "Europe/Moscow",
    "daytime_reserve_priority_below": 0,
}


# --------------------------------------------------------------------------
# weighted_slots_enabled() config gate
# --------------------------------------------------------------------------


def test_weighted_slots_enabled_defaults_off(monkeypatch):
    """Flag is OFF by default and OPTS IN via ``kanban.weighted_slots: true``."""
    _patch_config(monkeypatch, {})
    assert kb.weighted_slots_enabled() is False

    _patch_config(monkeypatch, {"weighted_slots": False})
    assert kb.weighted_slots_enabled() is False

    _patch_config(monkeypatch, {"weighted_slots": True})
    assert kb.weighted_slots_enabled() is True


# --------------------------------------------------------------------------
# Weighted occupied-slot count (max_in_progress accounting)
# --------------------------------------------------------------------------


def test_count_running_slots_weights_goal_mode(kanban_home):
    """2 normal + 2 goal-mode controllers occupy 2*1.0 + 2*0.5 = 3.0 slots."""
    with kb.connect() as conn:
        for i, goal in [(0, False), (1, False), (2, True), (3, True)]:
            t = kb.create_task(
                conn, title=f"run-{i}", assignee="alice", goal_mode=goal,
            )
            kb.claim_task(conn, t)

        assert kb.count_running_slots(conn, weighted=True) == 3.0
        # Flag off -> the plain card count (bit-identical view).
        assert kb.count_running_slots(conn, weighted=False) == 4


def test_weighted_slots_off_count_is_plain_card_count(kanban_home):
    """With the flag off, running goal-mode cards each occupy exactly 1 slot."""
    with kb.connect() as conn:
        for i in range(3):
            t = kb.create_task(conn, title=f"g-{i}", assignee="a", goal_mode=True)
            kb.claim_task(conn, t)
        assert kb.count_running_slots(conn, weighted=False) == 3


# --------------------------------------------------------------------------
# Dispatch integration: weighted max_in_progress frees a slot for a normal card
# --------------------------------------------------------------------------


def test_dispatch_weighted_8_running_frees_slot_for_normal_card(
    kanban_home, bypass_model_route, monkeypatch,
):
    """Live shape: 8 running (6 normal + 2 goal-mode) = 7.0 occupied slots.

    At cap 8 there is 1 free slot, so a normal ready card spawns immediately.
    Without weighted accounting the plain count (8 >= 8) would have returned
    early and spawned nothing.
    """
    _patch_config(monkeypatch, {"weighted_slots": True})
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for i in range(6):
            t = kb.create_task(conn, title=f"run-n{i}", assignee="a")
            kb.claim_task(conn, t)
        for i in range(2):
            t = kb.create_task(conn, title=f"run-g{i}", assignee="a", goal_mode=True)
            kb.claim_task(conn, t)
        # Sanity: the census is 8 running cards = 7.0 weighted slots.
        assert kb.count_running_slots(conn, weighted=True) == 7.0

        kb.create_task(conn, title="ready-normal", assignee="alice")
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    # dry_run never invokes spawn_fn; the would-be spawn is in res.spawned.
    assert spawns == []
    assert len(res.spawned) == 1


def test_dispatch_weighted_slots_off_returns_early_at_card_cap(
    kanban_home, bypass_model_route,
):
    """Bit-compat: flag OFF -> plain card counting, 8 running cards block a
    9th even though 2 are goal-mode controllers."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for i in range(6):
            t = kb.create_task(conn, title=f"run-n{i}", assignee="a")
            kb.claim_task(conn, t)
        for i in range(2):
            t = kb.create_task(conn, title=f"run-g{i}", assignee="a", goal_mode=True)
            kb.claim_task(conn, t)
        kb.create_task(conn, title="ready-normal", assignee="alice")
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    assert spawns == []
    assert res.spawned == []


# --------------------------------------------------------------------------
# Daytime reserve: goal-mode controllers always count as background
# --------------------------------------------------------------------------


def test_dispatch_goal_mode_priority_twenty_counts_background_in_window(
    kanban_home, bypass_model_route, monkeypatch,
):
    """A goal-mode controller pinned to priority 20 does NOT bypass the
    daytime reserve: it counts as background and is deferred when the
    background census is exhausted. A plain priority-20 card still uses the
    full pool."""
    cfg = {
        "weighted_slots": True,
        "max_in_progress": 8,
        **RESERVE_BLOCK,
    }
    _patch_config(monkeypatch, cfg)
    _patch_clock(
        monkeypatch,
        datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        # Fill the background census to the reserve ceiling first:
        # 8 slots - 2 reserved = 6 background slots, all occupied by running
        # background cards (priority -1).
        for i in range(6):
            t = kb.create_task(conn, title=f"bg-run{i}", assignee="a", priority=-1)
            kb.claim_task(conn, t)
        # Reserve counter starts at 6.0 (6 running background x 1.0) >= limit 6.
        assert kb.count_running_background_slots(
            conn, weighted=True, priority_below=0,
        ) == 6.0

        # A goal-mode controller at priority 20 (the old bypass path)…
        goal_id = kb.create_task(
            conn, title="goal-prio20", assignee="bob", priority=20, goal_mode=True,
        )
        # …and a plain priority-20 card as the control.
        normal_id = kb.create_task(
            conn, title="norm-prio20", assignee="x", priority=20,
        )

        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    # Control: the plain priority-20 card uses the full pool.
    assert [i for (i, _a, _w) in res.spawned] == [normal_id]
    # Goal-mode controller at priority 20 is treated as background and defers.
    assert goal_id in res.reserved_by_daytime


def test_dispatch_goal_mode_priority_twenty_off_keeps_current_behavior(
    kanban_home, bypass_model_route, monkeypatch,
):
    """Bit-compat with the flag OFF: goal-mode is NOT auto-background; a
    priority-20 controller spawns via the full pool exactly as today."""
    cfg = {"max_in_progress": 8, **RESERVE_BLOCK}
    _patch_config(monkeypatch, cfg)
    _patch_clock(
        monkeypatch,
        datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for i in range(6):
            t = kb.create_task(conn, title=f"bg-run{i}", assignee="a", priority=-1)
            kb.claim_task(conn, t)
        goal_id = kb.create_task(
            conn, title="goal-prio20", assignee="bob", priority=20, goal_mode=True,
        )
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    # Same as today: priority 20 >= threshold -> uses the full pool, spawns.
    assert [i for (i, _a, _w) in res.spawned] == [goal_id]
    assert res.reserved_by_daytime == []


def test_count_running_background_slots_goal_mode_or_priority(kanban_home, monkeypatch):
    """The reserve background census counts ``goal_mode OR priority<threshold``
    when the flag is on; with the flag off, only priority<threshold counts."""
    _patch_config(monkeypatch, {"weighted_slots": True})
    with kb.connect() as conn:
        t = kb.create_task(conn, title="bg-low", assignee="a", priority=-1)
        kb.claim_task(conn, t)
        t = kb.create_task(conn, title="bg-goal-hi", assignee="b", priority=20, goal_mode=True)
        kb.claim_task(conn, t)
        t = kb.create_task(conn, title="fg-hi", assignee="x", priority=20)
        kb.claim_task(conn, t)

        # On: 1.0 (priority<0) + 0.5 (goal-mode hi) = 1.5; the plain hi card is
        # foreground and excluded.
        assert kb.count_running_background_slots(
            conn, weighted=True, priority_below=0,
        ) == 1.5
        # Off: bit-compat — only priority<0 counts, goal_mode is ignored.
        assert kb.count_running_background_slots(
            conn, weighted=False, priority_below=0,
        ) == 1


# --------------------------------------------------------------------------
# Reserve limit consumes the M2 adaptive effective cap (K2 combination)
# --------------------------------------------------------------------------


def test_dispatch_reserve_bg_limit_uses_adaptive_effective_max(
    kanban_home, bypass_model_route, monkeypatch,
):
    """bg_limit = adaptive_effective_max - reserve_slots. The adaptive seam
    reports an effective cap of 6 (below the static base 8), so with 2
    reserved slots the background ceiling is 4 — NOT the static-base-derived 6.
    Six ready background cards -> 4 spawned, 2 deferred; a static-base bug
    would have spawned all 6."""
    monkeypatch.setattr(
        kb, "_adaptive_effective_cap",
        lambda base, conn, **kw: (object(), 6),
    )
    cfg = {
        "weighted_slots": True,
        "max_in_progress": 8,
        **RESERVE_BLOCK,
    }
    _patch_config(monkeypatch, cfg)
    _patch_clock(
        monkeypatch,
        datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        bg_ids = []
        for i in range(6):
            bg_ids.append(
                kb.create_task(conn, title=f"bg-{i}", assignee="alice", priority=-1)
            )
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    assert len(res.spawned) == 4
    assert len(res.reserved_by_daytime) == 2
    assert set(res.reserved_by_daytime) <= set(bg_ids)
