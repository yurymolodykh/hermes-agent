"""Live smoke: `boards rm --delete` refuses over preserved scratch work, --force lists paths."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORK = Path(__file__).resolve().parents[2]  # the git worktree this card owns
REPO_ROOT = WORK.parents[1]  # the platform checkout that owns the shared .venv
SITE = REPO_ROOT / ".venv" / "lib" / "python3.14" / "site-packages"

home = HERE.parent / "home"
home.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, HERMES_HOME=str(home))
for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
    env.pop(var, None)
env["PYTHONPATH"] = f"{WORK}:{SITE}"
PY = sys.executable


def cli(*args: str) -> subprocess.CompletedProcess:
    r = subprocess.run([PY, "-m", "hermes_cli.main", "kanban", *args], env=env, cwd=str(WORK),
                       capture_output=True, text=True, timeout=180)
    print(f"$ hermes kanban {' '.join(args)}  -> exit {r.returncode}")
    print((r.stdout or "").strip())
    if r.stderr.strip():
        print("STDERR:", r.stderr.strip()[-600:])
    return r


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


sys.path.insert(0, str(WORK))
from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402

cli("boards", "create", "smokeboard")
with kbc.connect_closing(board="smokeboard") as conn:
    tid = kb.create_task(conn, title="smoke scratch")
ws = kb.workspaces_root(board="smokeboard") / tid
repo = ws / "clone"
repo.mkdir(parents=True)
git("init", "-b", "main", str(repo), cwd=repo)
git("config", "user.email", "t@e.com", cwd=repo)
git("config", "user.name", "t", cwd=repo)
(repo / "w.txt").write_text("only copy\n", encoding="utf-8")
git("add", "w.txt", cwd=repo)
git("commit", "-m", "never pushed", cwd=repo)
with kbc.connect_closing(board="smokeboard") as conn:
    conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?", (str(ws), tid))
    conn.commit()

board_dir = kb.board_dir("smokeboard")
print("board dir exists:", board_dir.is_dir(), board_dir)

refuse = cli("boards", "rm", "smokeboard", "--delete")
print("CHECK refused(rc!=0):", refuse.returncode != 0)
print("CHECK board intact:", board_dir.is_dir())
print("CHECK file intact:", (repo / "w.txt").read_text(encoding="utf-8").strip() == "only copy")
with kbc.connect_closing(board="smokeboard") as conn:
    kinds = [e.kind for e in kb.list_events(conn, tid)]
print("CHECK event recorded:", kinds.count("workspace_cleanup_preserved_unsaved_work"), kinds)

forced = cli("boards", "rm", "smokeboard", "--delete", "--force")
print("CHECK forced(rc==0):", forced.returncode == 0)
print("CHECK forced names the path:", str(ws) in (forced.stdout or ""))
print("CHECK board gone:", not board_dir.exists())
