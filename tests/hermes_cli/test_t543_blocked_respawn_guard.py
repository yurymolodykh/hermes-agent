"""Direct empirical validation of blocked-task cyclic-respawn guards (t_543dd51f).

Validates the committed semantics of kanban_db against a scratch DB:

 G1. Worker/operator-initiated kanban_block stays blocked across many
     recompute_ready ticks (no auto-promote).
 G2. Circuit-breaker blocked (gave_up, consecutive_failures >= failure_limit)
     is NOT auto-promoted (no infinite retry loop).
 G3. An explicit unblock_task clears the sticky state and lets the task resume.
 G4. dispatch_once never spawns a blocked task (blocked not in ready rows).

Uses a synthetic assignee + patched profile_exists so the DB layer's
assignee validation passes without a real profile.
"""
from __future__ import annotations

import os, sys, tempfile, time
from pathlib import Path

import pytest  # noqa: F401  (pytest import ensures monkeypatch available in some runners)


def main() -> None:
    from hermes_cli import profiles, kanban_db as kb

    # Patch profile_exists so synthetic assignees pass validation.
    real_exists = profiles.profile_exists
    profiles.profile_exists = lambda name: True
    try:
        _run()
    finally:
        profiles.profile_exists = real_exists


def _run() -> None:
    from hermes_cli import kanban_db as kb

    home = Path(tempfile.mkdtemp(prefix="t543-"))
    # Point HERMES_HOME at the scratch home so the DB + generation files land there.
    os.environ["HERMES_HOME"] = str(home)
    kb.init_db()

    with kb.connect() as conn:
        assignee = "t543-worker"

        # ---- G1: sticky worker block is not auto-promoted -----------------
        tid = kb.create_task(conn, title="g1 sticky", assignee=assignee)
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="needs_input: human decision required",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked", "block_task must set blocked"
        promotions = 0
        for _ in range(10):
            promotions += kb.recompute_ready(conn, failure_limit=2)
        assert promotions == 0, f"G1 FAIL: sticky blocked task auto-promoted {promotions}x"
        print(f"G1 OK: sticky worker block survived 10 recompute_ready ticks (promoted=0)")

        # ---- G2: circuit-breaker blocked (failures >= limit) not promoted --
        tid2 = kb.create_task(conn, title="g2 breaker", assignee=assignee)
        kb.claim_task(conn, tid2)
        # Force a gave_up-style block event sequence: crash via _record_task_failure
        # until it trips. outcomes: use spawn failure path with failure_limit=2.
        kb._record_task_failure(
            conn, tid2, "boom 1",
            outcome="crashed", failure_limit=2, release_claim=False, end_run=False,  # noqa
        )
        # Drive the failure counter to the limit, then set blocked like the breaker does.
        # (Simulate the breaker outcome directly: status=blocked, gave_up event, count=limit.)
        kb._record_task_failure(
            conn, tid2, "boom 2",
            outcome="gave_up", failure_limit=2, release_claim=True, end_run=True,  # noqa
        )
        assert kb.get_task(conn, tid2).status == "blocked", "breaker must set blocked"
        promotions = 0
        for _ in range(10):
            promotions += kb.recompute_ready(conn, failure_limit=2)
        assert promotions == 0, f"G2 FAIL: breaker-blocked task auto-promoted"
        print(f"G2 OK: circuit-breaker blocked (failures>=2) never auto-promoted")

        # ---- G3: explicit unblock resumes the task -------------------------
        assert kb.unblock_task(conn, tid) is True, "unblock should succeed"
        assert kb.recompute_ready(conn, failure_limit=2) >= 0
        # unblock_task flips status directly; confirm it's no longer blocked
        assert kb.get_task(conn, tid).status in ("ready", "todo"), \
            f"G3 FAIL: after unblock status={kb.get_task(conn, tid).status}"
        print(f"G3 OK: explicit unblock restored task (status={kb.get_task(conn, tid).status})")

        # ---- G4: blocked task is not spawnable via dispatch logic ----------
        tid4 = kb.create_task(conn, title="g4 not spawnable", assignee=assignee)
        kb.claim_task(conn, tid4)
        kb.block_task(
            conn, tid4,
            reason="needs_input: blocked for g4",
            expected_run_id=kb.get_task(conn, tid4).current_run_id,
        )
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='ready' AND claim_lock IS NULL"
        ).fetchall()
        ids = {r["id"] for r in rows}
        assert tid4 not in ids, "G4 FAIL: blocked task appears in ready spawn set"
        print("G4 OK: blocked task absent from ready spawn set")

        # ---- G5: repeated circuit-breaker crash/timed_out loop after an
        #      unblock ESCALATES to triage, not back to blocked ------------
        # (t_543dd51f fix: crash/timed_out trips now feed block_recurrences;
        #  at BLOCK_RECURRENCE_LIMIT the task routes to triage — breaking the
        #  cron-unblock ↔ crash-reblock loop that resurrected gave_up cards.)
        tid5 = kb.create_task(conn, title="g5 crash loop", assignee=assignee)
        # Breaker trip #1: 2 consecutive crashes (failure_limit=2) -> blocked
        kb.claim_task(conn, tid5)
        kb._record_task_failure(
            conn, tid5, "pid crash 1a",
            outcome="crashed", failure_limit=2, release_claim=False, end_run=False,
        )
        kb._record_task_failure(
            conn, tid5, "pid crash 1b",
            outcome="crashed", failure_limit=2, release_claim=False, end_run=False,
        )
        assert kb.get_task(conn, tid5).status == "blocked", \
            f"G5 FAIL: trip 1 should block, got {kb.get_task(conn, tid5).status}"
        # External cron unblocks (resets consecutive_failures, KEEPS recurrences=1)
        assert kb.unblock_task(conn, tid5) is True
        assert kb.get_task(conn, tid5).status == "ready"
        # Breaker trip #2: same-circuit-breaker re-crash after unblock -> triage
        kb.claim_task(conn, tid5)
        kb._record_task_failure(
            conn, tid5, "pid crash 2a",
            outcome="crashed", failure_limit=2, release_claim=False, end_run=False,
        )
        kb._record_task_failure(
            conn, tid5, "pid crash 2b",
            outcome="crashed", failure_limit=2, release_claim=False, end_run=False,
        )
        t5 = kb.get_task(conn, tid5)
        assert t5.status == "triage", \
            f"G5 FAIL: 2nd breaker loop should escalate to triage, got {t5.status}"
        evs = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'block_loop_detected'", (tid5,)
        ).fetchall()
        assert evs, "G5 FAIL: triage escalation must emit block_loop_detected"
        # The cron cannot revive a triage card via unblock_task
        assert kb.unblock_task(conn, tid5) is False, \
            "G5 FAIL: unblock_task must refuse a triage card"
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='ready' AND claim_lock IS NULL"
        ).fetchall()
        ids = {r["id"] for r in rows}
        assert tid5 not in ids, "G5 FAIL: triage task appears in ready spawn set"
        print("G5 OK: repeated breaker loop after unblock escalates to triage "
              "(block_loop_detected emitted; not reviveable via unblock_task)")

        # ---- G6: a genuinely transient single breaker trip stays blocked
        #      (recoverable) — the loop-breaker does NOT over-escalate a
        #      first-time trip that was never unblocked.
        tid6 = kb.create_task(conn, title="g6 transient first-trip", assignee=assignee)
        kb.claim_task(conn, tid6)
        kb._record_task_failure(
            conn, tid6, "one-off timeout 1",
            outcome="timed_out", failure_limit=2, release_claim=False, end_run=False,
        )
        kb._record_task_failure(
            conn, tid6, "one-off timeout 2",
            outcome="timed_out", failure_limit=2, release_claim=False, end_run=False,
        )
        assert kb.get_task(conn, tid6).status == "blocked", \
            f"G6 FAIL: first break should stay blocked, got {kb.get_task(conn, tid6).status}"
        assert kb.unblock_task(conn, tid6) is True, "G6 FAIL: unblockable after first trip"
        print("G6 OK: first-time breaker trip stays blocked/recoverable (no over-escalation)")

    print("\nALL GUARDS VERIFIED (t_543dd51f): G1-G6 passed")


if __name__ == "__main__":
    main()
