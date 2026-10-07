"""AIMD-adaptive host-level worker cap for the kanban dispatcher (M2).

Tests :mod:`hermes_cli.kanban_adaptive` (the additive-increase /
multiplicative-decrease automaton running on top of the K1 cross-platform
memory sampler) and its integration seam in
``hermes_cli.kanban_db.dispatch_once``.

Accepted rules (task t_4b7d10af):

* growth: +1 to cap per 3 CONSECUTIVE calm ticks (ok pressure AND a ready
  queue exists AND past the 180s post-spawn stabilization AND not inside the
  600s decrease cooldown), ceiling = min(base + 4, 12).
* NO growth without a ready queue.
* multiplicative-decrease: cap x0.5 (floor 4) under elevated pressure.
* critical pressure -> spawns 0 (integration) and never counts as calm.
* 600s cooldown after a decrease blocks growth.
* 180s stabilization after the last spawn: those ticks don't count as calm.
* idle decay: once the queue has been empty under ok pressure for >= 30
  minutes, cap decays by 1 per tick toward base (never below base).
* broken/corrupt persisted state -> rebuilt fresh from base.
* adaptive=false -> dispatch_once behaves bit-for-bit as before (fail-open,
  no state file, static max_in_progress honoured exactly).

The sysinfo suite's shared conftest pins ``kanban_db._system_memory_sample``
to ``{}`` by default; tests that need a concrete pressure level patch that
seam (the K1 sampler + ``_memory_pressure_level`` classify from it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

from hermes_cli import kanban_adaptive


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same shape as the
    memory-guard suite's fixture)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


GIB = 1024 * 1024  # KiB per GiB


def _pressure_sample(level: str) -> dict:
    total = 1 * GIB
    if level == "critical":
        return {"mem_available_kib": 32 * 1024, "mem_total_kib": total}
    if level == "elevated":
        # 150 MiB of 1 GiB ~= 14.6% — elevated (not critical) under the
        # unified 20%/10% thresholds.
        return {"mem_available_kib": 150 * 1024, "mem_total_kib": total}
    return {"mem_available_kib": total // 2, "mem_total_kib": total}


@pytest.fixture
def ctl(tmp_path):
    """A controller seeded fresh at base=8 on an isolated state file."""
    path = tmp_path / "adaptive_state.json"
    return kanban_adaptive.AdaptiveController.from_disk(8, path=path)


@pytest.fixture
def bypass_model_route(monkeypatch):
    """Reach the dispatch spawn path under the sandboxed test config.

    The sandboxed HERMES_HOME has no provider/model route, so the runtime
    MODEL_LOCK gate in ``resolve_task_root_route`` would block every ready
    task with kind="capability" before it reaches the spawn loop (a
    pre-existing suite constraint). Dispatch tests unrelated to model
    routing bypass it exactly the way ``test_kanban_db`` does.
    """
    monkeypatch.setattr(
        kb, "resolve_task_root_route",
        lambda conn, tid, persist=False, allow_unpinned=False: (
            (tid,), "prof", "prof-model"
        ),
    )
    monkeypatch.setattr(kb, "check_respawn_guard", lambda conn, tid: None)


# --------------------------------------------------------------------------
# Growth: +1 per 3 consecutive calm ticks, ceiling = min(base + 4, 12)
# --------------------------------------------------------------------------


def test_growth_after_three_consecutive_calm_ticks(ctl):
    caps = []
    t = 1000.0
    for _ in range(4):
        caps.append(ctl.advance(8, "ok", True, now=t, min_advance_interval=0))
        t += 60.0
    # Ticks 1..2 are calm but not enough; the 3rd calm tick grows to 9.
    assert caps == [8, 8, 9, 9]


def test_growth_ceils_at_twelve_for_base_eight(ctl):
    t = 5000.0
    for _ in range(20):
        ctl.advance(8, "ok", True, now=t, min_advance_interval=0)
        t += 60.0
    assert ctl.state.cap == 12
    assert ctl.state.cap == min(8 + kanban_adaptive.CAP_CEILING_EXTRA, 12)


def test_growth_ceiling_scales_with_smaller_base(tmp_path):
    c = kanban_adaptive.AdaptiveController.from_disk(
        3, path=tmp_path / "adaptive_state.json"
    )
    t = 5000.0
    for _ in range(20):
        c.advance(3, "ok", True, now=t, min_advance_interval=0)
        t += 60.0
    # base 3 -> ceiling min(3 + 4, 12) = 7, never 12.
    assert c.state.cap == 7


# --------------------------------------------------------------------------
# No growth without a ready queue
# --------------------------------------------------------------------------


def test_no_growth_without_ready_queue(ctl):
    t = 1000.0
    for _ in range(10):
        ctl.advance(8, "ok", False, now=t, min_advance_interval=0)
        t += 60.0
    assert ctl.state.cap == 8
    assert ctl.state.streak == 0


# --------------------------------------------------------------------------
# Multiplicative decrease under elevated pressure, floor 4
# --------------------------------------------------------------------------


def test_elevated_pressure_halves_cap_with_floor_four(ctl):
    t = 1000.0
    for _ in range(13):  # grow to ceiling 12 first
        ctl.advance(8, "ok", True, now=t, min_advance_interval=0)
        t += 60.0
    assert ctl.state.cap == 12

    cap = ctl.advance(8, "elevated", True, now=t, min_advance_interval=0)
    t += 60.0
    assert cap == 6
    cap = ctl.advance(8, "elevated", True, now=t, min_advance_interval=0)
    t += 60.0
    assert cap == 4  # 6 // 2 = 3 -> clamped to floor 4
    cap = ctl.advance(8, "elevated", True, now=t, min_advance_interval=0)
    assert cap == 4


def test_decrease_records_last_decrease_and_streak_reset(ctl):
    ctl.state.cap = 12
    ctl.state.streak = 3
    t = 2000.0
    ctl.advance(8, "elevated", True, now=t, min_advance_interval=0)
    assert ctl.state.cap == 6
    assert ctl.state.streak == 0
    assert ctl.state.last_decrease_ts == t
    assert ctl.state.cooldown_until == t + kanban_adaptive.DECREASE_COOLDOWN_SECONDS


# --------------------------------------------------------------------------
# 600s cooldown blocks growth after a decrease
# --------------------------------------------------------------------------


def test_cooldown_blocks_growth_after_decrease(ctl):
    t0 = 5000.0
    cap = ctl.advance(8, "elevated", True, now=t0, min_advance_interval=0)
    assert cap == 4
    assert ctl.state.cooldown_until == t0 + 600

    # Three calm ticks INSIDE the cooldown must not grow (and must not even
    # accumulate streak).
    for k in range(1, 4):
        cap = ctl.advance(
            8, "ok", True, now=t0 + 60.0 * k, min_advance_interval=0
        )
        assert cap == 4
        assert ctl.state.streak == 0

    # Once cooldown expires, three calm ticks grow again.
    t = t0 + 660.0
    cap = ctl.advance(8, "ok", True, now=t, min_advance_interval=0)
    t += 60.0
    cap = ctl.advance(8, "ok", True, now=t, min_advance_interval=0)
    t += 60.0
    assert ctl.state.streak == 2
    cap = ctl.advance(8, "ok", True, now=t, min_advance_interval=0)
    assert cap == 5


# --------------------------------------------------------------------------
# 180s post-spawn stabilization window
# --------------------------------------------------------------------------


def test_stabilization_window_blocks_calm_after_spawn(ctl):
    t = 1000.0
    ctl.advance(8, "ok", True, now=t, min_advance_interval=0)  # calm, streak 1
    ctl.note_spawned(now=t)
    assert ctl.state.last_spawn_ts == t

    # 60s after spawn: still stabilizing -> the tick is not calm, streak reset.
    cap = ctl.advance(8, "ok", True, now=t + 60.0, min_advance_interval=0)
    assert cap == 8
    assert ctl.state.streak == 0

    # 3 calm ticks starting at 180s after the spawn (stabilization over).
    caps = []
    now = t + 180.0
    for _ in range(3):
        caps.append(ctl.advance(8, "ok", True, now=now, min_advance_interval=0))
        now += 60.0
    assert caps == [8, 8, 9]


# --------------------------------------------------------------------------
# Idle decay to base after the queue has been empty >= 30 minutes
# --------------------------------------------------------------------------


def test_idle_decay_to_base_after_30min_empty_queue(ctl):
    t = 1000.0
    for _ in range(13):  # grow to ceiling 12
        ctl.advance(8, "ok", True, now=t, min_advance_interval=0)
        t += 60.0
    assert ctl.state.cap == 12

    empty_ticks = []
    for _ in range(40):  # 60s per tick, empty queue, ok pressure
        empty_ticks.append(
            ctl.advance(8, "ok", False, now=t, min_advance_interval=0)
        )
        t += 60.0

    # First 30+ minutes: no decay (28 subsequent empty ticks before 1800s).
    assert all(c == 12 for c in empty_ticks[1:30])
    # empty_since was pinned on the first empty tick (index 0 at t=1720+60).
    assert empty_ticks[0] == 12
    # At ~1800s after empty_since the cap starts decaying by 1 per tick...
    assert empty_ticks[30] == 11
    # ...down to base 8 and never below.
    assert empty_ticks[33] == 8
    assert all(c == 8 for c in empty_ticks[34:])
    assert min(empty_ticks) == 8


def test_no_idle_decay_while_queue_active(ctl):
    ctl.state.cap = 12
    t = 5000.0
    for _ in range(5):
        cap = ctl.advance(8, "ok", True, now=t, min_advance_interval=0)
        t += 60.0
    assert cap == 12  # a queue is present -> no decay, only growth potential


# --------------------------------------------------------------------------
# Broken / corrupt persisted state -> rebuilt from base
# --------------------------------------------------------------------------


def test_broken_json_rebuilds_from_base(tmp_path):
    path = tmp_path / "adaptive_state.json"
    path.write_text("{not valid json", encoding="utf-8")
    c = kanban_adaptive.AdaptiveController.from_disk(8, path=path)
    assert c.state.cap == 8
    assert c.state.streak == 0


def test_invalid_state_dict_rebuilds_from_base(tmp_path):
    path = tmp_path / "adaptive_state.json"
    path.write_text(
        json.dumps({"cap": "banana", "streak": 1}), encoding="utf-8"
    )
    c = kanban_adaptive.AdaptiveController.from_disk(8, path=path)
    assert c.state.cap == 8


def test_out_of_band_cap_rebuilds_from_base(tmp_path):
    # A persisted cap above the global 12 is impossible for the automaton
    # to have produced -> treat as corrupt, rebuild from base.
    path = tmp_path / "adaptive_state.json"
    path.write_text(json.dumps({"cap": 5000}), encoding="utf-8")
    c = kanban_adaptive.AdaptiveController.from_disk(8, path=path)
    assert c.state.cap == 8


# --------------------------------------------------------------------------
# State persistence (round-trip + atomic write)
# --------------------------------------------------------------------------


def test_state_roundtrip_persists_and_reloads(tmp_path):
    path = tmp_path / "adaptive_state.json"
    c = kanban_adaptive.AdaptiveController.from_disk(8, path=path)
    c.advance(8, "ok", True, now=1000.0, min_advance_interval=0)
    c.advance(8, "ok", True, now=1060.0, min_advance_interval=0)
    c.save_dirty()
    assert path.exists()

    c2 = kanban_adaptive.AdaptiveController.from_disk(8, path=path)
    assert c2.state.streak == 2
    assert c2.state.cap == 8
    # last_advance survives so a restart cannot double-advance inside an
    # interval.
    assert c2.state.last_advance_ts == 1060.0


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "adaptive_state.json"
    c = kanban_adaptive.AdaptiveController.from_disk(8, path=path)
    c.advance(8, "ok", True, now=1000.0, min_advance_interval=0)
    c.save_dirty()
    assert not path.with_name(path.name + ".tmp").exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["cap"] == 8
    assert raw["version"] == kanban_adaptive.STATE_VERSION


# --------------------------------------------------------------------------
# Module wiring: tick_for_dispatch / note_spawned (+ persistence, throttle)
# --------------------------------------------------------------------------


def test_adaptive_state_path_lives_next_to_kanban_home(kanban_home):
    assert kanban_adaptive.adaptive_state_path() == (
        kanban_home / "kanban" / "adaptive_state.json"
    )


def test_tick_for_dispatch_grows_and_persists(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb, "_system_memory_sample", lambda: _pressure_sample("ok")
    )
    monkeypatch.setattr(kanban_adaptive, "adaptive_enabled", lambda: True)
    kanban_adaptive._reset_for_tests()
    try:
        caps = []
        t = 1000.0
        for _ in range(4):
            caps.append(
                kanban_adaptive.tick_for_dispatch(
                    base=8, queue_exists=True, now=t, min_advance_interval=0
                )
            )
            t += 60.0
        assert caps == [8, 8, 9, 9]
        assert kanban_adaptive.adaptive_state_path().exists()
    finally:
        kanban_adaptive._reset_for_tests()


def test_unknown_pressure_never_grows(ctl):
    t = 1000.0
    for _ in range(6):
        ctl.advance(8, "unknown", True, now=t, min_advance_interval=0)
        t += 60.0
    assert ctl.state.cap == 8
    assert ctl.state.streak == 0


# --------------------------------------------------------------------------
# Dispatch integration
# --------------------------------------------------------------------------


def test_adaptive_disabled_dispatch_is_bit_for_bit_unchanged(
    kanban_home, all_assignees_spawnable, bypass_model_route,
):
    """adaptive is OFF by default: static max_in_progress is honoured exactly,
    no state file is created, and the adaptive module leaves no trace."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        kb.create_task(conn, title="ready1", assignee="alice")
        kb.create_task(conn, title="ready2", assignee="bob")
        kb.create_task(conn, title="ready3", assignee="carol")
        running = kb.create_task(conn, title="running", assignee="dave")
        kb.claim_task(conn, running)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=3)

    # 1 running + cap 3 -> exactly 2 new spawns, the 3rd ready task waits.
    assert len(spawns) == 2
    assert len(res.spawned) == 2
    assert not kanban_adaptive.adaptive_state_path().exists()


def test_adaptive_critical_pressure_spawns_nothing(
    kanban_home, all_assignees_spawnable, bypass_model_route, monkeypatch,
):
    """With adaptive enabled, critical pressure => 0 spawns (the existing
    memory-pressure guard) and the automaton never counts the tick as calm."""
    monkeypatch.setattr(
        kb, "_system_memory_sample", lambda: _pressure_sample("critical")
    )
    monkeypatch.setattr(kanban_adaptive, "adaptive_enabled", lambda: True)
    kanban_adaptive._reset_for_tests()
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    try:
        with kb.connect() as conn:
            kb.create_task(conn, title="a", assignee="alice")
            kb.create_task(conn, title="b", assignee="bob")
            res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=8)

        assert not spawns
        assert not res.spawned
        assert res.memory_pressure == "critical"
        # The tick was critical -> no calm accumulation, cap stays at base.
        assert kanban_adaptive.adaptive_state_path().exists()
        raw = json.loads(
            kanban_adaptive.adaptive_state_path().read_text(encoding="utf-8")
        )
        assert raw["cap"] == 8
        assert raw["streak"] == 0
    finally:
        kanban_adaptive._reset_for_tests()


# --------------------------------------------------------------------------
# _adaptive_effective_cap seam (fail-open contract)
# --------------------------------------------------------------------------


def test_effective_cap_disabled_returns_static_base(kanban_home):
    with kb.connect() as conn:
        mod, eff = kb._adaptive_effective_cap(8, conn)
    assert eff == 8
    assert mod is None


def test_effective_cap_none_base_is_inert(kanban_home):
    with kb.connect() as conn:
        mod, eff = kb._adaptive_effective_cap(None, conn)
    assert eff is None
    assert mod is None


def test_effective_cap_enabled_advances(
    kanban_home, all_assignees_spawnable, bypass_model_route, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_system_memory_sample", lambda: _pressure_sample("ok")
    )
    monkeypatch.setattr(kanban_adaptive, "adaptive_enabled", lambda: True)
    kanban_adaptive._reset_for_tests()
    try:
        with kb.connect() as conn:
            kb.create_task(conn, title="a", assignee="alice")
            caps = []
            for t in (1000.0, 1060.0, 1120.0):
                mod, eff = kb._adaptive_effective_cap(8, conn, now=t)
                assert mod is not None
                caps.append(eff)
            assert caps == [8, 8, 9]  # 3rd calm tick (queue present, ok pressure)
    finally:
        kanban_adaptive._reset_for_tests()


def test_effective_cap_fails_open_when_automaton_raises(kanban_home, monkeypatch):
    monkeypatch.setattr(kanban_adaptive, "adaptive_enabled", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("sampler exploded")

    monkeypatch.setattr(kanban_adaptive, "tick_for_dispatch", boom)
    with kb.connect() as conn:
        mod, eff = kb._adaptive_effective_cap(8, conn)
    assert eff == 8
    assert mod is None
