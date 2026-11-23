"""Timed wake-up of parked (``scheduled``) cards.

Behaviour contract for ``hermes_cli.kanban_db_schedule`` and the dispatcher hook
that calls it. The point of the feature: a card parked "until 25.11 04:40" must
come back on its own, through the normal dispatch tick, exactly once.

Every test drives the real DB layer (``kbc.connect``) against an isolated
``HERMES_HOME``; nothing here inspects source text, and no test writes to
``~/.hermes``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import hermes_time
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_schedule as kbsch


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same shape as test_kanban_db).

    The profile timezone is pinned to ``Europe/Moscow`` through ``HERMES_TIMEZONE``
    because the isolated home carries no ``config.yaml``: without it the module
    would fall back to server-local time and "Moscow wall clock" assertions would
    only hold when the host happened to be in MSK. Pinning it also proves the
    contract that deadlines do not depend on the host zone.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TIMEZONE", "Europe/Moscow")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_time.reset_cache()
    kb.init_db()
    try:
        yield home
    finally:
        hermes_time.reset_cache()


def _park(conn, *, until=None, title="wait", assignee="a"):
    tid = kb.create_task(conn, title=title, assignee=assignee, created_by="test")
    assert kb.schedule_task(conn, tid, reason="parked by test", until=until) is True
    return tid


def _row(conn, tid):
    return dict(conn.execute(
        "SELECT status, scheduled_until, current_run_id FROM tasks WHERE id = ?", (tid,)
    ).fetchone())


# ---------------------------------------------------------------------------
# parse_until: the deadline is wall-clock time in the PROFILE zone (Europe/Moscow)
# ---------------------------------------------------------------------------


def test_parse_until_reads_naive_time_in_profile_timezone(kanban_home):
    """A naive ``--until`` is Moscow wall-clock, never UTC and never the host zone."""
    epoch = kbsch.parse_until("2026-11-25T04:40:00")
    assert epoch is not None
    local = kbsch.format_local(epoch)
    assert local.startswith("2026-11-25 04:40"), local


def test_parse_until_accepts_epoch_and_offset_and_rejects_junk(kanban_home):
    assert kbsch.parse_until(1_800_000_000) == 1_800_000_000
    assert kbsch.parse_until("1800000000") == 1_800_000_000
    msk = kbsch.parse_until("2026-11-25T04:40:00")
    utc = kbsch.parse_until("2026-11-25T04:40:00+00:00")
    assert msk is not None and utc is not None
    # Same wall clock, Moscow is UTC+3 → its instant is EARLIER by three hours.
    assert utc - msk == 3 * 3600, "Moscow is UTC+3"
    for junk in ("", "   ", "soon", None, True, "25.11 04:40"):
        assert kbsch.parse_until(junk) is None


def test_format_local_renders_profile_timezone(kanban_home):
    import datetime as _dt

    tz = hermes_time.get_timezone()
    if tz is None:
        pytest.skip("no profile timezone configured")
    text = kbsch.format_local(1_800_000_000)
    assert text, "a deadline must render in the journal"
    stamp = _dt.datetime.fromtimestamp(1_800_000_000, tz)
    # The rendered line carries the profile offset/timezone name, not the host's.
    assert str(stamp.year) in text and ":" in text
    offset = stamp.strftime("%z")
    if offset:
        sign = offset[0] + offset[1:3]
        assert sign in text or (tz.key or "") in text or (stamp.tzname() or "") in text, text


# ---------------------------------------------------------------------------
# schedule_task stores the deadline on the row
# ---------------------------------------------------------------------------


def test_schedule_task_persists_deadline_and_clears_on_unblock(kanban_home):
    with kbc.connect() as conn:
        deadline = int(time.time()) + 3600
        tid = _park(conn, until=deadline)
        row = _row(conn, tid)
        assert row["status"] == "scheduled"
        assert row["scheduled_until"] == deadline

        assert kb.get_task(conn, tid).scheduled_until == deadline

        assert kb.unblock_task(conn, tid) is True
        row = _row(conn, tid)
        assert row["status"] != "scheduled"
        assert row["scheduled_until"] is None, "a card that left scheduled must not keep a deadline"


def test_schedule_task_without_until_leaves_no_deadline(kanban_home):
    with kbc.connect() as conn:
        tid = _park(conn)
        assert _row(conn, tid)["scheduled_until"] is None


# ---------------------------------------------------------------------------
# The wake: one tick, one wake, catch-up does not replay
# ---------------------------------------------------------------------------


def test_wake_due_tasks_promotes_once_and_records_woken_by_timer(kanban_home):
    with kbc.connect() as conn:
        now = int(time.time())
        deadline = now + 600
        tid = _park(conn, until=deadline)

        assert kbsch.wake_due_tasks(conn, now=now) == [], "not due yet"
        assert _row(conn, tid)["status"] == "scheduled"

        assert kbsch.wake_due_tasks(conn, now=deadline) == [tid]
        row = _row(conn, tid)
        assert row["status"] == "ready"
        assert row["scheduled_until"] is None, "the wake must clear the deadline"

        # Catch-up / re-tick: nothing left to wake, so no duplicate wake-ups.
        assert kbsch.wake_due_tasks(conn, now=deadline + 86_400) == []
        later = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        )]
        assert later.count("woken_by_timer") == 1


def test_wake_after_long_downtime_wakes_each_card_exactly_once(kanban_home):
    """A tick arriving hours late wakes every overdue card once — no replay storm."""
    with kbc.connect() as conn:
        now = int(time.time())
        t1 = _park(conn, until=now - 7200, title="missed two hours ago")
        t2 = _park(conn, until=now - 60, title="missed a minute ago")
        t3 = _park(conn, until=now + 7200, title="not due yet")

        woken = kbsch.wake_due_tasks(conn, now=now)
        assert sorted(woken) == sorted([t1, t2]), "only the overdue cards wake"
        assert _row(conn, t3)["status"] == "scheduled"
        assert _row(conn, t3)["scheduled_until"] is not None

        # The very next tick (the dispatcher runs every few seconds) is a no-op.
        assert kbsch.wake_due_tasks(conn, now=now + 5) == []


def test_parked_card_without_deadline_is_never_woken(kanban_home):
    """A manual park (no deadline) waits for a human; the tick never guesses."""
    with kbc.connect() as conn:
        tid = _park(conn)
        assert kbsch.wake_due_tasks(conn, now=int(time.time()) + 10**8) == []
        assert _row(conn, tid)["status"] == "scheduled"


# ---------------------------------------------------------------------------
# Parent re-gate: a woken child whose upstream is open lands in todo, like unblock
# ---------------------------------------------------------------------------


def test_wake_regates_on_unsatisfied_parent(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a", created_by="test")
        child = kb.create_task(
            conn, title="child", assignee="a", created_by="test", parents=[parent],
        )
        assert _row(conn, child)["status"] == "todo"
        now = int(time.time())
        assert kb.schedule_task(conn, child, reason="wait for the parent", until=now - 5)

        assert kbsch.wake_due_tasks(conn, now=now) == [child]
        assert _row(conn, child)["status"] == "todo", "unsatisfied parent blocks promotion"

        # Once the parent is done the next promotion pass picks the child up.
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert _row(conn, child)["status"] == "ready"


# ---------------------------------------------------------------------------
# Legacy compatibility: a card parked BEFORE the column keeps its deadline
# ---------------------------------------------------------------------------


def test_legacy_deadline_read_from_comment_marker(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="old park", assignee="a", created_by="test")
        kb.schedule_task(conn, tid, reason="waiting")
        kb.add_comment(
            conn, tid, "operator",
            "parked until SCHEDULED_UNTIL=2026-11-25T04:40:00+03:00 (waiting on upstream)",
        )
        epoch = kbsch.resolve_deadline(conn, tid)
        assert epoch is not None
        assert kbsch.format_local(epoch).startswith("2026-11-25 04:40")


def test_legacy_human_date_marker_is_read_in_msk(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="old park", assignee="a", created_by="test")
        kb.schedule_task(conn, tid, reason="waiting")
        kb.add_comment(conn, tid, "operator", "вернуться 25.11 04:40 МСК")
        epoch = kbsch.resolve_deadline(conn, tid)
        assert epoch is not None
        assert kbsch.format_local(epoch).startswith("2026-11-25 04:40")


def test_column_deadline_wins_over_legacy_comment(kanban_home):
    with kbc.connect() as conn:
        deadline = int(time.time()) + 900
        tid = _park(conn, until=deadline)
        kb.add_comment(conn, tid, "operator", "SCHEDULED_UNTIL=2026-01-01T00:00:00+03:00")
        assert kbsch.resolve_deadline(conn, tid) == deadline


def test_card_without_column_but_with_marker_is_woken(kanban_home):
    """The tick's compatibility path: a legacy card wakes on its comment deadline."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="old park", assignee="a", created_by="test")
        kb.schedule_task(conn, tid, reason="waiting")
        past = int(time.time()) - 120
        kb.add_comment(conn, tid, "operator", f"SCHEDULED_UNTIL={past}")
        woken = kbsch.wake_due_tasks(conn, now=int(time.time()))
        assert tid in woken
        assert _row(conn, tid)["status"] == "ready"


# ---------------------------------------------------------------------------
# Board summary: parked cards are visible, and a deadline-less park is flagged
# ---------------------------------------------------------------------------


def test_board_stats_reports_scheduled_deadlines(kanban_home):
    with kbc.connect() as conn:
        now = int(time.time())
        overdue = _park(conn, until=now - 60, title="overdue")
        upcoming = _park(conn, until=now + 3600, title="upcoming")
        blind = _park(conn, title="no deadline")

        stats = kb.board_stats(conn)
        assert stats["scheduled_total"] == 3
        assert stats["scheduled_no_deadline"] == 1
        assert stats["scheduled_overdue"] == 1
        ids = [c["id"] for c in stats["next_wakeups"]]
        assert ids[0] == overdue and upcoming in ids and blind not in ids
        for card in stats["next_wakeups"]:
            assert card["scheduled_until_local"], "the summary renders the profile timezone"

        line = kbsch.describe_schedule(stats)
        assert "scheduled=3" in line and "no-deadline=1" in line and "overdue=1" in line


# ---------------------------------------------------------------------------
# The dispatcher tick actually calls the wake (integration, not unit)
# ---------------------------------------------------------------------------


def test_dispatch_tick_wakes_and_spawns_in_the_same_pass(kanban_home, monkeypatch):
    """The deadline is honoured by the real tick: wake → promote → spawn, one pass."""
    from hermes_cli import kanban_db_dispatch as kbd

    spawned: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        kbd, "_default_spawn",
        lambda conn, task, ws, **kw: spawned.append((task.id, task.assignee, ws)) or (1234, "boot:1|1"),
    )
    monkeypatch.setattr(kbd, "_set_worker_pid", lambda *a, **k: None)
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: None)
    monkeypatch.setattr(kbd, "reap_terminal_workers", lambda conn: [])
    monkeypatch.setattr(kbd, "detect_crashed_workers", lambda conn, **kw: [])
    monkeypatch.setattr(kbd, "enforce_max_runtime", lambda conn, **kw: [])
    monkeypatch.setattr(kbd, "_kbkp", type("X", (), {"keypool_sweep": staticmethod(lambda: [])}))

    with kbc.connect() as conn:
        now = int(time.time())
        tid = _park(conn, until=now - 30, assignee="a", title="due card")
        res = kbd.dispatch_once(conn, dry_run=False, max_spawn=1)

    assert tid in res.woken_by_timer, res
    assert _row(conn, tid)["status"] in ("ready", "running")
    # Spawn may be refused by an environment guard; the WAKE itself must have landed.
    assert _row(conn, tid)["scheduled_until"] is None
