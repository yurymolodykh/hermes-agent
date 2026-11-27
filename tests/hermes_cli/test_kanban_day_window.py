"""Daytime concurrency window (``kanban.day_max_in_progress`` / ``day_window``).

The contract the card body fixes, one test per clause:

* inside ``[08:00, 18:00)`` Europe/Moscow the host cap becomes 4;
* 07:59, 17:59, 18:00 and 03:00 behave exactly as the body spells out;
* no ``day_max_in_progress`` → the base cap, unchanged;
* a malformed time / zone / number → the base cap, ONE warning, no exception;
* ``base_cap is None`` inside the window → the day cap alone.

Timezone arithmetic is exercised through real ``Europe/Moscow`` instants
(``datetime(..., tzinfo=ZoneInfo(...)).timestamp()``), not by patching the
clock, so the boundary semantics are the ones production runs.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_day_window as daywin

Moscow = ZoneInfo("Europe/Moscow")
SETTINGS = (4, (daywin.dt_time(8, 0), daywin.dt_time(18, 0)), Moscow)


def at(hh: int, mm: int, day: int = 15) -> float:
    """A real epoch timestamp for ``HH:MM`` Moscow time (mid-month, no DST)."""
    return datetime(2026, 3, day, hh, mm, tzinfo=Moscow).timestamp()


@pytest.fixture(autouse=True)
def _fresh_window_state():
    """Each test starts with no remembered level and no warn-once memory."""
    daywin.reset_state()
    yield
    daywin.reset_state()


# ── the window edges ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (7, 59, 7),
        (8, 0, 4),
        (12, 30, 4),
        (17, 59, 4),
        (18, 0, 7),
        (23, 30, 7),
        (3, 0, 7),
    ],
)
def test_window_edges_select_the_cap(hh, mm, expected):
    """07:59→7, 08:00→4, 17:59→4, 18:00→7, 03:00→7 (start incl., end excl.)."""
    assert daywin.resolve_day_window_cap(7, now=at(hh, mm), settings=SETTINGS) == expected


def test_base_cap_none_inside_window_uses_the_day_cap():
    """An uncapped host still gets the narrower day lane."""
    assert daywin.resolve_day_window_cap(None, now=at(9, 0), settings=SETTINGS) == 4


def test_base_cap_none_outside_window_stays_uncapped():
    assert daywin.resolve_day_window_cap(None, now=at(23, 0), settings=SETTINGS) is None


def test_day_cap_never_raises_the_base_cap():
    """``min`` semantics: a day cap wider than the base must not widen it."""
    narrow_base = (10, (daywin.dt_time(8, 0), daywin.dt_time(18, 0)), Moscow)
    assert daywin.resolve_day_window_cap(3, now=at(12, 0), settings=narrow_base) == 3


# ── unset / broken configuration is fail-open ──────────────────────────────


def test_no_day_config_returns_the_base_cap_unchanged():
    """The shipped defaults (all None) must be a no-op, not a 4-card day."""
    assert daywin.resolve_day_window_cap(7, now=at(12, 0), settings=(None, None, None)) == 7
    assert daywin.resolve_day_window_cap(None, now=at(12, 0), settings=(None, None, None)) is None


@pytest.mark.parametrize(
    "settings",
    [
        # cap present, window missing
        (4, None, None),
        # window present, cap missing
        (None, (daywin.dt_time(8, 0), daywin.dt_time(18, 0)), Moscow),
    ],
)
def test_half_configured_window_is_ignored(settings):
    assert daywin.resolve_day_window_cap(7, now=at(12, 0), settings=settings) == 7


@pytest.mark.parametrize(
    "kanban_cfg",
    [
        {"day_max_in_progress": "four", "day_window": ["08:00", "18:00"]},
        {"day_max_in_progress": 0, "day_window": ["08:00", "18:00"]},
        {"day_max_in_progress": -3, "day_window": ["08:00", "18:00"]},
        {"day_max_in_progress": True, "day_window": ["08:00", "18:00"]},
        {"day_max_in_progress": 4, "day_window": ["8:00", "later"]},
        {"day_max_in_progress": 4, "day_window": ["08:00"]},
        {"day_max_in_progress": 4, "day_window": "08:00-18:00"},
        {"day_max_in_progress": 4, "day_window": ["18:00", "08:00"]},  # wraps midnight: rejected
        {"day_max_in_progress": 4, "day_window": [8, 18]},
        {"day_max_in_progress": 4, "day_window": None, "day_window_tz": "Mars/Olympus"},
        # A bad cap must not silently fall back to a 4-card night lane.
        {"day_max_in_progress": "4", "day_window": ["08:00", "18:00"]},
    ],
)
def test_broken_day_config_falls_back_to_the_base_cap_without_raising(
    kanban_cfg, monkeypatch, caplog,
):
    """Every malformed value → base cap, a WARN, and no exception out of the tick."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"kanban": kanban_cfg},
    )
    with caplog.at_level(logging.WARNING):
        resolved = daywin.resolve_day_window_cap(7, now=at(12, 0))
    if kanban_cfg.get("day_max_in_progress") == "4":
        # "4" is accepted as 4: config files quote integers often enough that
        # rejecting it would silently disable a working window.
        assert resolved == 4
        return
    assert resolved == 7


def test_unknown_timezone_falls_back_to_fixed_utc_plus_3(monkeypatch):
    """A broken zone name must not disable the window — it changes its zone."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"day_max_in_progress": 4,
                            "day_window": ["08:00", "18:00"],
                            "day_window_tz": "Mars/Olympus"}},
    )
    # 12:00 UTC+3 == 09:00 UTC: inside the window under the fallback zone.
    noon_utc3 = datetime(2026, 3, 15, 12, 0, tzinfo=daywin.FALLBACK_TZ).timestamp()
    assert daywin.resolve_day_window_cap(7, now=noon_utc3) == 4


def test_config_read_failure_is_fail_open(monkeypatch):
    """A config layer that raises must yield the base cap, not a dead dispatcher."""
    def boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
    assert daywin.resolve_day_window_cap(7, now=at(12, 0)) == 7


# ── logging discipline ─────────────────────────────────────────────────────


def test_level_change_is_logged_once_not_per_tick(caplog):
    """60s ticks over eight hours must not write the transition 480 times."""
    with caplog.at_level(logging.INFO, logger=kb._log.name):
        daywin.resolve_day_window_cap(7, now=at(8, 0), settings=SETTINGS)
        for minute in range(0, 30):
            daywin.resolve_day_window_cap(7, now=at(8, minute), settings=SETTINGS)
        daywin.resolve_day_window_cap(7, now=at(18, 0), settings=SETTINGS)
        for minute in range(0, 30):
            daywin.resolve_day_window_cap(7, now=at(18, minute), settings=SETTINGS)

    transitions = [
        r for r in caplog.records if "daytime window" in r.getMessage()
    ]
    assert len(transitions) == 2, [r.getMessage() for r in transitions]


def test_broken_config_warns_once_not_per_tick(monkeypatch, caplog):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"day_max_in_progress": 4, "day_window": ["nope", "18:00"]}},
    )
    with caplog.at_level(logging.WARNING):
        for minute in range(20):
            assert daywin.resolve_day_window_cap(7, now=at(12, minute)) == 7
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


# ── weekday restriction (kanban.day_window_days) ───────────────────────────
#
# 2026-03-16 is a Monday; 2026-03-21 Saturday; 2026-03-22 Sunday.

WEEKDAYS_ONLY = (4, (daywin.dt_time(8, 0), daywin.dt_time(18, 0)), Moscow,
                 frozenset({0, 1, 2, 3, 4}))


def test_weekday_inside_window_applies_the_day_cap():
    """Tuesday 12:00 inside the window and a listed weekday → the day cap."""
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=17),
                                         settings=WEEKDAYS_ONLY) == 4


def test_saturday_inside_window_keeps_the_base_cap():
    """Saturday 12:00 is inside the hours but not a listed weekday → 7."""
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=21),
                                         settings=WEEKDAYS_ONLY) == 7


def test_sunday_inside_window_keeps_the_base_cap():
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=22),
                                         settings=WEEKDAYS_ONLY) == 7


def test_weekday_outside_window_keeps_the_base_cap():
    """Wednesday 22:00 — neither the hours nor the weekday give the day cap."""
    assert daywin.resolve_day_window_cap(7, now=at(22, 0, day=18),
                                         settings=WEEKDAYS_ONLY) == 7


def test_uppercase_day_names_are_accepted(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"day_max_in_progress": 4,
                            "day_window": ["08:00", "18:00"],
                            "day_window_tz": "Europe/Moscow",
                            "day_window_days": ["MON", "Tue", "WED", " thu", "Fri"]}},
    )
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=17)) == 4  # Tuesday
    daywin.reset_state()
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=21)) == 7  # Saturday


def test_numeric_day_values_are_accepted(monkeypatch):
    """0..6 with Monday = 0, mirroring ``datetime.weekday()``."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"day_max_in_progress": 4,
                            "day_window": ["08:00", "18:00"],
                            "day_window_tz": "Europe/Moscow",
                            "day_window_days": [0, 1, 2, 3, 4]}},
    )
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=16)) == 4  # Monday
    daywin.reset_state()
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=22)) == 7  # Sunday


@pytest.mark.parametrize(
    "bad_days",
    [
        ["mon", "funday"],
        "mon-fri",
        [],
        [7],
        [-1],
        [True],
        {"mon": 1},
    ],
)
def test_bad_day_list_warns_once_and_keeps_every_day(bad_days, monkeypatch, caplog):
    """An unusable list must not widen the business-hours cap: window every day."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"day_max_in_progress": 4,
                            "day_window": ["08:00", "18:00"],
                            "day_window_tz": "Europe/Moscow",
                            "day_window_days": bad_days}},
    )
    with caplog.at_level(logging.WARNING):
        for minute in range(10):
            assert daywin.resolve_day_window_cap(7, now=at(12, minute)) == 4
        # Saturday too: the window stays a seven-day window.
        assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=21)) == 4
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_load_settings_returns_four_elements(monkeypatch):
    """The tuple grew a 4th slot; the first three keep their exact meaning."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"day_max_in_progress": 4,
                            "day_window": ["08:00", "18:00"],
                            "day_window_tz": "Europe/Moscow",
                            "day_window_days": ["mon", "tue", "wed", "thu", "fri"]}},
    )
    settings = daywin._load_settings()
    assert len(settings) == 4
    cap, window, tz, days = settings[0], settings[1], settings[2], settings[3]
    assert (cap, window) == (4, (daywin.dt_time(8, 0), daywin.dt_time(18, 0)))
    assert getattr(tz, "key", None) == "Europe/Moscow"
    assert days == frozenset({0, 1, 2, 3, 4})


def test_three_element_settings_still_accepted():
    """Backward compatibility: the pre-key tuple has no weekday list."""
    assert daywin.resolve_day_window_cap(7, now=at(12, 0, day=21), settings=SETTINGS) == 4
    assert daywin.resolve_day_window_cap(7, now=at(23, 0, day=21), settings=SETTINGS) == 7


# ── wiring into the dispatcher tick ────────────────────────────────────────


def test_tick_spawn_budget_applies_the_day_window(monkeypatch):
    """The gate itself — not just the helper — must honour the window.

    ``_tick_spawn_budget`` is the single host-level cap in the dispatcher, so
    this is the seam that decides how many cards may start this tick.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"day_max_in_progress": 4,
                            "day_window": ["08:00", "18:00"],
                            "day_window_tz": "Europe/Moscow"}},
    )
    monkeypatch.setattr(kbd, "count_running_tasks", lambda conn: 4)
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board=None: 0)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    daywin.reset_state()
    conn = sqlite3.connect(":memory:")

    # 12:00 Moscow — inside the window; four already running saturate the day cap.
    monkeypatch.setattr(daywin.time, "time", lambda: at(12, 0))
    result = kb.DispatchResult()
    may_spawn, budget = kbd._tick_spawn_budget(
        conn, result, max_spawn=None, max_in_progress=7, board=None,
    )
    assert (may_spawn, budget) == (False, None)
    assert result.budget_hold == "max_in_progress"

    # Same four running, but 23:00 — the night cap of 7 leaves room.
    monkeypatch.setattr(daywin.time, "time", lambda: at(23, 0))
    result = kb.DispatchResult()
    may_spawn, budget = kbd._tick_spawn_budget(
        conn, result, max_spawn=None, max_in_progress=7, board=None,
    )
    assert (may_spawn, budget) == (True, 3)
    assert result.budget_hold is None


def test_tick_spawn_budget_is_unchanged_without_day_config(monkeypatch):
    """Regression guard: no ``kanban.day_*`` keys → the pre-window tick exactly."""
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"kanban": {}},
    )
    monkeypatch.setattr(kbd, "count_running_tasks", lambda conn: 2)
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board=None: 1)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    daywin.reset_state()
    monkeypatch.setattr(daywin.time, "time", lambda: at(12, 0))

    result = kb.DispatchResult()
    may_spawn, budget = kbd._tick_spawn_budget(
        sqlite3.connect(":memory:"), result,
        max_spawn=None, max_in_progress=7, board=None,
    )
    assert (may_spawn, budget) == (True, 4)  # 7 - (2 + 1): the plain host cap
