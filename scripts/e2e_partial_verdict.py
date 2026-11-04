"""E2E proof for the partial-verdict channel (t_417a182e, reissued for t_48696422).

Runs against a REAL sqlite board created in a TEMP dir, a REAL claimed+bound run
and the REAL ``agent.kanban_soft_finish.force_complete`` path. Nothing about the
board, the comments or the close is mocked.

Why this file was reissued (t_6c41691e D3)
------------------------------------------
The previous version isolated the board with ``HERMES_KANBAN_HOME`` only. That
is not enough: ``kanban_db.kanban_db_path()`` gives ``HERMES_KANBAN_DB`` the
highest precedence, and the dispatcher exports that variable into every worker.
So the script silently opened the LIVE board and created six phantom cards
under ``09-independent-reviewer``, which then occupied the «recent work» block
of every later worker's context.

This version therefore:
  * unsets ``HERMES_KANBAN_DB`` (and the other board-locating vars) BEFORE the
    board module is imported or ``init_db()`` runs;
  * ASSERTS that ``kanban_db.kanban_db_path()`` resolves under the temp dir,
    and aborts before touching anything if it does not;
  * snapshots the live board's shape before and after, and fails if it moved —
    so «this proof leaves the production board untouched» is measured, not
    claimed.

Scenarios
---------
  1. a review run commits each decided item as it decides it, in the wording a
     real reviewer used («**Промежуточный вердикт … часть 1/3**»), not only the
     tidy ``verdict:`` form;
  2. the run burns its budget mid-check and never calls ``kanban_complete``;
  3. ``force_complete`` closes the card and the close CARRIES those items;
  4. an ordinary mid-work note and a foreign author's verdict do NOT leak in;
  5. a negated opening («Вердикт не вынесен…») records ZERO items (D1);
  6. a SECOND attempt on the same card inherits nothing into
     ``verdict_items_recorded`` (D2);
  7. the scratch workspace is still cleaned up and the verdict file survives as
     an attachment (t_24120021).

Usage:  venv/Scripts/python.exe scripts/e2e_partial_verdict.py [--control]
Exit 0 = every check behaved as documented.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name + (f": {detail}" if detail else ""))


# ── production-board fingerprint (read-only, taken before and after) ────────
def live_board_path() -> Path | None:
    """The live board the DISPATCHER would hand a worker, or None.

    Read from the inherited environment BEFORE we clear it, so the guard below
    watches the exact board this script could have polluted.
    """
    raw = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    if raw:
        return Path(raw).expanduser()
    home = (os.environ.get("HERMES_KANBAN_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "kanban.db"
    return Path.home() / "AppData" / "Local" / "hermes" / "kanban.db"


def fingerprint(path: Path | None) -> tuple | None:
    """Shape of the board, cheap and stable. ``None`` when it is not there."""
    if path is None or not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tasks = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            comments = con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
            links = con.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]
            max_id = con.execute("SELECT COALESCE(MAX(id), '') FROM tasks").fetchone()[0]
        finally:
            con.close()
        return (tasks, comments, links, max_id)
    except Exception:
        return None


def main() -> int:
    live_path = live_board_path()
    live_before = fingerprint(live_path)
    print(f"live board under watch: {live_path}  shape={live_before}")

    home = Path(tempfile.mkdtemp(prefix="t_48696422_e2e_"))
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    # HERMES_KANBAN_DB OUTRANKS HERMES_KANBAN_HOME in kanban_db_path(); leaving
    # it set is exactly how the previous script wrote 6 cards to production.
    for var in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD", "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_RUN_STARTED_AT",
    ):
        os.environ.pop(var, None)

    try:
        from hermes_cli import kanban_db as kb

        # Isolation must be PROVEN before init_db() can create anything.
        resolved = Path(kb.kanban_db_path()).expanduser().resolve()
        home_resolved = home.resolve()
        print(f"resolved board: {resolved}")
        check("board path is inside the temp dir (pre-init_db guard)",
              home_resolved in resolved.parents or resolved.parent == home_resolved,
              f"{resolved} not under {home_resolved}")
        if FAILURES:
            print("\nABORT: refusing to run against a non-isolated board.")
            return 1

        kb.init_db()
        conn = kb.connect()
        kb._validate_assignee_profile = lambda a: a  # type: ignore[assignment]

        REVIEWER = "09-independent-reviewer"
        PART_1 = (
            "**Промежуточный вердикт (пишу по частям, чтобы не потерять при обрыве "
            "бюджета) — часть 1/3: что подтверждено инструментом**\n\n"
            "Объект: sno-economics-report. Подтверждено: 12/12 чисел сходятся."
        )
        PART_2 = (
            "ВЕРДИКТ НЕЗАВИСИМОЙ ПРИЁМКИ — часть 2/2, финал\n\n"
            "Статус: revision_required. Числовой слой принят, ссылки требуют правки."
        )
        CHATTER = "Сейчас перепроверяю структуру таблиц, вердикт будет позже."
        FOREIGN = "verdict: чужой профиль не должен попасть в чужой handoff"

        # ── attempt 1: two decided items, then the budget dies ──────────────
        tid = kb.create_task(conn, title="ФИНАЛ: независимая приёмка", assignee=REVIEWER)
        task = kb.claim_task(conn, tid)
        assert task is not None, "claim failed"
        run1 = task.current_run_id
        kb.bind_worker_session(conn, tid, run1, REVIEWER, "e2e-sess-1")

        ws = kb.resolve_workspace(kb.get_task(conn, tid))
        kb.set_workspace_path(conn, tid, ws)
        verdict_file = ws / "VERDICT_t_f1581cbb.md"
        verdict_file.write_text("item-level verdict", encoding="utf-8")

        kb.add_comment(conn, tid, author=REVIEWER, body=PART_1)
        kb.add_comment(conn, tid, author=REVIEWER, body=CHATTER)
        kb.add_comment(conn, tid, author="03-software-engineer", body=FOREIGN)
        kb.add_comment(conn, tid, author=REVIEWER, body=PART_2)
        # A negated opening: this run ALSO said, at one point, that it had no
        # verdict. It must never be counted as one (D1).
        kb.add_comment(conn, tid, author=REVIEWER,
                       body="Вердикт не вынесен по пункту 4: бюджет исчерпан.")

        os.environ.update({
            "HERMES_KANBAN_TASK": tid,
            "HERMES_KANBAN_RUN_ID": str(run1),
            "HERMES_KANBAN_DB": str(resolved),
            "HERMES_PROFILE": REVIEWER,
            "HERMES_SESSION_ID": "e2e-sess-1",
            "HERMES_KANBAN_WORKSPACE": str(ws),
        })

        messages = [
            {"role": "user", "content": f"work kanban task {tid}"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c0", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps(
                                 {"path": str(verdict_file).replace("\\", "/")})},
            }]},
            {"role": "tool", "tool_call_id": "c0", "name": "write_file",
             "content": '{"bytes_written": 20}'},
            {"role": "assistant",
             "content": "Now re-verifying the key numeric and structural claims "
                        "with fresh independent scripts before I issue any verdict."},
        ]
        budget = {"steps_used": 156, "steps_max": 180, "fraction": 0.8667,
                  "time_elapsed": None, "time_limit": None}

        # ── CONTROL: the pre-fix prefix-only harvester ──────────────────────
        from agent import kanban_soft_finish as soft

        if "--control" in sys.argv:
            _real = soft._harvest_verdict_notes

            def _prefix_only(task_id=None):
                env_tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
                profile = (os.environ.get("HERMES_PROFILE") or "").strip()
                out = []
                c2 = kb.connect()
                try:
                    for c in kb.list_comments(c2, env_tid):
                        body = str(getattr(c, "body", "") or "").strip()
                        author = str(getattr(c, "author", "") or "").strip()
                        if profile and author and author != profile:
                            continue
                        if body.lower().startswith("verdict:"):
                            out.append(body[len("verdict:"):].strip())
                finally:
                    c2.close()
                return soft.VerdictHarvest(out, [], None, False)

            soft._harvest_verdict_notes = _prefix_only  # type: ignore[assignment]

        ok = soft.force_complete(reason="soft_finish_grace_expired",
                                 messages=messages, budget=budget)
        check("force_complete closed the card", ok is True)

        c2 = kb.connect()
        t1 = kb.get_task(c2, tid)
        run_row = c2.execute(
            "SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (run1,)
        ).fetchone()
        meta = json.loads(run_row["metadata"] or "{}")
        summary = run_row["summary"] or ""
        attachments = kb.list_attachments(c2, tid)
        c2.close()

        if "--control" in sys.argv:
            # The gap: prefix-only harvest loses BOTH real items.
            gap = (meta.get("verdict_items_recorded") == 0
                   and "часть 1/3" not in summary
                   and "revision_required" not in summary)
            check("CONTROL: prefix-only harvester loses both items (gap reproduced)", gap)
            print("\nRESULT:", "PASS (gap reproduced)" if (ok and gap) else "FAIL")
            return 0 if (ok and gap) else 1

        # ── what the close must contain ─────────────────────────────────────
        check("card is done", t1.status == "done", str(t1.status))
        check("this run's items recorded == 2", meta.get("verdict_items_recorded") == 2,
              json.dumps(meta, ensure_ascii=False))
        check("nothing inherited on a first attempt",
              meta.get("verdict_items_inherited") == 0, json.dumps(meta, ensure_ascii=False))
        check("boundary taken from the run row (env absent)",
              meta.get("verdict_boundary_known") is True
              and isinstance(meta.get("verdict_run_started_at"), int),
              json.dumps(meta, ensure_ascii=False))
        check("verdict_partial flagged", meta.get("verdict_partial") is True)
        check("part 1 survives the close", "часть 1/3" in summary)
        check("part 2 survives the close", "revision_required" in summary)
        check("foreign profile's verdict is not re-attributed",
              "чужой профиль" not in summary)
        check("mid-work note is not harvested", "перепроверяю структуру" not in summary)
        check("negated opening is not harvested as an item (D1)",
              "Вердикт не вынесен по пункту 4" not in summary)
        check("narration tail preserved", "re-verifying" in summary)
        check("verdict file preserved as an attachment",
              any(a.filename == "VERDICT_t_f1581cbb.md" for a in attachments))
        check("scratch workspace cleaned", not ws.exists())

        # ── attempt 2 on the SAME card: must inherit nothing (D2) ───────────
        #
        # ``task_comments.created_at`` has ONE-SECOND resolution, so an attempt
        # opened in the same second as the previous attempt's comments is
        # genuinely indistinguishable from them by the boundary. Real
        # re-dispatches never happen that fast (the dispatcher tick is far
        # coarser), so the proof lets the wall clock cross the boundary instead
        # of back-dating rows: that keeps the evidence honest.
        time.sleep(1.2)
        conn3 = kb.connect()
        # A re-dispatch reopens the card: status back to ``ready`` with
        # ``completed_at`` cleared (the documented reopen path — ``claim_task``
        # refuses a task whose ``completed_at`` is still set, and ``claim_task``
        # itself only transitions ready -> running).
        conn3.execute(
            "UPDATE tasks SET status='ready', completed_at=NULL, claim_lock=NULL, "
            "claim_expires=NULL WHERE id=?",
            (tid,),
        )
        conn3.commit()
        task2 = kb.claim_task(conn3, tid)
        if task2 is None:
            check("attempt 2 claimed", False, "claim_task returned None")
        else:
            run2 = task2.current_run_id
            kb.bind_worker_session(conn3, tid, run2, REVIEWER, "e2e-sess-2")
            os.environ.update({
                "HERMES_KANBAN_TASK": tid,
                "HERMES_KANBAN_RUN_ID": str(run2),
                "HERMES_SESSION_ID": "e2e-sess-2",
            })
            ok2 = soft.force_complete(reason="soft_finish_grace_expired",
                                      messages=[{"role": "assistant",
                                                 "content": "Второй прогон ничего не решил."}],
                                      budget=budget)
            check("attempt 2 closed", ok2 is True)
            row2 = conn3.execute(
                "SELECT summary, metadata FROM task_runs WHERE id = ?", (run2,)
            ).fetchone()
            meta2 = json.loads(row2["metadata"] or "{}")
            summary2 = row2["summary"] or ""
            check("attempt 2 records ZERO items of its own (D2)",
                  meta2.get("verdict_items_recorded") == 0,
                  json.dumps(meta2, ensure_ascii=False))
            check("attempt 2 reports the 2 earlier items as inherited",
                  meta2.get("verdict_items_inherited") == 2,
                  json.dumps(meta2, ensure_ascii=False))
            check("attempt 2 summary labels them as a PREVIOUS attempt",
                  soft._VERDICT_NOTE_INHERITED_HEADER in summary2
                  and "часть 1/3" in summary2)
            own_block = summary2.split(soft._VERDICT_NOTE_INHERITED_HEADER)[0]
            check("inherited items are not presented as this run's result",
                  "часть 1/3" not in own_block)
        conn3.close()
        conn.close()
    finally:
        shutil.rmtree(home, ignore_errors=True)

    # ── the production board must be untouched ─────────────────────────────
    live_after = fingerprint(live_path)
    check("live board untouched by the proof", live_after == live_before,
          f"{live_before} -> {live_after}")

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
