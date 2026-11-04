"""Tests for the Kanban DB layer (hermes_cli.kanban_db)."""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path

import pytest

import hermes_state
from hermes_cli import kanban_db as kb


pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------







@pytest.mark.windows_only
def test_cross_process_init_lock_uses_windows_byte_range_lock(tmp_path, monkeypatch):
    """Windows must use a real (non-blocking) process lock, not a no-op open.

    The init lock acquires with LK_NBLCK in a bounded retry loop (#36644) so a
    wedged holder can never block connect() forever; a clean acquire takes the
    lock once and releases it once.

    ``windows_only``: ``msvcrt`` does not exist off Windows, so faking
    ``_IS_WINDOWS`` on Linux meant injecting a fake ``msvcrt`` module too —
    the test then asserted against its own stub rather than the byte-range
    locking API. Here the platform is real; only ``msvcrt.locking`` is
    instrumented so the call sequence is observable.
    """
    calls: list[tuple[int, int, int]] = []
    import msvcrt as _msvcrt

    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=_msvcrt.LK_NBLCK,
        LK_UNLCK=_msvcrt.LK_UNLCK,
        locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    db_path = tmp_path / "kanban.db"
    with kb._cross_process_init_lock(db_path):
        # Acquired exactly once via the non-blocking byte-range lock.
        assert [call[1:] for call in calls] == [(fake_msvcrt.LK_NBLCK, 1)]

    # Released once on exit.
    assert [call[1:] for call in calls] == [
        (fake_msvcrt.LK_NBLCK, 1),
        (fake_msvcrt.LK_UNLCK, 1),
    ]


def test_connect_migrates_legacy_db_before_optional_column_indexes(tmp_path):
    """Legacy DBs missing additive indexed columns must migrate cleanly.

    SCHEMA_SQL runs in ``connect()`` before ``_migrate_add_optional_columns``.
    Indexes over additive columns therefore must be created after the
    migration adds those columns, or boards predating the column fail to
    open before migration can run.

    Covers all four indexes that sit on additive columns:
    - ``tasks.session_id``       -> ``idx_tasks_session_id``    (#28447)
    - ``tasks.tenant``           -> ``idx_tasks_tenant``        (#16081)
    - ``tasks.idempotency_key``  -> ``idx_tasks_idempotency``   (#17805)
    - ``task_events.run_id``     -> ``idx_events_run``          (#17805)
    """
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    # Pre-#16081 ``tasks`` shape: missing tenant, idempotency_key, session_id.
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
    """)
    # Pre-#17805 ``task_events`` shape: missing run_id. Required because
    # ``_migrate_add_optional_columns`` unconditionally runs PRAGMA on
    # ``task_events`` for run_id back-fill.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        task_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        event_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(task_events)")
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    # Additive columns added by migration:
    assert "session_id" in task_columns
    assert "tenant" in task_columns
    assert "idempotency_key" in task_columns
    assert "run_id" in event_columns
    # And their indexes — the regression scope of this test:
    assert "idx_tasks_session_id" in indexes
    assert "idx_tasks_tenant" in indexes
    assert "idx_tasks_idempotency" in indexes
    assert "idx_events_run" in indexes


# ---------------------------------------------------------------------------
# Task creation + status inference
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Links + dependency resolution
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Atomic claim (CAS)
# ---------------------------------------------------------------------------



def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        assert any(e.kind == "scheduled" and e.payload == {"reason": "run next week"} for e in events)








def test_stale_claim_reclaim_event_records_diagnostic_payload(
    kanban_home, monkeypatch,
):
    """``reclaimed`` events should carry claim_expires, last_heartbeat_at,
    and worker_pid so operators can diagnose why a claim went stale
    (#23025: previous payload only had ``stale_lock`` which gives no
    timing context)."""
    import json
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 3600
        hb_at = int(time.time()) - 1800
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, hb_at, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (t,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["claim_expires"] == old_expires
        assert payload["last_heartbeat_at"] == hb_at
        assert payload["worker_pid"] == 12345
        assert payload["host_local"] is True






# ---------------------------------------------------------------------------
# Rate-limit requeue: a worker that bails on a provider quota wall must be
# released back to ``ready`` WITHOUT counting a failure, so a long (e.g.
# 5-hour) quota window can't trip the circuit breaker and permanently block
# the card. The respawn guard then defers it on a cooldown until quota
# returns. Regression coverage for the kanban-rate-limit-failure report.
# ---------------------------------------------------------------------------


def _exited_status(code: int) -> int:
    """Platform-specific reap-registry value for an exited child."""
    return code if os.name == "nt" else code << 8




def test_rate_limit_exit_requeues_without_counting_failure(
    kanban_home, monkeypatch,
):
    """A rate-limit sentinel exit releases the task to ``ready`` and leaves
    ``consecutive_failures`` untouched — the breaker must never trip on a
    transient throttle, even across many quota-wall hits."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")

        # Simulate FAR more quota-wall hits than DEFAULT_FAILURE_LIMIT (2).
        # If any of these counted as a failure the task would be blocked.
        for i in range(6):
            pid = 70000 + i
            # Claim to open a real run (so detect_crashed_workers can close
            # it with a rate_limited outcome), then point the claim at this
            # host + a dead pid so the crash path acts on it.
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? "
                "WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            _kb._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            )

            crashed = kb.detect_crashed_workers(conn)
            # Rate-limited requeues are NOT crashes.
            assert tid not in crashed
            rl = getattr(_kb.detect_crashed_workers, "_last_rate_limited", [])
            assert tid in rl

            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.consecutive_failures == 0, (
                f"hit {i}: rate-limit must not count a failure, "
                f"got {task.consecutive_failures}"
            )

        # Last failure error stamped so the respawn guard recognizes the
        # quota wall.
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # A ``rate_limited`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "rate_limited" in outcomes
        assert "crashed" not in outcomes




def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# Stale quota stamp: ``last_failure_error`` must describe the CURRENT blocker,
# not the history of the card.
#
# Incident (audit 2026-11-01, 40 cards idle 10-23 h, 21 294 ``respawn_guarded``
# events with reason=blocker_auth): a run hit the quota wall (``rate_limited``
# → quota-flavored stamp), then a LATER run ended ``api_unavailable`` (provider
# outage) or was ``reclaimed`` (gateway restart). Both of those paths leave the
# task in ``ready`` without counting a failure, and neither refreshed nor
# cleared ``last_failure_error``. The cooldown branch keys off the LATEST run
# outcome, so it stopped applying; the ``blocker_auth`` regex then matched the
# stale quota text on every tick forever — a permanent ready-but-never-spawned
# card with no exit (``consecutive_failures`` stays 0, so the breaker never
# fires either).
#
# Required behaviour: an outcome that is not a task refusal must clear the
# column (exactly what ``unblock_task``/``_clear_failure_counter`` already do).
# ---------------------------------------------------------------------------


def _provider_hit(conn, tid, n, *, host, exit_code):
    """Claim → repoint at a dead pid → record the reap → run one
    ``detect_crashed_workers`` pass. ``exit_code`` selects the branch under
    test (``KANBAN_RATE_LIMIT_EXIT_CODE`` / ``KANBAN_PROVIDER_UNAVAILABLE_EXIT_CODE``).
    """
    import hermes_cli.kanban_db as _kb

    assert kb.claim_task(conn, tid, claimer=f"{host}:w{n}")
    pid = 90000 + n
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    _kb._record_worker_exit(pid, _exited_status(exit_code))
    kb.detect_crashed_workers(conn)
    return pid


def test_stale_quota_stamp_cleared_by_later_api_unavailable_run(
    kanban_home, monkeypatch,
):
    """rate_limited → api_unavailable must NOT leave the guard returning
    ``blocker_auth``. Currently RED: the stamp from the quota wall survives
    the provider-outage requeue, so the card is deferred on every tick."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        _kb, "_provider_route_blocker", lambda provider, model: None
    )
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    host = _kb._claimer_id().split(":", 1)[0]
    now = 5_000_000
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="stale-quota", assignee="a",
            model_override="deepseek-v4-flash", provider_override="letovo",
        )

        # Step 1: the card hits the quota wall → requeued rate_limited, stamp set.
        _provider_hit(
            conn, tid, 0, host=host,
            exit_code=_kb.KANBAN_RATE_LIMIT_EXIT_CODE,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert "rate-limited" in (task.last_failure_error or "")
        # Inside the cooldown the guard defers for the RIGHT reason.
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Step 2: the next run dies of a provider outage (NOT a task refusal).
        # Advance past the quota cooldown so branch 1 no longer applies.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        _provider_hit(
            conn, tid, 1, host=host,
            exit_code=_kb.KANBAN_PROVIDER_UNAVAILABLE_EXIT_CODE,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "ready", "provider outage requeues to ready"
        assert task.consecutive_failures == 0
        latest = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? "
            "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
            (tid,),
        ).fetchone()[0]
        assert latest == "api_unavailable"

        # The stale quota stamp must be gone: nothing current says "quota".
        assert kb.check_respawn_guard(conn, tid) is None, (
            "a later api_unavailable run must not inherit the previous quota "
            f"wall's stamp (got {kb.get_task(conn, tid).last_failure_error!r})"
        )
        # And the dispatch tick must actually spawn the card.
        import hermes_cli.profiles as _profiles
        monkeypatch.setattr(_profiles, "profile_exists", lambda name: True)
        monkeypatch.setattr(
            _kb, "resolve_task_root_route",
            lambda conn, tid, persist=False, allow_unpinned=False: (
                (tid,), "letovo", "deepseek-v4-flash",
            ),
        )
        # Assignee-aware provider pre-flight needs a real profile on disk;
        # this test is about the respawn guard, not route resolution.
        monkeypatch.setattr(
            _kb, "resolve_assignee_spawn_route",
            lambda assignee, provider, model: (provider, model, None),
        )
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws, **kw: 424242, dry_run=False,
        )
        assert tid in [s[0] for s in res.spawned], (
            "card with a fresh api_unavailable outcome must spawn on the next "
            f"tick; guarded={res.respawn_guarded}"
        )


def test_stale_quota_stamp_cleared_by_later_reclaimed_run(
    kanban_home, monkeypatch,
):
    """rate_limited → reclaimed (gateway restart) must also clear the stamp.
    Currently RED for the same reason: the reclaim path never touches it."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        _kb, "_provider_route_blocker", lambda provider, model: None
    )
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    host = _kb._claimer_id().split(":", 1)[0]
    now = 5_000_000
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="stale-quota-reclaim", assignee="a",
            model_override="deepseek-v4-flash", provider_override="letovo",
        )
        _provider_hit(
            conn, tid, 0, host=host,
            exit_code=_kb.KANBAN_RATE_LIMIT_EXIT_CODE,
        )
        assert "rate-limited" in (kb.get_task(conn, tid).last_failure_error or "")

        # A stale claim from a DEAD dispatcher generation reclaims the card.
        kb.claim_task(conn, tid, claimer=f"{host}:w-reclaim")
        conn.execute(
            "UPDATE tasks SET gateway_generation=?, worker_pid=? WHERE id=?",
            ("pid:1:start:0.0:boot:deadbeef", 987654, tid),
        )
        conn.commit()
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        kb.reclaim_dead_generation_running(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        latest = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? "
            "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
            (tid,),
        ).fetchone()[0]
        assert latest == "reclaimed"
        assert task.consecutive_failures == 0

        assert kb.check_respawn_guard(conn, tid) is None, (
            "a later reclaimed run must not inherit the previous quota wall's "
            f"stamp (got {task.last_failure_error!r})"
        )


def test_respawn_cooldown_still_applies_after_stamp_hygiene(
    kanban_home, monkeypatch,
):
    """Guard against over-clearing: while ``rate_limited`` IS the latest
    outcome, cooldown still defers inside the window and allows the probe
    after it (the documented rate-limit contract must survive the reset)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        _kb, "_provider_route_blocker", lambda provider, model: None
    )
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    host = _kb._claimer_id().split(":", 1)[0]
    now = 5_000_000
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="cooldown-intact", assignee="a",
            model_override="deepseek-v4-flash", provider_override="letovo",
        )
        _provider_hit(
            conn, tid, 0, host=host,
            exit_code=_kb.KANBAN_RATE_LIMIT_EXIT_CODE,
        )
        # ensure the latest run's ended_at is the injected "now"
        conn.execute(
            "UPDATE task_runs SET ended_at=? WHERE task_id=? AND ended_at IS NOT NULL",
            (now, tid),
        )
        conn.commit()
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
        # After the cooldown the card is allowed to probe.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, tid) is None








# ---------------------------------------------------------------------------
# Complete / block / unblock / archive / assign
# ---------------------------------------------------------------------------







def test_recompute_ready_honours_dispatcher_failure_limit(kanban_home):
    """The guard's effective limit must follow the same resolution order
    as the circuit breaker (#35072): per-task max_retries → dispatcher
    failure_limit → DEFAULT_FAILURE_LIMIT.

    Without threading the dispatcher's ``kanban.failure_limit`` through,
    the guard falls back to DEFAULT_FAILURE_LIMIT and disagrees with the
    breaker — sticking a task prematurely (config limit > default) or
    letting a tripped task escape (config limit < default).
    """
    with kb.connect() as conn:
        # Config allows MORE retries than the default. A task blocked
        # with failures below the configured limit must still recover.
        t = kb.create_task(conn, title="lenient", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=? "
            "WHERE id=?",
            (kb.DEFAULT_FAILURE_LIMIT, t),
        )
        conn.commit()
        # Default-limit call would stick it (failures >= default).
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"
        # Dispatcher configured a higher limit → recover, preserve counter.
        promoted = kb.recompute_ready(
            conn, failure_limit=kb.DEFAULT_FAILURE_LIMIT + 2
        )
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT

        # Config allows FEWER retries than the default. A task at the
        # stricter limit must stay blocked even though it's below default.
        t2 = kb.create_task(conn, title="strict", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (t2,),
        )
        conn.commit()
        # Default-limit (2) would recover it (1 < 2).
        # Stricter config limit (1) must keep it blocked (1 >= 1).
        assert kb.recompute_ready(conn, failure_limit=1) == 0
        assert kb.get_task(conn, t2).status == "blocked"




# ---------------------------------------------------------------------------
# Parent-completion invariant at the claim gate (RCA t_a6acd07d)
# ---------------------------------------------------------------------------














def test_delete_archived_task_removes_related_rows(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        tid = kb.create_task(conn, title="child", parents=[parent], assignee="worker")
        kb.add_comment(conn, tid, "user", "cleanup me")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="done")
        assert kb.archive_task(conn, tid)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', '123', '', 'u', 0, 0)",
            (tid,),
        )
        conn.commit()

        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (tid, tid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_task_removes_task_and_cascades(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="to-delete", assignee="alice")
        kb.add_comment(conn, t, "user", "comment")
        kb.add_comment(conn, t, "user", "another")
        assert kb.delete_task(conn, t)
        assert kb.get_task(conn, t) is None
        assert len(kb.list_comments(conn, t)) == 0
        assert len(kb.list_events(conn, t)) == 0
        assert len(kb.list_runs(conn, t)) == 0




# ---------------------------------------------------------------------------
# Comments / events / worker context
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Respawn guard (check_respawn_guard + dispatch_once integration)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------









def test_worktree_workspace_explicit_target_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "custom-task"
    branch = "wt/custom-task"
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="ship",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=branch,
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    assert ws == target
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {str(target).replace(chr(92), '/')}" in listed
    assert f"branch refs/heads/{branch}" in listed


# ---------------------------------------------------------------------------
# Scratch cleanup containment (#28818)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Completion-prose artifact discovery (t_24120021)
# ---------------------------------------------------------------------------


def _scratch_task_with_files(conn, tmp_path, names):
    t = kb.create_task(conn, title="review")
    task = kb.get_task(conn, t)
    ws = kb.resolve_workspace(task)
    kb.set_workspace_path(conn, t, ws)
    files = []
    for name in names:
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("output", encoding="utf-8")
        files.append(p)
    return t, ws, files


def test_prose_artifact_discovery_accepts_forward_slashes(kanban_home):
    """A Windows worker that names its scratch files with ``/`` keeps them.

    t_24120021: the soft-finish close of t_f1581cbb listed six files in the
    summary using the documented forward-slash notation; the literal
    workspace-prefix match found none, ``artifacts`` stayed empty, and
    ``_cleanup_workspace`` deleted ``VERDICT_t_f1581cbb.md`` with the rest.
    """
    with kb.connect() as conn:
        t, ws, files = _scratch_task_with_files(conn, None, ["VERDICT_x.md"])
        prose = "Файлы, созданные в этом прогоне:\n- " + str(files[0]).replace("\\", "/")

        assert kb.complete_task(conn, t, summary=prose)

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        payload = completed.payload.get("artifacts") or []

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert len(payload) == 1
    preserved = Path(payload[0])
    assert preserved.read_text(encoding="utf-8") == "output"
    assert preserved.parent == kb.task_attachments_dir(t)


def test_prose_artifact_discovery_dedupes_two_spellings(kanban_home):
    """``C:/a/b`` and ``C:\\a\\b`` in one summary are one artifact, not two."""
    with kb.connect() as conn:
        t, _ws, files = _scratch_task_with_files(conn, None, ["report.md"])
        prose = (
            f"Отчёт: {files[0]}\n"
            f"Копия того же пути: {str(files[0]).replace(chr(92), '/')}\n"
        )

        assert kb.complete_task(conn, t, summary=prose)

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        payload = completed.payload.get("artifacts") or []
        attachments = kb.list_attachments(conn, t)

    assert len(payload) == 1, payload
    assert [a.filename for a in attachments] == ["report.md"]


def test_prose_artifact_discovery_handles_spaces_in_filenames(kanban_home):
    """A bullet line is read whole, so ``rv defects.py`` is not truncated."""
    with kb.connect() as conn:
        t, _ws, files = _scratch_task_with_files(conn, None, ["rv defects.py"])
        prose = "Файлы:\n- " + str(files[0])

        assert kb.complete_task(conn, t, summary=prose)

        attachments = kb.list_attachments(conn, t)

    assert [a.filename for a in attachments] == ["rv defects.py"]


def test_prose_artifact_discovery_ignores_paths_outside_workspace(kanban_home, tmp_path):
    """Only files inside the managed scratch workspace are promoted."""
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not a deliverable", encoding="utf-8")
    with kb.connect() as conn:
        t, _ws, files = _scratch_task_with_files(conn, None, ["inside.md"])
        prose = f"- {files[0]}\n- {outside}\n"

        assert kb.complete_task(conn, t, summary=prose)

        attachments = kb.list_attachments(conn, t)

    assert [a.filename for a in attachments] == ["inside.md"]


def test_complete_task_persists_scratch_artifacts_before_cleanup(kanban_home):
    """Completion artifacts from scratch workspaces survive workspace cleanup."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="render chart")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"png-bytes")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert persisted.exists(), "artifact copy should survive scratch cleanup"
    assert persisted.parent == kb.task_attachments_dir(t)
    assert persisted.name == "chart.png"
    assert persisted.read_bytes() == b"png-bytes"
    assert str(persisted) != str(artifact)
    assert run is not None
    assert run.metadata["artifacts"] == [str(persisted)]
    with kb.connect() as conn:
        attachments = kb.list_attachments(conn, t)
    assert [(a.filename, a.stored_path) for a in attachments] == [
        ("chart.png", str(persisted.resolve()))
    ]




# ---------------------------------------------------------------------------
# Deferred scratch cleanup for parent/child handoff (#33774)
# ---------------------------------------------------------------------------




def test_dir_child_completion_unblocks_deferred_scratch_parent(kanban_home, tmp_path):
    """A non-scratch ('dir') child completing must still sweep its scratch parent.

    Regression for the gap where ``_cleanup_workspace`` returned early for a
    non-scratch task and never ran the parent sweep — leaking the parent's
    deferred scratch dir forever.
    """
    child_dir = tmp_path / "persistent-child"
    child_dir.mkdir()
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(
            conn, title="dir child", workspace_kind="dir",
            workspace_path=str(child_dir),
        )
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)

        kb.complete_task(conn, parent, result="handoff")
        assert parent_ws.exists(), "deferred while dir child active"

        kb.complete_task(conn, child, result="built")

    assert not parent_ws.exists(), (
        "A 'dir' child completing must trigger the parent scratch sweep"
    )
    assert child_dir.exists(), "Non-scratch 'dir' child workspace is never deleted"




def test_is_managed_scratch_path_rejects_kanban_metadata_subtrees(kanban_home):
    """Hermes' own DB/metadata/log subtrees under ``<kanban_home>/kanban`` are NOT managed.

    Regression guard for the Copilot finding on #28819: a scratch task whose
    ``workspace_path`` was mis-set to the kanban home, the logs dir, or a
    board's metadata dir (i.e. the board root itself, not its ``workspaces/``
    child) must be refused. Without this, the containment check would happily
    ``shutil.rmtree`` Hermes' DB/metadata/logs on task completion.
    """
    kanban_root = kanban_home / "kanban"
    kanban_root.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(kanban_root)

    logs_dir = kanban_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(logs_dir)

    board_root = kanban_root / "boards" / "my-board"
    board_root.mkdir(parents=True, exist_ok=True)
    # The board root itself is NOT a managed scratch dir — only the
    # ``workspaces/`` child (and its descendants) are.
    assert not kb._is_managed_scratch_path(board_root)

    # Sibling subtrees of ``workspaces/`` under a board (e.g. its kanban.db
    # or board.json living next to ``workspaces/``) are also not managed.
    board_logs = board_root / "logs"
    board_logs.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(board_logs)

    # Now create the board's workspaces dir and a task scratch dir under it —
    # the latter is the only thing the guard should allow.
    board_workspaces = board_root / "workspaces"
    board_workspaces.mkdir(parents=True, exist_ok=True)
    # The workspaces root itself is also NOT managed — deleting it would
    # wipe every task's scratch dir at once.
    assert not kb._is_managed_scratch_path(board_workspaces)
    task_dir = board_workspaces / "task-42"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert kb._is_managed_scratch_path(task_dir)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Originating session id (ACP propagation)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Shared-board path resolution (issue #19348)
#
# The kanban board is a cross-profile coordination primitive: a worker
# spawned with `hermes -p <profile>` must read/write the same kanban.db
# as the dispatcher that claimed the task. These tests exercise the
# path-resolution layer directly and would have caught the regression
# where `kanban_db_path()` resolved to the active profile's HERMES_HOME.
# ---------------------------------------------------------------------------

class TestSharedBoardPaths:
    """`kanban_home`/`kanban_db_path`/`workspaces_root`/`worker_log_path`
    must anchor at the **shared root**, not the active profile's HERMES_HOME."""

    def _set_home(self, monkeypatch, tmp_path, hermes_home):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)


    def test_profile_worker_resolves_to_shared_root(
        self, tmp_path, monkeypatch
    ):
        # Reproduces the bug: dispatcher uses ~/.hermes/kanban.db,
        # worker spawned with -p <profile> previously resolved to
        # ~/.hermes/profiles/<profile>/kanban.db. After the fix both
        # converge on ~/.hermes/kanban.db.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, profile_home)

        # All four resolvers must anchor at the shared root, not the
        # profile-local HERMES_HOME.
        assert kb.kanban_home() == default_home
        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"
        assert (
            kb.worker_log_path("t_0d214f19")
            == default_home / "kanban" / "logs" / "t_0d214f19.log"
        )

        # Sanity: the profile-local path that used to be returned is
        # explicitly NOT what we resolve to anymore.
        assert kb.kanban_db_path() != profile_home / "kanban.db"






    def test_dispatcher_and_worker_share_a_real_database(
        self, tmp_path, monkeypatch
    ):
        # Belt-and-suspenders: round-trip a task across the two
        # HERMES_HOME perspectives via a real SQLite file. Without the
        # fix the worker would open a different file and see no rows.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)

        # Dispatcher creates the board and a task.
        self._set_home(monkeypatch, tmp_path, default_home)
        kb.init_db()
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="cross-profile")

        # Worker switches to the profile HERMES_HOME and reads.
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.title == "cross-profile"




    def test_dispatcher_spawn_injects_kanban_paths_without_stale_session(
        self, tmp_path, monkeypatch
    ):
        # The dispatcher must pin board paths while stripping any unrelated
        # HERMES_SESSION_* identity inherited from the long-lived gateway.
        # The one exception is HERMES_SESSION_SOURCE, which the dispatcher
        # re-sets to its own `kanban` tag AFTER the strip — a value it owns,
        # never one inherited from whatever the gateway last routed.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        from gateway import session_context as sc

        # A dispatcher can launch before the gateway binds its first session.
        monkeypatch.setattr(sc, "_session_context_engaged", False)
        sc.reset_session_vars()
        for key in sc._VAR_MAP:
            monkeypatch.setenv(key, "stale-routing-value")

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = kb.Task(
            id="t_dispatch_env",
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_dispatch_env",
        )
        kb._default_spawn(task, str(tmp_path / "ws"))

        env = captured["env"]
        assert env["HERMES_KANBAN_DB"] == str(default_home / "kanban.db")
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
            default_home / "kanban" / "workspaces"
        )
        assert env["HERMES_KANBAN_TASK"] == "t_dispatch_env"
        assert env["HERMES_KANBAN_BRANCH"] == "wt/t_dispatch_env"
        for key in sc._VAR_MAP:
            if key == "HERMES_SESSION_SOURCE":
                # Re-set by the dispatcher, so what matters is that it carries
                # the worker's own tag rather than the inherited routing value.
                assert env[key] == "kanban"
                continue
            assert key not in env


# ---------------------------------------------------------------------------
# latest_summary / latest_summaries — surface task_runs.summary handoffs
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# NFS / network-filesystem fallback (see hermes_state.apply_wal_with_fallback)
# ---------------------------------------------------------------------------

def test_connect_falls_back_to_delete_on_locking_protocol(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must handle ``locking protocol`` on NFS/SMB.

    Without this fallback, the gateway's kanban dispatcher crashes every
    60s and the kanban migration (``consecutive_failures`` ADD COLUMN) is
    retried forever — which is what the real-world user report shows
    (see hermes-agent issue #22032).

    NOTE: We do NOT use the ``kanban_home`` fixture here because that
    fixture pre-initializes the DB via ``kb.init_db()`` — putting the
    file in WAL on disk. The Bug D safety guard now refuses to downgrade
    to DELETE when the on-disk header is already WAL, so testing the
    NFS-fallback path requires a truly-fresh DB file (NFS scenario in
    production: first connection of the first process ever to touch the
    file, where downgrading is safe because nobody else has WAL state
    yet).
    """
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # These tests exercise the WAL-attempt path; assume a fixed SQLite so the
    # WAL-reset vulnerability gate doesn't short-circuit before the pragma.
    import hermes_state as _hermes_state
    monkeypatch.setattr(
        _hermes_state, "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )
    _hermes_state._wal_fallback_warned_paths.clear()

    # Clear module cache so a fresh connect() is attempted
    kb._INITIALIZED_PATHS.clear()
    hermes_state._wal_fallback_warned_paths.clear()

    real_connect = _sqlite3.connect

    class _WalBlockingConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                raise _sqlite3.OperationalError("locking protocol")
            return super().execute(sql, *args, **kwargs)

    def wal_blocking_connect(*args, **kwargs):
        # connect_tracked passes a tracking-augmented factory; drop it and
        # substitute the double, which connect_tracked re-applies to the
        # returned instance.
        kwargs.pop("factory", None)
        return real_connect(
            *args, factory=_WalBlockingConnection, **kwargs
        )

    with _patch("hermes_cli.kanban_db.sqlite3.connect", side_effect=wal_blocking_connect):
        with caplog.at_level("ERROR", logger="hermes_state"):
            conn = kb.connect()

    # One fallback error, naming kanban.db
    errors = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "kanban.db" in r.getMessage()
    ]
    assert len(errors) >= 1, (
        f"Expected a kanban.db ERROR, got: {[r.getMessage() for r in caplog.records]}"
    )

    # DB still usable end-to-end — create + list a task
    t = kb.create_task(conn, title="post-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()


def test_connect_works_when_wal_is_silently_refused(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must stay usable when WAL silently no-ops to DELETE."""
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    hermes_state._wal_fallback_warned_paths.clear()
    # Assume a fixed SQLite so the WAL-reset gate doesn't short-circuit.
    monkeypatch.setattr(
        hermes_state, "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )

    real_connect = _sqlite3.connect

    class _WalSilentNoOpConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                return super().execute("PRAGMA journal_mode=delete", *args, **kwargs)
            return super().execute(sql, *args, **kwargs)

    def wal_silent_noop_connect(*args, **kwargs):
        kwargs.pop("factory", None)
        return real_connect(
            *args, factory=_WalSilentNoOpConnection, **kwargs
        )

    with _patch(
        "hermes_cli.kanban_db.sqlite3.connect",
        side_effect=wal_silent_noop_connect,
    ):
        with caplog.at_level("ERROR", logger="hermes_state"):
            conn = kb.connect()

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    t = kb.create_task(conn, title="post-silent-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()

    errors = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "kanban.db" in r.getMessage()
    ]
    assert len(errors) >= 1, (
        f"Expected a kanban.db ERROR, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_unlink_tasks_triggers_recompute_ready(kanban_home):
    """Regression test for issue #22459.

    Removing a dependency via unlink_tasks must immediately promote the child
    to ready when all remaining parents are done — same contract as
    complete_task and unblock_task.

    Before the fix, child stayed 'todo' indefinitely after unlink; only the
    next dispatcher tick or a manual 'hermes kanban recompute' would promote it.
    """
    with kb.connect() as conn:
        # A is done.
        a = kb.create_task(conn, title="parent-done")
        kb.complete_task(conn, a)

        # C is running (not done) — blocks child B.
        c = kb.create_task(conn, title="parent-running")
        kb.claim_task(conn, c, claimer="worker:1")

        # B depends on both A (done) and C (running) → stays todo.
        b = kb.create_task(conn, title="child", parents=[a, c])
        assert kb.get_task(conn, b).status == "todo"

        # Remove the blocking dependency C → B.
        removed = kb.unlink_tasks(conn, c, b)
        assert removed is True

        # B's only remaining parent is A (done) → must be ready immediately.
        assert kb.get_task(conn, b).status == "ready", (
            "child should promote to ready immediately after unlink_tasks "
            "removes its last blocking dependency"
        )



# ---------------------------------------------------------------------------
# _add_column_if_missing / _migrate_add_optional_columns idempotency (#21708)
# ---------------------------------------------------------------------------

def test_add_column_if_missing_is_idempotent_on_race(kanban_home):
    """``_add_column_if_missing`` must swallow 'duplicate column name' errors.

    Regression for #21708: the kanban dispatcher opens the DB twice per tick
    (once via _tick_once_for_board, once via init_db's discard-and-reconnect
    path).  A second concurrent connection runs _migrate_add_optional_columns
    before the first one commits, so ALTER TABLE raises OperationalError with
    'duplicate column name: consecutive_failures'.  Without the idempotency
    guard that crashes the dispatcher on the first tick after every restart.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )

    # First call adds the column — returns True.
    added = kb._add_column_if_missing(conn, "tasks", "extra_col", "extra_col TEXT")
    assert added is True
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "extra_col" in cols

    # Second call on same connection — column already exists — must return
    # False without raising, simulating the race the dispatcher hits.
    added_again = kb._add_column_if_missing(
        conn, "tasks", "extra_col", "extra_col TEXT"
    )
    assert added_again is False

    conn.close()


def test_migrate_add_optional_columns_tolerates_concurrent_migration(kanban_home):
    """Full _migrate_add_optional_columns must not raise when columns already
    exist (issue #21708 race window — two connections migrate concurrently).

    The in-memory DB is built from the REAL production SCHEMA_SQL so the state
    genuinely is the fully-migrated one (all optional columns + task_runs +
    task_events + kanban_notify_subs present). A hand-written minimal schema
    drifts: the migration pass also ALTERs task_runs.worker_session_id and
    backfills in-flight ``running`` rows from task columns (assignee,
    started_at, ...), so a stale subset raises instead of testing idempotency.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)

    # Running migration on an already-migrated schema must not raise.
    kb._migrate_add_optional_columns(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Migrator totality on boards that predate task_runs / task_events (t_5c74d00b)
#
# `connect()` runs SCHEMA_SQL before the additive pass, so the tables always
# exist there and the missing-table hole was invisible. The pass is also called
# directly (`init_db` re-migration, repair tooling, tests) on hand-built legacy
# files, and there `PRAGMA table_info` on an absent table returns zero rows
# instead of raising — so every column read as missing and the following
# `ALTER TABLE ... ADD COLUMN` died with `no such table: task_runs`, taking the
# whole migration (and `connect()`) down with it.
# ---------------------------------------------------------------------------


def _build_pre_run_model_board(conn, *, with_claim_columns: bool) -> None:
    """Create a legacy kanban schema that has no ``task_runs`` table.

    ``with_claim_columns`` selects the realistic pre-run-model shape (v1 task
    table carrying the claim/worker columns the in-flight backfill imports)
    versus the pared-down shape the older migration tests use.
    """
    claim_columns = (
        " assignee TEXT, started_at INTEGER, claim_lock TEXT,"
        " claim_expires INTEGER, worker_pid INTEGER,"
        " max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,"
    )
    if not with_claim_columns:
        claim_columns = ""
    conn.execute(
        "CREATE TABLE tasks ("
        " id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,"
        f"{claim_columns}"
        " status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,"
        " created_by TEXT, created_at INTEGER NOT NULL, completed_at INTEGER,"
        " workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'pre-run-model card', 'ready', 1)"
    )
    conn.commit()


def test_migrator_creates_task_runs_when_board_predates_it():
    """`no such table: task_runs` must be impossible (t_5c74d00b).

    Pins the exact crash from the card: the pass is handed a board with only
    ``tasks`` + ``task_events`` and must create ``task_runs`` (with
    ``worker_session_id``) instead of raising, and must do it idempotently.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _build_pre_run_model_board(conn, with_claim_columns=False)

    before = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'"
    ).fetchone()
    assert before is None, "fixture must not contain task_runs"

    kb._migrate_add_optional_columns(conn)  # was: OperationalError

    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "task_runs" in tables, "migration must materialize task_runs"
    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
    assert "worker_session_id" in run_cols
    ev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_events)")}
    assert "run_id" in ev_cols

    indexes = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    for expected in ("idx_runs_task", "idx_runs_status", "idx_events_run"):
        assert expected in indexes, f"bootstrap must create {expected}"

    # Second pass is a no-op, not a duplicate CREATE / duplicate column crash.
    kb._migrate_add_optional_columns(conn)
    conn.close()


def test_migrator_survives_board_without_task_events_either():
    """A board missing BOTH additive-target tables must still migrate."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        " id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " status TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1','ancient','ready',1)"
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)

    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"task_runs", "task_events"} <= tables
    conn.close()


def test_migrator_backfill_still_imports_claim_state_when_columns_exist():
    """Skipping the backfill must be conditioned on missing columns ONLY.

    The guard added for t_5c74d00b must not quietly disable the real
    pre-run-model backfill: a board that does carry the v1 claim columns still
    gets exactly one synthesized run, adopted as ``current_run_id``, and a
    second migration must not duplicate it.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _build_pre_run_model_board(conn, with_claim_columns=True)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, started_at,"
        " assignee, claim_lock) VALUES ('running-card','was running','running',"
        " 1, 1, 'worker', 'lock-legacy')"
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)

    runs = conn.execute(
        "SELECT id, profile, status FROM task_runs WHERE task_id = 'running-card'"
    ).fetchall()
    assert len(runs) == 1, f"expected exactly 1 backfilled run, got {len(runs)}"
    assert runs[0]["status"] == "running"
    assert runs[0]["profile"] == "worker"
    pointer = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = 'running-card'"
    ).fetchone()["current_run_id"]
    assert pointer == runs[0]["id"]

    kb._migrate_add_optional_columns(conn)
    again = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = 'running-card'"
    ).fetchone()[0]
    assert again == 1, f"re-migration duplicated the backfilled run ({again})"
    conn.close()


def test_init_db_upgrades_on_disk_board_without_task_runs(tmp_path):
    """End-to-end AC3: a real legacy file opens through the public API.

    Builds the pre-run-model board on disk, drives ``init_db`` (the path that
    re-runs the additive pass WITHOUT SCHEMA_SQL going first), and reads the
    cards back through ``get_task``/``list_runs``.
    """
    import sqlite3

    db_path = tmp_path / "legacy_kanban.db"
    raw = sqlite3.connect(str(db_path))
    _build_pre_run_model_board(raw, with_claim_columns=True)
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at, started_at,"
        " assignee, claim_lock) VALUES ('running-card','was running','running',"
        " 1, 1, 'worker', 'lock-legacy')"
    )
    raw.commit()
    raw.close()

    kb.init_db(db_path)

    with kb.connect(db_path) as conn:
        assert kb.get_task(conn, "legacy").status == "ready"
        running = kb.get_task(conn, "running-card")
        assert running.status == "running"
        runs = kb.list_runs(conn, "running-card")
        assert len(runs) == 1
        assert running.current_run_id == runs[0].id


# ---------------------------------------------------------------------------
# Dispatcher spawn invocation — _resolve_hermes_argv()
#
# Workers spawned by the dispatcher must use a `hermes` invocation that does
# not depend on PATH being set up correctly. cron jobs, systemd User= services,
# launchd jobs, and other detached processes routinely run with a stripped
# $PATH that doesn't include the venv's bin/, so a bare `["hermes", ...]`
# spawn fails with FileNotFoundError and the task gets stuck. The resolver
# prefers the PATH shim (familiar `ps` output) but falls back to the module
# form so the spawn keeps working when PATH is missing the shim.
# ---------------------------------------------------------------------------


def test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim(monkeypatch):
    """When the shim is not on PATH, fall back to `python -m hermes_cli.main`.

    Pins the correct module name (NOT `hermes` — there is no top-level
    `hermes` package). Regression for #23198: the original PR shipped
    `python -m hermes` which fails with `No module named hermes` on every
    invocation.
    """
    import shutil
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = kb._resolve_hermes_argv()
    assert argv == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_module_actually_runs():
    """The fallback module name must be importable + runnable.

    A unit test that pins the literal string is necessary but not
    sufficient — if `hermes_cli.main` ever loses `if __name__ == "__main__"`
    handling or its argparse setup, `python -m hermes_cli.main --version`
    would fail and so would every dispatcher spawn that hits the fallback.
    Run it as a real subprocess to catch that regression.
    """
    import subprocess
    import hermes_cli.kanban_db as kb
    import shutil
    import unittest.mock as mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kb._resolve_hermes_argv()
    # Real subprocess run of `python -m hermes_cli.main --version`. The
    # timeout is generous: a cold CLI import is heavy (site-packages, plugins,
    # toolset registration) and on a loaded CI/desktop box the full-suite wall
    # time can spike well past the raw 10-15s startup. 120s bounds an actual
    # hang while never flaking on a merely slow machine.
    r = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        f"`{' '.join(argv)} --version` failed (rc={r.returncode}); "
        f"stderr={r.stderr[:200]!r}"
    )
    assert "Hermes Agent" in r.stdout, f"unexpected output: {r.stdout[:200]!r}"


# ---------------------------------------------------------------------------
# task_age — guard against corrupt timestamp values
#
# The Task dataclass declares ``created_at: int`` but rows come from sqlite
# without coercion at the boundary. A row that ever held a non-int (e.g. an
# unsubstituted ``'%s'`` from a logged format string, ``None``, an arbitrary
# string, or a float-as-string) used to crash ``task_age`` with ``ValueError``
# and turn ``GET /api/plugins/kanban/board`` into a 500 because the dashboard
# calls ``task_age`` unguarded for every task in the response.
#
# After the fix, ``_safe_int`` returns ``None`` on bad input and ``task_age``
# degrades gracefully (per-field ``None`` rather than a hard crash).
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> "kb.Task":
    """Minimal Task with all required fields filled in. Override anything."""
    defaults = dict(
        id="t_age",
        title="x",
        body=None,
        assignee=None,
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    defaults.update(overrides)
    return kb.Task(**defaults)












# ---------------------------------------------------------------------------
# Board-level default_workdir
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# dispatch_once — max_in_progress
# ---------------------------------------------------------------------------


def test_dispatch_max_in_progress_blocks_review_when_at_limit(
    kanban_home, all_assignees_spawnable,
):
    """Review-only backlog must still respect max_in_progress."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, running)
        review = kb.create_task(conn, title="review", assignee="bob")
        _set_task_status(conn, review, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=1)
        review_task = kb.get_task(conn, review)

    assert not res.spawned
    assert not spawns
    assert review_task is not None
    assert review_task.status == "review"

# Review column dispatch
# ---------------------------------------------------------------------------


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Test helper: set a task's status directly."""
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))








# Stale detection — detect_stale_running
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Corruption guard (issue #30687)
# ---------------------------------------------------------------------------

def _write_corrupt_db(path: Path) -> bytes:
    """Write a kanban DB with a VALID SQLite header but malformed page content.

    This is the corruption shape the integrity guard specifically targets
    (e.g. issue #29507 follow-up reports where the file's first 16 bytes
    pass the header byte check but ``PRAGMA integrity_check`` then fails
    because the internal pages are damaged). It's what main's header-only
    validator was letting through, and what this PR adds the full guard
    for.
    """
    # 100-byte SQLite header (magic + minimal valid-looking fields) so the
    # cheap header check passes, then deliberate garbage so sqlite refuses
    # to read the file past the header.
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob




def test_repeated_corrupt_open_reuses_single_backup(tmp_path):
    """Repeated quarantines of the same corrupt bytes must not amplify disk usage.

    Regression for the gateway dispatcher's 5-min retry loop on shared kanban
    DBs across multi-profile fleets: each retry on an unchanged corrupt file
    used to create a fresh ``.corrupt.<timestamp>.bak`` until disk filled. The
    content-addressed backup name is deterministic in the DB's sha256, so
    N retries of the same bytes share one backup.
    """
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)

    backups: set[Path] = set()
    for _ in range(10):
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)
        assert excinfo.value.backup_path is not None
        backups.add(excinfo.value.backup_path)

    assert len(backups) == 1, f"expected 1 deterministic backup, got {len(backups)}"
    (backup,) = backups
    assert backup.exists()
    assert backup.read_bytes() == original

    # Mutate the corrupt bytes — fingerprint changes, separate backup preserved.
    with db_path.open("r+b") as f:
        f.seek(4096)
        f.write(b"\xAB" * 64)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with pytest.raises(kb.KanbanDbCorruptError) as excinfo2:
        kb.connect(db_path=db_path)
    second_backup = excinfo2.value.backup_path
    assert second_backup is not None
    assert second_backup != backup
    assert second_backup.exists()


def test_locked_healthy_db_does_not_classify_as_corrupt(tmp_path, monkeypatch):
    """A transient lock during the probe must not produce a .corrupt backup
    and must not be reported as :class:`KanbanDbCorruptError`. Raw sqlite
    ``OperationalError`` (lock/busy) is acceptable and expected."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        # First call is the integrity probe — simulate a lock.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)

    with pytest.raises(sqlite3.OperationalError):
        kb.connect(db_path=db_path)

    # No .corrupt backup may be produced for a healthy-but-locked DB.
    backups = list(tmp_path.glob("*.corrupt.*"))
    assert backups == [], f"unexpected corrupt backups: {backups}"

    # And once the lock clears, normal access still works.
    monkeypatch.setattr(kb.sqlite3, "connect", real_connect)
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="still here")
        titles = [t.title for t in kb.list_tasks(conn)]
    assert "still here" in titles




# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------

def test_maybe_emit_scratch_tip_fires_once_per_install(kanban_home, caplog):
    """First scratch workspace materialization warns + emits an event.

    Subsequent scratch workspaces on the SAME install stay silent — the
    sentinel file under kanban_home() flips after the first emit.
    """
    import logging

    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="first scratch")
        t2 = kb.create_task(conn, title="second scratch")

    # Sentinel must not exist yet on a fresh install.
    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t1, "scratch")

    # Sentinel is now set.
    assert kb._scratch_tip_shown()
    assert kb._scratch_tip_sentinel_path().exists()

    # Warning was logged exactly once.
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert len(tip_records) == 1, (
        f"Expected exactly one tip warning, got {len(tip_records)}: "
        f"{[r.getMessage() for r in tip_records]!r}"
    )

    # An event row was appended on the first task.
    with kb.connect() as conn:
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t1,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "tip_scratch_workspace" in kinds, (
        f"Expected tip_scratch_workspace event on first scratch task; "
        f"got {kinds!r}"
    )

    # Second scratch materialization on the same install stays silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t2, "scratch")
    tip_records2 = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records2 == [], (
        f"Tip should not re-fire after sentinel is set; got "
        f"{[r.getMessage() for r in tip_records2]!r}"
    )
    with kb.connect() as conn:
        events2 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t2,),
        ).fetchall()
    assert "tip_scratch_workspace" not in [e["kind"] for e in events2], (
        "Tip event should not be appended for subsequent scratch tasks."
    )




# ---------------------------------------------------------------------------
# Connection pragmas (secure_delete, cell_size_check, synchronous=FULL)
# ---------------------------------------------------------------------------


def test_connect_sets_secure_delete_on(tmp_path):
    """secure_delete=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA secure_delete").fetchone()
    assert row[0] == 1, f"expected secure_delete=1, got {row[0]}"





# write_txn — rollback handler must not mask the original exception
# ---------------------------------------------------------------------------


def test_write_txn_preserves_original_exception_when_rollback_fails(kanban_home):
    """When a write inside write_txn raises an OperationalError that SQLite
    has already auto-rolled-back (e.g. ``disk I/O error``,
    ``database is locked``, ``database disk image is malformed``), the
    explicit ROLLBACK in ``write_txn.__exit__`` itself raises
    ``cannot rollback - no transaction is active``. The original cause
    must NOT be masked by the secondary rollback failure — operators rely
    on the original cause to diagnose the underlying issue.
    """

    class FailingConnWrapper:
        """Delegate to a real connection, simulating an EIO during an INSERT
        that SQLite has already auto-rolled-back."""

        def __init__(self, real):
            self._real = real
            self._fail_armed = True

        def execute(self, sql, *args, **kwargs):
            if (
                self._fail_armed
                and sql.lstrip().upper().startswith("INSERT")
                and "task_events" in sql.lower()
            ):
                self._fail_armed = False  # one-shot
                # Simulate SQLite auto-rolling back the transaction by
                # issuing a real ROLLBACK now. After this, BEGIN IMMEDIATE
                # is no longer active and an explicit ROLLBACK would error.
                try:
                    self._real.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with kb.connect() as conn:
        wrapper = FailingConnWrapper(conn)
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            with kb.write_txn(wrapper):
                kb._append_event(wrapper, "t_bogus", "promoted", None)

    msg = str(excinfo.value)
    assert "disk I/O error" in msg, (
        f"write_txn masked the original exception with rollback failure; "
        f"got {msg!r} (expected to contain 'disk I/O error')"
    )
    assert "cannot rollback" not in msg, (
        f"write_txn surfaced the rollback failure instead of the original "
        f"OperationalError; got {msg!r}"
    )


def test_write_txn_check_reads_correct_header_fields(tmp_path):
    """A genuinely truncated DB is never reported as passing the invariant.

    The check no longer opens the database file to read header bytes (that
    open/close would cancel this process's POSIX advisory locks — the
    corruption route in sqlite.org/howtocorrupt.html §2.2). It asks SQLite for
    ``page_count`` instead. On a truncated file SQLite refuses that pragma, so
    the helper reports "not healthy" rather than a page-count mismatch; either
    way the file must never come back clean.
    """
    import struct
    from hermes_cli.kanban_db import connect
    from hermes_cli.sqlite_safe_read import file_length_matches_header

    db = tmp_path / "synthetic.db"
    conn = connect(db_path=db)
    conn.execute("PRAGMA journal_mode=DELETE")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()

    with open(db, "rb") as f:
        data = bytearray(f.read())
    real_page_count = struct.unpack(">I", data[28:32])[0]
    if real_page_count < 2:
        pytest.skip("DB too small for synthetic truncation test")
    truncated = bytes(data[: (real_page_count - 1) * page_size])
    with open(db, "wb") as f:
        f.write(truncated)

    raw_conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        assert file_length_matches_header(raw_conn) is not True
    finally:
        raw_conn.close()


# ---------------------------------------------------------------------------
# reap_worker_zombies() tests
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# connect_closing(): context manager that actually closes the FD
# Regression coverage for #33159 (kanban.db FD leak — gateway crashes after
# ~4 days). sqlite3.Connection's built-in __exit__ commits/rollbacks but
# does NOT close, so `with kb.connect() as conn:` leaks the FD in
# long-lived processes (gateway run_slash, dashboard decompose handler).
# `connect_closing()` is the leak-safe replacement.
# ---------------------------------------------------------------------------




def test_bare_connect_does_not_close_on_context_exit(tmp_path):
    """Document the leak that connect_closing exists to prevent.

    sqlite3.Connection's __exit__ commits/rollbacks but doesn't close.
    This is the upstream behaviour we cannot change; the regression
    guard is to make sure connect_closing() does the right thing.
    """
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        pass
    # Still usable after with-block exit (the leak).
    conn.execute("SELECT 1").fetchone()
    conn.close()  # explicit close to avoid leaking THIS test


# ---------------------------------------------------------------------------
# Dispatcher never re-spawns terminal (completed/archived) tasks (#t_db662ef7)
#
# Root cause: claim_task / claim_review_task only gated on ``status`` and
# ``claim_lock``. A broken retry path that corrupts a terminal task's status
# back to ``ready`` made the dispatcher re-spawn an already-completed/archived
# task forever (incident: t_2b49cfcd, archived + completed_at set, re-spawned
# 29x). The guard below adds ``completed_at IS NULL`` to the claim CAS so a
# task that has reached a terminal state can never transition back to running.
# ---------------------------------------------------------------------------

def test_claim_task_rejects_terminal_done_task(kanban_home):
    """A ``done`` task (completed_at set) whose status is corrupted back to
    ``ready`` must not be re-claimed — the fix for the re-spawn loop."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="ship", assignee="a")
        assert kb.complete_task(conn, t, result="ok")
        task = kb.get_task(conn, t)
        assert task.status == "done"
        assert task.completed_at is not None

        # Simulate the broken retry path flipping the terminal task back to ready.
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, current_run_id=NULL WHERE id=?",
            (t,),
        )
        conn.commit()

        # Claiming a terminal task must be refused even though status=='ready'.
        assert kb.claim_task(conn, t) is None
        # Status stays non-running; no claim, no new run, no spawn.
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.current_run_id is None
        run = kb.latest_run(conn, t)
        assert run is not None and run.outcome == "completed"  # no new run


def test_claim_task_rejects_archived_with_completed_at(kanban_home):
    """An ``archived`` task that kept its completed_at (archived after done)
    and whose status is corrupted to ``ready`` must not be re-claimed."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="archive", assignee="a")
        assert kb.complete_task(conn, t, result="ok")
        assert kb.archive_task(conn, t)
        task = kb.get_task(conn, t)
        assert task.status == "archived"
        assert task.completed_at is not None

        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, current_run_id=NULL WHERE id=?",
            (t,),
        )
        conn.commit()

        assert kb.claim_task(conn, t) is None
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.current_run_id is None
        run = kb.latest_run(conn, t)
        assert run is not None and run.outcome == "completed"  # no new run


def test_claim_task_still_claims_genuine_ready_task(kanban_home):
    """No false positives: a live (never-terminated) ready task is still
    claimable, so real ready/running work is unaffected."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="live", assignee="a")
        # Sanity: completed_at is NULL for a never-done task.
        assert kb.get_task(conn, t).completed_at is None
        claimed = kb.claim_task(conn, t)
        assert claimed is not None
        task = kb.get_task(conn, t)
        assert task.status == "running"
        assert task.current_run_id == claimed.current_run_id
        # A genuine preserved run id was allocated.
        assert kb.latest_run(conn, t) is not None


def test_dispatch_once_does_not_respawn_terminal_task(kanban_home, monkeypatch):
    """End-to-end: ``dispatch_once`` must not spawn a terminal task even when
    its status was corrupted to ``ready``, while still spawning a genuine
    ready task in the same tick."""
    import hermes_cli.kanban_db as _kb

    spawned = []
    _fake_pid = 424242

    def fake_spawn(*args, **kwargs):
        spawned.append(args)
        # A real spawn returns the worker pid (int); mirror that exactly so the
        # dispatch loop's `_set_worker_pid(..., int(pid))` succeeds.
        return _fake_pid

    monkeypatch.setattr(_kb, "_default_spawn", fake_spawn)
    # Ensure the assignee passes the spawnable profile check so the loop reaches
    # the spawn path (the real resolution is covered elsewhere).
    import hermes_cli.profiles as _profiles
    monkeypatch.setattr(_profiles, "profile_exists", lambda name: True)
    # Bypass the separate `recent_success` respawn guard so this test isolates
    # exactly the `completed_at IS NULL` claim guard under test (a freshly
    # completed task would otherwise be deferred by that unrelated cooldown).
    monkeypatch.setattr(_kb, "check_respawn_guard", lambda conn, tid: None)
    # Bypass the unrelated MODEL_LOCK route-resolution gate (live profile +
    # model route wiring is covered elsewhere) so this test reaches the claim
    # step that carries the fix.
    monkeypatch.setattr(
        _kb, "resolve_task_root_route",
        lambda conn, tid, persist=False, allow_unpinned=False: ((tid,), "prof", "prof-model"),
    )

    with kb.connect() as conn:
        # Terminal task whose status was corrupted to ready.
        term = kb.create_task(conn, title="term", assignee="prof")
        assert kb.complete_task(conn, term, result="ok")
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL WHERE id=?", (term,),
        )
        # Genuine ready task that must still spawn.
        live = kb.create_task(conn, title="live", assignee="prof")
        conn.commit()

        _kb.dispatch_once(conn)

    # Only the live task was spawned; the terminal task was skipped entirely.
    spawned_titles = []
    with kb.connect() as conn:
        for _claimed, _workspace in spawned:
            spawned_titles.append(kb.get_task(conn, _claimed.id).title)
    assert "live" in spawned_titles
    assert "term" not in spawned_titles
    assert len(spawned_titles) == 1
    # The terminal task was not advanced to running; no new run was created.
    with kb.connect() as conn:
        assert kb.get_task(conn, term).status == "ready"
        run = kb.latest_run(conn, term)
        assert run is not None and run.outcome == "completed"  # terminal run only

# ---------------------------------------------------------------------------
# Daytime worker reserve (t_b12721ab)
# ---------------------------------------------------------------------------

def _reserve_cfg_dict(**overrides):
    """Build a valid kanban config dict carrying the daytime reserve block."""
    cfg = {
        "kanban": {
            "max_in_progress": 8,
            "daytime_reserve_slots": 2,
            "daytime_reserve_window": ["08:00", "20:00"],
            "daytime_reserve_tz": "Europe/Moscow",
            "daytime_reserve_priority_below": 0,
        },
    }
    kanban = cfg["kanban"]
    for key, val in overrides.items():
        kanban[key] = val
    return cfg


def _patch_daytime_reserve(monkeypatch, now, cfg_dict=None):
    """Pin the reserve clock to ``now`` (tz-aware) and stub config read.

    ``daytime_reserve_config()`` reads via ``load_config_readonly``; the
    dispatcher must see the synthetic kanban block. ``now`` drives
    ``_daytime_reserve_now`` so the window check is deterministic.
    """
    import hermes_cli.config as hcfg
    from datetime import datetime, timezone

    if cfg_dict is None:
        cfg_dict = _reserve_cfg_dict()
    monkeypatch.setattr(hcfg, "load_config_readonly", lambda: cfg_dict)
    if now is None:
        now = datetime.now(timezone.utc)
    monkeypatch.setattr(kb, "_daytime_reserve_now", lambda tz: now)
    # Bypass the unrelated MODEL_LOCK route-resolution and respawn cooldown
    # gates (same hermetic pattern as the existing dispatch tests) so these
    # tests exercise exactly the reserve ceiling, not route wiring.
    monkeypatch.setattr(
        kb, "resolve_task_root_route",
        lambda conn, tid, persist=False, allow_unpinned=False: ((tid,), "prof", "prof-model"),
    )
    monkeypatch.setattr(kb, "check_respawn_guard", lambda conn, tid: None)
    return now


def test_daytime_reserve_window_msk_boundary():
    """10:00 MSK is inside the window; 22:00 MSK is outside; edges inclusive/exclusive."""
    from datetime import datetime, time as clock_time
    from zoneinfo import ZoneInfo

    cfg = {
        "window_start": clock_time(8, 0),
        "window_end": clock_time(20, 0),
        "tz": ZoneInfo("Europe/Moscow"),
        "priority_below": 0,
    }

    def _msk(y=2026, mo=5, d=10, h=0, mi=0):
        return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("Europe/Moscow"))

    assert kb.in_daytime_window(cfg, now=_msk(h=10, mi=0)) is True
    assert kb.in_daytime_window(cfg, now=_msk(h=22, mi=0)) is False
    assert kb.in_daytime_window(cfg, now=_msk(h=7, mi=59)) is False
    assert kb.in_daytime_window(cfg, now=_msk(h=8, mi=0)) is True   # start inclusive
    assert kb.in_daytime_window(cfg, now=_msk(h=19, mi=59)) is True
    assert kb.in_daytime_window(cfg, now=_msk(h=20, mi=0)) is False  # end exclusive
    # Non-MSK wall clock is clamped into the reserve tz before comparison.
    assert kb.in_daytime_window(
        cfg, now=datetime(2026, 5, 10, 7, 0, tzinfo=ZoneInfo("UTC")),
    ) is True  # 07:00 UTC == 10:00 MSK


def test_daytime_reserve_config_parses_valid_block(monkeypatch):
    """A configured block returns a normalised dict; empty/invalid -> None."""
    import hermes_cli.config as hcfg

    cfg = _reserve_cfg_dict()
    monkeypatch.setattr(hcfg, "load_config_readonly", lambda: cfg)
    parsed = kb.daytime_reserve_config()
    assert parsed is not None
    assert parsed["slots"] == 2
    assert parsed["window_start"].hour == 8
    assert parsed["window_end"].hour == 20
    assert parsed["priority_below"] == 0
    assert str(parsed["tz"]) == "Europe/Moscow"

    # Missing block -> None (no reservation).
    monkeypatch.setattr(hcfg, "load_config_readonly", lambda: {"kanban": {}})
    assert kb.daytime_reserve_config() is None
    # Partially invalid -> None.
    monkeypatch.setattr(
        hcfg, "load_config_readonly",
        lambda: _reserve_cfg_dict(daytime_reserve_slots=0),
    )
    assert kb.daytime_reserve_config() is None
    monkeypatch.setattr(
        hcfg, "load_config_readonly",
        lambda: _reserve_cfg_dict(daytime_reserve_window="08:00"),
    )
    assert kb.daytime_reserve_config() is None


def test_dispatch_daytime_reserve_background_capped_in_window(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """In-window: background ready cards (priority<0) may not exceed
    max_in_progress - slots = 6; the extra ones land in reserved_by_daytime."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _patch_daytime_reserve(
        monkeypatch,
        now=datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        cfg_dict=_reserve_cfg_dict(),
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        bg_ids = []
        for i in range(8):
            bg_ids.append(
                kb.create_task(
                    conn, title=f"bg-{i}", assignee="alice", priority=-1,
                )
            )
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    assert len(res.spawned) == 6
    assert len(res.reserved_by_daytime) == 2
    assert set(res.reserved_by_daytime) <= set(bg_ids)
    assert not res.skipped_unassigned
    assert not res.skipped_nonspawnable


def test_dispatch_daytime_reserve_background_capped_in_window_real(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Same ceiling on the REAL (non-dry-run) spawn path."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _patch_daytime_reserve(
        monkeypatch,
        now=datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        cfg_dict=_reserve_cfg_dict(),
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for i in range(8):
            kb.create_task(conn, title=f"bg-{i}", assignee="alice", priority=-1)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=False,
        )

    assert len(res.spawned) == 6
    assert len(res.reserved_by_daytime) == 2
    assert len(spawns) == 6


def test_dispatch_daytime_reserve_priority_passes_to_full_pool(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """In-window: priority>=0 ready cards are NOT limited by the reserve — a
    fully priority board still spawns up to the whole pool (8)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _patch_daytime_reserve(
        monkeypatch,
        now=datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        cfg_dict=_reserve_cfg_dict(),
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for i in range(8):
            kb.create_task(conn, title=f"urg-{i}", assignee="alice", priority=5)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    assert len(res.spawned) == 8
    assert not res.reserved_by_daytime


def test_dispatch_daytime_reserve_inactive_outside_window(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Outside the window (22:00 MSK) the reserve is OFF: background ready
    cards occupy the whole pool up to max_in_progress (8)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _patch_daytime_reserve(
        monkeypatch,
        now=datetime(2026, 5, 10, 22, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        cfg_dict=_reserve_cfg_dict(),
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for i in range(8):
            kb.create_task(conn, title=f"bg-{i}", assignee="alice", priority=-1)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8, dry_run=True,
        )

    assert len(res.spawned) == 8
    assert not res.reserved_by_daytime


def test_dispatch_daytime_reserve_inactive_without_pool(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """No explicit positive max_in_progress pool -> no reservation (fails open)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _patch_daytime_reserve(
        monkeypatch,
        now=datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        cfg_dict=_reserve_cfg_dict(),
    )

    def fake_spawn(task, workspace, board=None):
        return 42

    with kb.connect() as conn:
        for i in range(8):
            kb.create_task(conn, title=f"bg-{i}", assignee="alice", priority=-1)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=None, dry_run=True,
        )

    assert not res.reserved_by_daytime

# ---------------------------------------------------------------------------
# active_run() — Kanban WebUI live-run lookup (WebUI /api/operations 500 fix)
# ---------------------------------------------------------------------------


def test_active_run_returns_claimed_run_until_completed(kanban_home):
    """Once a task is claimed, ``active_run`` reports the live run; after
    completion it returns None while ``latest_run`` still resolves it."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="active-run-probe", assignee="a")
        assert kb.active_run(conn, t) is None  # never claimed yet

        claimed = kb.claim_task(conn, t)
        assert claimed is not None
        active = kb.active_run(conn, t)
        assert active is not None
        assert active.id == claimed.current_run_id
        assert active.outcome is None  # not yet terminal

        assert kb.complete_task(conn, t, result="ok")
        assert kb.active_run(conn, t) is None  # no live run remains
        run = kb.latest_run(conn, t)
        assert run is not None and run.outcome == "completed"


def test_active_run_ignores_older_completed_runs(kanban_home):
    """A task with a previously completed run and no new claim must not
    report a stale run as active."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="active-run-stale", assignee="a")
        claimed = kb.claim_task(conn, t)
        assert claimed is not None
        assert kb.complete_task(conn, t, result="ok")
        # Re-query: latest_run exists but active_run must be None.
        assert kb.active_run(conn, t) is None
        assert kb.latest_run(conn, t) is not None


# ---------------------------------------------------------------------------
# API/provider-unavailable requeue (kanban provider-outage policy).
#
# A worker that bails with KANBAN_PROVIDER_UNAVAILABLE_EXIT_CODE (74) did so
# because the provider/API was down, not because the task failed. The FIRST
# KANBAN_API_UNAVAILABLE_RETRY_LIMIT (=3) consecutive occurrences for the same
# task auto-return it to its source phase WITHOUT counting into
# ``consecutive_failures`` — so the regular failure_limit=2 circuit breaker can
# never trip on a provider outage. Only on the 4th+ consecutive occurrence is
# the user asked (blocked + needs_input, existing question text). The repeat
# count lives in ``tasks.api_unavailable_streak`` (persisted across dispatcher
# restarts) and is reset on successful completion / operator reclaim.
# ---------------------------------------------------------------------------


def _provider_unavailable_hit(conn, tid, n, *, host):
    """Claim → repoint at a dead pid → record an exit-74 reap → run one
    ``detect_crashed_workers`` pass. Returns the pid used."""
    import hermes_cli.kanban_db as _kb

    assert kb.claim_task(conn, tid, claimer=f"{host}:w{n}")
    pid = 90000 + n
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    _kb._record_worker_exit(
        pid, _exited_status(_kb.KANBAN_PROVIDER_UNAVAILABLE_EXIT_CODE)
    )
    assert tid not in kb.detect_crashed_workers(conn)  # never a regular crash
    return pid


def test_provider_unavailable_first_three_auto_return_to_ready(
    kanban_home, monkeypatch,
):
    """Occurrences 1..3 of provider_unavailable for one task go back to
    ``ready`` — not ``blocked`` — with no failure counted and no user
    question; the repeat streak is persisted in the DB column."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    # Stub route preflight: these unit tests exercise detect_crashed_workers,
    # not create_task route validation against the live host.
    monkeypatch.setattr(_kb, "_provider_route_blocker", lambda provider, model: None)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = _kb._claimer_id().split(":", 1)[0]

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="api-down", assignee="a",
                             model_override="deepseek-v4-flash",
                             provider_override="letovo")
        for i in range(3):
            _provider_unavailable_hit(conn, tid, i, host=host)
            # Auto-returned requeues are surfaced via the side-channel (they
            # are NOT crashes), mirroring the rate_limited reporting pattern.
            api_na = getattr(
                _kb.detect_crashed_workers, "_last_api_unavailable", []
            )
            assert tid in api_na
            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.block_kind is None
            assert task.consecutive_failures == 0, (
                f"hit {i}: provider outage must NOT count a failure, "
                f"got {task.consecutive_failures}"
            )
            streak = conn.execute(
                "SELECT api_unavailable_streak FROM tasks WHERE id=?", (tid,),
            ).fetchone()[0]
            assert streak == i + 1

        # No blocked-with-question event, no delivery_pending on this path.
        blocked = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND kind='blocked'",
            (tid,),
        ).fetchall()
        assert blocked == []
        # The requeued run is closed with the api_unavailable outcome.
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,),
        ).fetchall()]
        assert outcomes == ["api_unavailable"] * 3
        assert "crashed" not in outcomes


def test_provider_unavailable_fourth_repeat_blocks_with_question(
    kanban_home, monkeypatch,
):
    """After three auto-returns without success, the 4th consecutive
    provider_unavailable for the SAME task asks the user (blocked +
    needs_input) with the existing question text, and the regular breaker
    never tripped (consecutive_failures stays 0)."""
    import json
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    # Stub route preflight: these unit tests exercise detect_crashed_workers,
    # not create_task route validation against the live host.
    monkeypatch.setattr(_kb, "_provider_route_blocker", lambda provider, model: None)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = _kb._claimer_id().split(":", 1)[0]

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="api-down-4", assignee="a",
                             model_override="deepseek-v4-flash",
                             provider_override="letovo")
        for i in range(3):
            _provider_unavailable_hit(conn, tid, i, host=host)
            assert kb.get_task(conn, tid).status == "ready"

        # 4th consecutive occurrence for the same task → ask the user.
        _provider_unavailable_hit(conn, tid, 3, host=host)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.consecutive_failures == 0, (
            "breaker must not trip through the provider-outage path"
        )
        assert "после повторов API всё ещё недоступен" in task.last_failure_error

        blocked = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='blocked'", (tid,),
        ).fetchall()
        assert len(blocked) == 1
        payload = json.loads(blocked[0]["payload"])
        assert payload["kind"] == "needs_input"
        assert payload["provider_route"] == "letovo/deepseek-v4-flash"

        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,),
        ).fetchall()]
        assert outcomes[:3] == ["api_unavailable"] * 3
        assert outcomes[3] == "blocked"


def test_provider_unavailable_streak_persists_and_completion_resets(
    kanban_home, monkeypatch,
):
    """The repeat counter is backed by a DB column (survives a dispatcher
    restart / fresh connection), and a successful ``complete_task`` resets it
    so the next outage gets a full 3-attempt budget."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    # Stub route preflight: these unit tests exercise detect_crashed_workers,
    # not create_task route validation against the live host.
    monkeypatch.setattr(_kb, "_provider_route_blocker", lambda provider, model: None)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = _kb._claimer_id().split(":", 1)[0]

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="streak", assignee="a",
                             model_override="deepseek-v4-flash",
                             provider_override="letovo")
        _provider_unavailable_hit(conn, tid, 0, host=host)
        _provider_unavailable_hit(conn, tid, 1, host=host)
        assert kb.get_task(conn, tid).status == "ready"
        assert conn.execute(
            "SELECT api_unavailable_streak FROM tasks WHERE id=?", (tid,),
        ).fetchone()[0] == 2

    # Fresh connection ≈ dispatcher restart: streak comes from the DB.
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert conn.execute(
            "SELECT api_unavailable_streak FROM tasks WHERE id=?", (tid,),
        ).fetchone()[0] == 2
        # Still inside budget: hit #3 auto-returns.
        _provider_unavailable_hit(conn, tid, 2, host=host)
        assert kb.get_task(conn, tid).status == "ready"

        # Successful completion resets the counter.
        assert kb.claim_task(conn, tid, claimer=f"{host}:w3")
        assert kb.complete_task(conn, tid, result="ok")
        assert kb.get_task(conn, tid).status == "done"
        assert conn.execute(
            "SELECT api_unavailable_streak FROM tasks WHERE id=?", (tid,),
        ).fetchone()[0] == 0


def test_provider_unavailable_streak_reset_on_unblock(kanban_home, monkeypatch):
    """A human unblocking the card is operator intervention — it resets the
    API-outage streak so the next attempt gets a fresh 3-attempt budget
    instead of re-asking immediately."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    # Stub route preflight: these unit tests exercise detect_crashed_workers,
    # not create_task route validation against the live host.
    monkeypatch.setattr(_kb, "_provider_route_blocker", lambda provider, model: None)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = _kb._claimer_id().split(":", 1)[0]

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="streak-unblock", assignee="a",
                             model_override="deepseek-v4-flash",
                             provider_override="letovo")
        for i in range(4):
            _provider_unavailable_hit(conn, tid, i, host=host)
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        assert conn.execute(
            "SELECT api_unavailable_streak FROM tasks WHERE id=?", (tid,),
        ).fetchone()[0] == 0


def test_regular_crash_still_blocks_at_failure_limit_two(
    kanban_home, monkeypatch,
):
    """Regression: a real crash (nonzero exit) is unaffected by the new
    provider-outage policy — it still trips the breaker at failure_limit=2."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    # Stub route preflight: these unit tests exercise detect_crashed_workers,
    # not create_task route validation against the live host.
    monkeypatch.setattr(_kb, "_provider_route_blocker", lambda provider, model: None)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = _kb._claimer_id().split(":", 1)[0]

    def crash(conn, tid, n):
        assert kb.claim_task(conn, tid, claimer=f"{host}:c{n}")
        pid = 92000 + n
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        _kb._record_worker_exit(pid, _exited_status(2))  # real nonzero crash
        return kb.detect_crashed_workers(conn)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="crash-loop", assignee="a")
        assert tid in crash(conn, tid, 0)
        t = kb.get_task(conn, tid)
        assert t.status == "ready" and t.consecutive_failures == 1
        assert tid in crash(conn, tid, 1)
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"  # DEFAULT_FAILURE_LIMIT == 2 reached
        assert t.consecutive_failures == 2


def test_dispatch_once_reports_api_unavailable_in_result(
    kanban_home, monkeypatch,
):
    """``DispatchResult.api_unavailable`` is populated by ``dispatch_once``:
    an exit-74 requeue inside the full tick surfaces on the result object
    (not only on the private side-channel), so telemetry / dashboards see
    it, mirroring ``rate_limited`` reporting."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    # Stub route preflight: these unit tests exercise detect_crashed_workers,
    # not create_task route validation against the live host.
    monkeypatch.setattr(_kb, "_provider_route_blocker", lambda provider, model: None)
    # The assignee-aware spawn pre-flight resolves the card's pinned route in
    # the ASSIGNEE profile's filesystem config — under the synthetic-assignee
    # registry that would raise FileNotFoundError for the real (non-existent)
    # profile. This tick's intent is the provider-outage requeue reporting, not
    # spawn-route validation, so accept any pinned route as usable (same
    # hermetic pattern as the ``_provider_route_blocker`` stub above).
    monkeypatch.setattr(
        _kb, "resolve_assignee_spawn_route",
        lambda assignee, provider, model: (provider, model, None),
    )
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    # Stub the actual subprocess launcher: after this tick reclaims the task
    # back to ``ready``, the ready lane re-spawns it in the same pass. Mirror a
    # real spawn's int-pid return, but do not launch any subprocess.
    monkeypatch.setattr(_kb, "_default_spawn", lambda task, workspace, board=None: 424242)
    monkeypatch.setattr(_kb, "check_respawn_guard", lambda conn, tid: None)
    monkeypatch.setattr(_kb, "resolve_task_root_route",
                        lambda conn, tid, persist=False, allow_unpinned=False:
                        ((tid,), "prof", "prof-model"))

    host = _kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="api-down-tick", assignee="a",
                             model_override="deepseek-v4-flash",
                             provider_override="letovo")
        # Leave the task ``running`` with a dead pid reaped as exit 74 so THIS
        # dispatch tick performs the provider-outage requeue itself (streak
        # 0 -> 1, within the retry limit: auto-return to ready, not blocked).
        assert kb.claim_task(conn, tid, claimer=f"{host}:w0")
        fake_pid = 909900
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (fake_pid, tid))
        conn.commit()
        _kb._record_worker_exit(
            fake_pid, _exited_status(_kb.KANBAN_PROVIDER_UNAVAILABLE_EXIT_CODE)
        )

        res = _kb.dispatch_once(conn)
        # The requeue was reported on the result object, and did NOT trip
        # the breaker or get counted as a failure/crash.
        assert tid in res.api_unavailable
        assert tid not in res.crashed
        assert tid not in res.rate_limited
        assert tid not in res.auto_blocked
        t = kb.get_task(conn, tid)
        assert t.consecutive_failures == 0



# ---------------------------------------------------------------------------
# do_not_deliver (t_82d6df66): delivery-suppression flag for synthetic cards.
# Synthetic readback cards (F7 fixtures, watcher-created bookkeeping cards)
# must be created with the flag set; ordinary cards must never carry it.
# ---------------------------------------------------------------------------


def test_create_task_do_not_deliver_flag_defaults_false(kanban_home):
    """An ordinary card created without the flag must NOT carry it.

    ``do_not_deliver`` defaults to False so normal card creation semantics
    (delivery as usual) are preserved exactly.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ordinary card", assignee="worker")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.do_not_deliver is False

        row = conn.execute(
            "SELECT do_not_deliver FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row is not None
        assert row["do_not_deliver"] == 0


def test_create_task_do_not_deliver_flag_set_for_synthetic(kanban_home):
    """A synthetic readback card created with the flag carries it.

    The synthetic readback creation path passes ``do_not_deliver=True`` so
    terminal/branch-summary delivery routing can skip the card.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="[F7 readback] mission A (host stays open)",
            assignee="01-orchestrator",
            initial_status="blocked",
            do_not_deliver=True,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.do_not_deliver is True

        row = conn.execute(
            "SELECT do_not_deliver FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row is not None
        assert row["do_not_deliver"] == 1


def test_create_task_do_not_deliver_false_explicit(kanban_home):
    """Explicit ``do_not_deliver=False`` is stored as 0 (normal card).

    Even when the flag is explicitly set false, the card remains a normal
    delivery subject — same as legacy behaviour.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="explicit normal", assignee="worker",
            do_not_deliver=False,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.do_not_deliver is False

        row = conn.execute(
            "SELECT do_not_deliver FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["do_not_deliver"] == 0


def test_schema_has_do_not_deliver_with_default(kanban_home):
    """The tasks table exposes ``do_not_deliver`` (default 0).

    Fresh DBs get the column from SCHEMA_SQL; the additive migration must
    idempotently add it for legacy DBs as well.
    """
    with kb.connect() as conn:
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
        }
        assert "do_not_deliver" in cols

        # The column has a NOT NULL DEFAULT 0 declaration.
        col = conn.execute(
            "PRAGMA table_info(tasks)"
        ).fetchall()
        do_not_deliver_col = next(
            (row for row in col if row["name"] == "do_not_deliver"), None
        )
        assert do_not_deliver_col is not None
        assert "0" in (do_not_deliver_col["dflt_value"] or "")
        assert do_not_deliver_col["notnull"] == 1
