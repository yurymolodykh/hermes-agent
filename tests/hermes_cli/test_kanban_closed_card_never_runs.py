"""OPS-16: a closed card must never cost another worker run, another failure, or another alarm.

The board journal (18-19.11) showed the same attempt burning to its iteration cap AFTER
``completed``: ``t_6018df99`` completed 02:05:10, then ``commented`` 02:07:03, then
``timed_out`` "Iteration budget exhausted (300/300)" at 02:07:04. Nothing re-spawned the
card — the leftover turn did — but the consequences were real: a false "timed out" alarm in
the owner's chat and a failure booked onto a ``done`` card.

These tests pin the four acceptance criteria of OPS-16:

1. no worker starts on a terminal card (dispatcher lanes stay ``ready``/``review``);
2. a comment on a closed card reaches the chat but wakes nobody;
3. no "timed out" alarm is produced for a card already in ``done``;
4. ``consecutive_failures`` of a closed card never moves.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _done_card(conn, *, title="closed") -> tuple[str, int]:
    """A running card completed through the real transition, run row retained."""
    tid = kb.create_task(conn, title=title, assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id) is True
    return tid, run_id


# --- 1. the dispatcher never starts a worker on a terminal card ----------------------------

def test_dispatcher_never_spawns_a_done_card(conn):
    """Every lane the dispatcher walks holds only ``ready``/``review``; a ``done`` card is
    invisible to it even when it is the only row on the board."""
    _done_card(conn)
    spawned = []

    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 0)

    assert spawned == []
    assert result.spawned == []
    # Belt and braces: the lane queries themselves cannot return a terminal row.
    for status in ("ready", "review"):
        ids = [r["id"] for r in kbd._lane_rows(conn, status)]
        assert ids == []


# --- 2. a comment on a closed card is delivered but wakes nobody --------------------------

def test_comment_on_done_card_is_never_a_wake_kind():
    """``commented`` is deliverable and explicitly NOT a wake kind: a note is information for
    the human, it must not start a worker turn."""
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _WAKE_KINDS

    assert "commented" in TERMINAL_KINDS
    assert "commented" not in _WAKE_KINDS


def test_comment_on_done_card_keeps_the_card_closed_and_carries_a_preview(conn):
    tid, _run_id = _done_card(conn)
    failures_before = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0]

    kb.add_comment(conn, tid, author="02-hermes-platform", body="Итог: работа сдана.")

    assert kb.get_task(conn, tid).status == "done"
    assert conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0] == failures_before
    ev = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    payload = json.loads(ev[0])
    # The chat gets the note itself, not a bare "someone commented".
    assert payload["preview"] == "Итог: работа сдана."
    assert payload["author"] == "02-hermes-platform"


def test_comment_preview_is_bounded(conn):
    tid, _run_id = _done_card(conn)
    kb.add_comment(conn, tid, author="coder", body="я" * 5000)
    payload = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()[0])
    assert len(payload["preview"]) <= kb._COMMENT_PREVIEW_CHARS + 1
    assert payload["len"] == 5000  # the stored comment is untouched


# --- 3 + 4. no failure accounting / no alarm on a closed card -----------------------------

def test_record_task_failure_ignores_a_done_card(conn):
    """The late ``timed_out`` of an attempt that outlived its own handoff must not raise the
    counter, must not rewrite the status and must not emit an event the notifier pings."""
    tid, run_id = _done_card(conn)
    events_before = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]

    tripped = kbd._record_task_failure(
        conn, tid,
        error="Iteration budget exhausted (300/300) — task could not complete within the allowed iterations",
        outcome="timed_out", release_claim=True, end_run=True,
    )

    assert tripped is False
    task = kb.get_task(conn, tid)
    assert task.status == "done"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0] == events_before
    # The real run stayed `completed`/`done`; nothing re-opened or re-closed it.
    run = conn.execute("SELECT outcome, status FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert (run["outcome"], run["status"]) == ("completed", "done")


def test_stale_failure_event_is_not_delivered_for_a_done_card():
    from gateway.kanban_watchers_notifier import event_is_stale_for_closed_card

    CLOSED_AT = 1_000

    class _Ev:
        def __init__(self, kind, created_at=None):
            self.kind = kind
            self.created_at = created_at

    class _Task:
        def __init__(self, status, completed_at=None):
            self.status = status
            self.completed_at = completed_at

    # A failure echo booked AFTER the close: dropped.
    for kind in ("timed_out", "crashed", "gave_up"):
        for status in ("done", "archived"):
            late = _Ev(kind, CLOSED_AT + 60)
            assert event_is_stale_for_closed_card(late, _Task(status), CLOSED_AT) is True
    # Same failure while the card still needs someone: always delivered.
    for status in ("running", "ready", "review", "blocked"):
        late = _Ev("timed_out", CLOSED_AT + 60)
        assert event_is_stale_for_closed_card(late, _Task(status), CLOSED_AT) is False
    # Terminal kinds that carry information (not failures) are never suppressed.
    for kind in ("completed", "commented", "review_requested", "blocked"):
        late = _Ev(kind, CLOSED_AT + 60)
        assert event_is_stale_for_closed_card(late, _Task("done"), CLOSED_AT) is False


# --- a real failure that happened BEFORE the close is never suppressed ---------------------

def test_failure_before_the_close_is_delivered_even_when_processed_late():
    """The 21.11 defect: status-at-delivery swallowed 5 real crashes.

    ``t_48888b47``/``t_d9ceb3ac`` crashed/failed at 00:17:49…00:19:51 and were archived at
    00:27:18 — 569 s later. The notifier only reached those events after the archive, read
    ``status='archived'`` and dropped all five. The event's own timestamp is what decides.
    """
    from gateway.kanban_watchers_notifier import event_is_stale_for_closed_card

    CLOSED_AT = 1_795_210_038  # 21.11 00:27:18 — the ``archived`` event's own stamp

    class _Ev:
        def __init__(self, kind, created_at):
            self.kind = kind
            self.created_at = created_at

    class _Task:
        status = "archived"
        completed_at = None  # archived without completing — the live shape

    # Exactly the five event stamps from the incident journal, all before the close.
    # (Stamps below are the live ``task_events.created_at`` values.)
    incident = [
        ("crashed", 1_795_209_469),   # 21.11 00:17:49  t_48888b47 / t_d9ceb3ac
        ("crashed", 1_795_209_530),   # 21.11 00:18:50  both cards
        ("gave_up", 1_795_209_530),   # 21.11 00:18:50  both cards
        ("crashed", 1_795_209_591),   # 21.11 00:19:51  both cards
        ("gave_up", 1_795_209_591),   # 21.11 00:19:51  both cards
    ]
    for kind, stamp in incident:
        assert stamp < CLOSED_AT
        assert event_is_stale_for_closed_card(_Ev(kind, stamp), _Task(), CLOSED_AT) is False
    # The +569 s gap quoted in the incident: the archive came LONG after the failures.
    assert CLOSED_AT - incident[0][1] == 569 and CLOSED_AT - incident[1][1] == 508
    # The ``archived`` event itself is not a failure kind: never suppressed.
    assert event_is_stale_for_closed_card(_Ev("archived", CLOSED_AT), _Task(), CLOSED_AT) is False


def test_failure_without_a_known_closure_time_is_delivered():
    """No closure evidence ⇒ no suppression: an unknown moment cannot justify silence."""
    from gateway.kanban_watchers_notifier import event_is_stale_for_closed_card

    class _Ev:
        kind = "crashed"
        created_at = 1_000

    class _Task:
        status = "archived"
        completed_at = None

    assert event_is_stale_for_closed_card(_Ev(), _Task(), None) is False


def test_failure_at_the_closure_second_is_delivered():
    """Boundary: an event stamped at the same second as the close is not "after" it."""
    from gateway.kanban_watchers_notifier import event_is_stale_for_closed_card

    class _Ev:
        kind = "timed_out"
        created_at = 1_000

    class _Task:
        status = "done"
        completed_at = 1_000

    assert event_is_stale_for_closed_card(_Ev(), _Task(), 1_000) is False
    assert event_is_stale_for_closed_card(_Ev(), _Task(), 999) is True


# --- the worker's own turn ends at handoff ------------------------------------------------

def test_worker_card_closed_sees_the_handoff(conn, monkeypatch):
    from agent.kanban_stop import worker_card_closed

    tid, run_id = _done_card(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    # After the handoff the card has no live run, so the turn must stop.
    assert worker_card_closed() is True


def test_worker_card_closed_is_false_while_the_run_is_open(conn, monkeypatch):
    from agent.kanban_stop import worker_card_closed

    tid = kb.create_task(conn, title="live", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    assert worker_card_closed() is False

    # A successor attempt now owns the run: this one must stop working the card.
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id + 1000, tid))
    conn.commit()
    assert worker_card_closed() is True


def test_worker_card_closed_fails_closed_without_a_board(monkeypatch, tmp_path):
    from agent.kanban_stop import worker_card_closed

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "absent.db"))
    # An unreadable board must never truncate a live turn.
    assert worker_card_closed() is False


def test_worker_card_closed_is_inert_outside_a_worker(monkeypatch):
    from agent.kanban_stop import worker_card_closed

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert worker_card_closed() is False
