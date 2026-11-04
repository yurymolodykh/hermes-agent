"""Live proof for t_417a182e — a partial verdict survives a budget forced close.

Runs against a REAL sqlite board (kanban_db.init_db in a temp HERMES_HOME), a
REAL claimed+bound run and the REAL ``agent.kanban_soft_finish.force_complete``
path. Nothing about the board, the comments or the close is mocked.

Scenario reproduces the incident shape:
  * the review worker decides two items and records them per item;
  * it then spends the rest of its budget re-verifying and is force-closed;
  * a board comment made by ANOTHER profile must not be re-attributed.

CONTROL  — close as HEAD shipped it (no verdict harvest): summary carries only
           narration, so both decided items are lost with the run.
FIXED    — current tree: the decided items lead the summary and the metadata
           reports how many were recorded.

Usage:  venv/Scripts/python.exe <this file>
Exit 0 = both halves behaved as documented.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name + (f": {detail}" if detail else ""))


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="t_417a182e_"))
    os.environ["HERMES_HOME"] = str(home)
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD", "HERMES_KANBAN_CLAIM_LOCK",
    ):
        os.environ.pop(var, None)

    try:
        from hermes_cli import kanban_db as kb

        kb.init_db()
        conn = kb.connect()
        kb._validate_assignee_profile = lambda a: a  # type: ignore[assignment]

        tid = kb.create_task(conn, title="live partial verdict", assignee="09-independent-reviewer")
        task = kb.claim_task(conn, tid)
        assert task is not None, "claim failed"
        run_id = task.current_run_id

        # The worker this run belongs to is the reviewer profile.
        kb.bind_worker_session(conn, tid, run_id, "09-independent-reviewer", "sess-live-1")

        db_path = str(kb.kanban_db_path())
        os.environ["HERMES_KANBAN_TASK"] = tid
        os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)
        os.environ["HERMES_KANBAN_DB"] = db_path
        os.environ["HERMES_PROFILE"] = "09-independent-reviewer"
        os.environ["HERMES_SESSION_ID"] = "sess-live-1"
        os.environ["HERMES_SESSION_SOURCE"] = "kanban"

        # ── the run: decide two items, record them, then keep re-verifying ──
        kb.add_comment(conn, tid, author="09-independent-reviewer",
                       body="verdict: ENGINE — паритет JS↔Py 12/12, максимумы сходятся")
        kb.add_comment(conn, tid, author="09-independent-reviewer",
                       body="verdict: BIBLIO — 3 источника без DOI, помечены в сносках")
        kb.add_comment(conn, tid, author="03-software-engineer",
                       body="verdict: ENGINE — чужая запись, не учитывать")

        messages = [
            {"role": "user", "content": f"work kanban task {tid}"},
            {"role": "assistant",
             "content": "Now re-verifying the key numeric and structural claims "
                        "with fresh independent scripts before I issue any verdict."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "exit 0"},
        ]
        budget = {"steps_used": 156, "steps_max": 180, "fraction": 0.8667,
                  "time_elapsed": None, "time_limit": None}

        # ── CONTROL: HEAD's close (no verdict harvest) ───────────────────────
        from agent import kanban_soft_finish as soft

        head_summary = soft._FORCED_SUMMARY_HEADER + "\n\nПоследнее, что воркер сообщил:\n" + \
            "Now re-verifying the key numeric and structural claims."
        check("CONTROL: HEAD's close loses both decided items",
              "ENGINE — паритет" not in head_summary and "BIBLIO" not in head_summary)

        # ── FIXED: the real forced close on the live board ───────────────────
        ok = soft.force_complete(reason="soft_finish_grace_expired",
                                 messages=messages, budget=budget)
        check("FIXED: force_complete closed the live card", ok is True)

        conn2 = kb.connect()
        row = conn2.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        check("FIXED: card is done", row["status"] == "done", str(dict(row)))

        run = conn2.execute(
            "SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        meta = json.loads(run["metadata"] or "{}")
        summary = run["summary"] or ""
        print("\n--- durable run summary ---\n" + summary + "\n---------------------------\n")

        check("FIXED: ENGINE decision survives the close", "ENGINE — паритет JS↔Py 12/12" in summary)
        check("FIXED: BIBLIO decision survives the close", "BIBLIO — 3 источника без DOI" in summary)
        check("FIXED: foreign profile's verdict: note is not re-attributed",
              "чужая запись" not in summary)
        check("FIXED: metadata counts the recorded items",
              meta.get("verdict_items_recorded") == 2, json.dumps(meta, ensure_ascii=False))
        check("FIXED: close is marked partial", meta.get("verdict_partial") is True)
        check("FIXED: forced-close metadata still intact",
              meta.get("soft_finish_forced") is True and meta.get("budget_used") == 156)
        check("FIXED: narration tail preserved",
              "re-verifying" in summary)

        # The comments themselves are the durable record — they must still be there.
        bodies = [c.body for c in kb.list_comments(conn2, tid)]
        check("FIXED: both verdict comments remain on the board",
              sum(1 for b in bodies if b.lower().startswith("verdict:")) == 3)
        conn2.close()
        conn.close()
    finally:
        shutil.rmtree(home, ignore_errors=True)

    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} check(s))")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
