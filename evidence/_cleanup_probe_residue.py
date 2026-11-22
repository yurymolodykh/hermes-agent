"""Remove every probe card + workspace this card's live-CLI probes created on the real board.

The probe now pins HERMES_KANBAN_HOME, but earlier runs before that fix wrote to the
operator's live board. This deletes exactly those cards (title starts with 'probe ')
and their scratch workspaces; nothing else is touched.
"""
import shutil
import sqlite3
from pathlib import Path

DB = "/home/hermes/.hermes/kanban.db"
WORKSPACES = Path("/home/hermes/.hermes/kanban/workspaces")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = list(conn.execute(
    "select id, title, workspace_path from tasks where title like 'probe %'"))
print(f"{len(rows)} probe card(s)")
for row in rows:
    tid = row["id"]
    for sql in ("delete from task_links where parent_id=? or child_id=?",
                "delete from task_comments where task_id=?",
                "delete from task_events where task_id=?",
                "delete from task_runs where task_id=?",
                "delete from kanban_notify_subs where task_id=?"):
        conn.execute(sql, (tid, tid) if "task_links" in sql else (tid,))
    conn.execute("delete from tasks where id=?", (tid,))
    ws = Path(row["workspace_path"]) if row["workspace_path"] else WORKSPACES / tid
    if ws.is_dir() and ws.parent == WORKSPACES:
        shutil.rmtree(ws, ignore_errors=True)
    print(f"  removed {tid} ({row['title']}); workspace left: {ws.exists()}")
conn.commit()

left = list(conn.execute("select id from tasks where title like 'probe %'"))
print("probe cards remaining:", len(left))
