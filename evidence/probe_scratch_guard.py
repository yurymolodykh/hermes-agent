"""Live end-to-end probe: complete a real scratch-workspace card through the CLI.

Runs the real ``hermes kanban`` command against an isolated ``HERMES_HOME``, so the
path exercised is create -> claim (which materializes the scratch workspace) ->
work -> complete -> ``_cleanup_workspace``, not the Python API the unit tests use.

Usage: ``python evidence/probe_scratch_guard.py`` — prints a JSON verdict and exits
non-zero when either case misbehaves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRESERVED = "workspace_cleanup_preserved_unsaved_work"


def _run(args: list[str], *, cwd: Path, env: dict, check: bool = True):
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"FAILED {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def _git(env: dict, cwd: Path, *args: str) -> str:
    return _run(["git", *args], cwd=cwd, env=env).stdout


def _hermes(env: dict, cwd: Path, *args: str, check: bool = True):
    return _run([sys.executable, "./hermes", "kanban", *args], cwd=REPO, env=env, check=check)


def _make_remote(env: dict, root: Path) -> tuple[Path, Path]:
    origin = root / "origin.git"
    _git(env, root, "init", "--bare", "-q", str(origin))
    seed = root / "seed"
    _git(env, root, "clone", "-q", str(origin), str(seed))
    _git(env, seed, "config", "user.email", "probe@example.com")
    _git(env, seed, "config", "user.name", "probe")
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(env, seed, "add", "README.md")
    _git(env, seed, "commit", "-qm", "init")
    _git(env, seed, "push", "-q", "origin", "HEAD")
    return origin, seed


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="scratch-guard-probe."))
    # health: allow PS-P05 -- measurement probe, not a served-profile child: every HERMES_*/kanban var that could point a child at live state is stripped below.
    env = dict(os.environ)
    env["HERMES_HOME"] = str(root / "hermes")
    # kanban_home() prefers HERMES_KANBAN_HOME and falls back to the OPERATOR's real
    # root on purpose (boards are shared across profiles), so a probe that only set
    # HERMES_HOME would write cards into the live board.
    env["HERMES_KANBAN_HOME"] = str(root / "kanban-home")
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO)

    origin, _seed = _make_remote(env, root)

    def fresh_card(title: str) -> tuple[str, Path]:
        """Create + claim a scratch card; the claim persists its workspace path."""
        created = _hermes(env, root, "create", title, "--workspace", "scratch",
                          "--json", check=False)
        if created.returncode != 0:
            raise SystemExit(f"create failed:\n{created.stdout}\n{created.stderr}")
        task_id = json.loads(created.stdout)["id"]
        claimed = _hermes(env, root, "claim", task_id, check=False)
        if claimed.returncode != 0:
            raise SystemExit(f"claim failed:\n{claimed.stdout}\n{claimed.stderr}")
        ws = Path(claimed.stdout.strip().split("Workspace: ", 1)[1].splitlines()[0])
        return task_id, ws

    def show(task_id: str) -> dict:
        out = _hermes(env, root, "show", task_id, "--json", check=False)
        if out.returncode != 0:
            raise SystemExit(f"show failed:\n{out.stdout}\n{out.stderr}")
        return json.loads(out.stdout)

    report: dict = {"hermes_home": str(env["HERMES_HOME"]), "repo": str(REPO), "cases": {}}

    # Case A: a clone with a commit that was never pushed -> directory must survive.
    task_a, ws_a = fresh_card("probe unsaved work")
    repo_a = ws_a / "clone"
    _git(env, root, "clone", "-q", str(origin), str(repo_a))
    _git(env, repo_a, "config", "user.email", "probe@example.com")
    _git(env, repo_a, "config", "user.name", "probe")
    (repo_a / "unpushed.txt").write_text("only here\n", encoding="utf-8")
    _git(env, repo_a, "add", "unpushed.txt")
    _git(env, repo_a, "commit", "-qm", "never pushed")
    done_a = _hermes(env, root, "complete", task_a, "--result", "probe done", "--force", check=False)
    events_a = show(task_a).get("events", [])
    report["cases"]["unsaved_local_commit"] = {
        "complete_rc": done_a.returncode,
        "workspace": str(ws_a),
        "workspace_exists": ws_a.is_dir(),
        "unpushed_file_present": (repo_a / "unpushed.txt").exists(),
        "preserved_event": [e for e in events_a if e.get("kind") == PRESERVED],
        "status": show(task_a).get("task", {}).get("status"),
    }

    # Case B: a clean clone whose history is fully pushed -> directory must go.
    task_b, ws_b = fresh_card("probe clean work")
    _git(env, root, "clone", "-q", str(origin), str(ws_b / "clone"))
    done_b = _hermes(env, root, "complete", task_b, "--result", "probe done", "--force", check=False)
    events_b = show(task_b).get("events", [])
    report["cases"]["clean_pushed"] = {
        "complete_rc": done_b.returncode,
        "workspace": str(ws_b),
        "workspace_exists": ws_b.exists(),
        "preserved_event": [e for e in events_b if e.get("kind") == PRESERVED],
        "status": show(task_b).get("task", {}).get("status"),
    }

    a = report["cases"]["unsaved_local_commit"]
    b = report["cases"]["clean_pushed"]
    ok = (
        a["status"] == "done" and a["workspace_exists"] and a["unpushed_file_present"]
        and len(a["preserved_event"]) == 1
        and str(a["preserved_event"][0].get("payload", "")).find(str(ws_a)) >= 0
        and b["status"] == "done" and b["workspace_exists"] is False
        and b["preserved_event"] == []
    )
    report["verdict"] = "pass" if ok else "FAIL"
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text + "\n", encoding="utf-8")
    print(text)
    if not ok:
        print(f"\nartifacts kept at {root}", file=sys.stderr)
        return 1
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
