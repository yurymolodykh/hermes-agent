"""Regression: a requeue stamp must not park a card in ``blocker_auth`` forever.

A rate-limit requeue writes ``last_failure_error = "pid N exited rate-limited
(quota wall) — requeued without counting a failure"`` on purpose without
counting a failure. That text matches ``_RESPAWN_BLOCKER_RE``, so the respawn
guard used to return ``blocker_auth`` for it *forever* once the rate-limit run
stopped being the latest one: the card was never re-spawned, its
``consecutive_failures`` stayed 0 so the breaker never tripped, and nothing on
the board or in the logs said why (#119070).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def test_rate_limit_stamp_does_not_park_card_after_a_normal_run(kanban_home, monkeypatch):
    """``rate_limit_cooldown`` while the rate-limited run is latest and inside
    the cooldown; ``None`` once a later run reached a normal terminal outcome
    and the stamp is merely stale context."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")

    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl-stamp", assignee="a")

        kb.claim_task(conn, tid, claimer=f"{host}:w0")
        pid = 72000
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        kbd._record_worker_exit(pid, _exited_status(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # Latest run is the rate-limited one and inside the cooldown.
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # The card is picked up again and this time reaches a normal outcome.
        claimed = kb.claim_task(conn, tid, claimer=f"{host}:w1")
        assert claimed is not None
        assert kb.request_review(
            conn, tid, summary="done", reviewer="r",
            expected_run_id=claimed.current_run_id,
        )

        latest = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 1", (tid,),
        ).fetchone()
        assert latest["outcome"] == "review_requested"
        # The stale stamp is still on the row — it must not park the card.
        assert (kb.get_task(conn, tid).last_failure_error or "")
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None, (
            "a rate-limit stamp superseded by a normal run must not return blocker_auth"
        )
        assert kbd.check_respawn_guard(conn, tid) is None


def test_latest_run_order_breaks_same_second_ties(kanban_home, monkeypatch):
    """Two runs can end inside the same second; ordering on ``ended_at`` alone
    picked an arbitrary row, so a rate-limited run could shadow the normal run
    that superseded it. The newest *inserted* run must win."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")

    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="tie", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{host}:w0")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (72100, tid))
        conn.commit()
        kbd._record_worker_exit(72100, _exited_status(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
        kbd.detect_crashed_workers(conn)

        claimed = kb.claim_task(conn, tid, claimer=f"{host}:w1")
        assert claimed is not None
        with kb.write_txn(conn):
            # Force a same-second collision: both runs end at the identical stamp.
            row = conn.execute(
                "SELECT ended_at FROM task_runs WHERE task_id=? AND outcome='rate_limited'", (tid,),
            ).fetchone()
            assert kb.request_review(
                conn, tid, summary="done", reviewer="r",
                expected_run_id=claimed.current_run_id,
            )
            conn.execute(
                "UPDATE task_runs SET ended_at=? WHERE task_id=? AND outcome='review_requested'",
                (row["ended_at"], tid),
            )

        assert kb.get_task(conn, tid).last_failure_error  # stamp still present
        assert kbd.check_respawn_guard(conn, tid) is None
