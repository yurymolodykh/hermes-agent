"""Live smoke: an unreadable subdirectory is "cannot tell", so scratch cleanup preserves it."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORK = HERE.parents[2]
SITE = WORK.parents[1] / ".venv" / "lib" / "python3.14" / "site-packages"

home = HERE.parent / "d1home"
kanban_home = HERE.parent / "d1kanban"
home.mkdir(parents=True, exist_ok=True)
kanban_home.mkdir(parents=True, exist_ok=True)

for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_BOARD_PIN",
            "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_BRANCH"):
    os.environ.pop(var, None)
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_KANBAN_HOME"] = str(kanban_home)
sys.path.insert(0, str(WORK))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402

kb.init_db()
with kbc.connect() as conn:
    tid = kb.create_task(conn, title="d1 smoke", created_by="smoke", assignee="02-hermes-platform")
    conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
                 (str(kb.workspaces_root() / tid), tid))
    conn.commit()

ws = kb.workspaces_root() / tid
repo = ws / "mid" / "clone"
repo.mkdir(parents=True)
def git(*a: str) -> None:
    subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True)
git("init", "-b", "main", str(repo))
git("config", "user.email", "t@e.com")
git("config", "user.name", "t")
(repo / "only.txt").write_text("never pushed\n", encoding="utf-8")
git("add", "only.txt")
git("commit", "-m", "never pushed")
(ws / "neighbour.txt").write_text("readable\n", encoding="utf-8")

barrier = ws / "mid"
barrier.chmod(0o000)
try:
    with kbc.connect() as conn:
        kb.complete_task(conn, tid, result="done")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "workspace_cleanup_preserved_unsaved_work"]
finally:
    barrier.chmod(0o755)

print("CHECK workspace kept:", ws.is_dir())
print("CHECK readable neighbour intact:", (ws / "neighbour.txt").read_text(encoding="utf-8").strip() == "readable")
print("CHECK unpushed file intact:", (repo / "only.txt").read_text(encoding="utf-8").strip() == "never pushed")
print("CHECK exactly one preserved event:", len(events) == 1)
reason = events[0].payload["reason"] if events else ""
print("CHECK reason says it could not read:", "readable" in reason)
print("reason:", reason)
