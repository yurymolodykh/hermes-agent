"""Live end-to-end probe: complete a real scratch-workspace card through the CLI.

Runs against an isolated ``HERMES_HOME`` with the real ``hermes kanban`` command, not
the Python API the unit tests use, so the dispatcher-side completion path
(``hermes kanban complete`` → ``complete_task`` → ``_cleanup_workspace``) is what
is actually exercised.

Usage: ``python evidence/probe_scratch_guard.py`` — prints a JSON verdict.
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


def _hermes(env: dict, cwd: Path, *args: str, check: bool = True):
    return _run([sys.executable, "-m", "hermes_cli.kanban", *args], cwd=cwd, env=env, check=check)


def _events(env: dict, cwd: Path, task_id: str) -> list[dict]:
    out = _hermes(env, cwd, "show", task_id, "--json").stdout
    return json.loads(out).get("events", [])


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="scratch-guard-probe."))
    env = dict(os.environ)
    env["HERMES_HOME"] = str(root / "hermes")
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_KANBAN_WORKSPACE", None)
    env.pop("HERMES_KANBAN_WORKSPACES_ROOT", None)
    env["PYTHONPATH"] = str(REPO)

    origin = root / "origin.git"
    _run(["git", "init", "--bare", "-q", str(origin)], cwd=root, env=env)
    seed = root / "seed"
    _run(["git", "clone", "-q", str(origin), str(seed)], cwd=root, env=env)
    _run(["git", "config", "user.email", "probe@example.com"], cwd=seed, env=env)
    _run(["git", "config", "user.name", "probe"], cwd=seed, env=env)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=seed, env=env)
    _run(["git", "commit", "-qm", "init"], cwd=seed, env=env)
    _run(["git", "push", "-q", "origin", "HEAD"], cwd=seed, env=env)

    def card(title: str) -> tuple[str, Path]:
        out = _hermes(env, root, "create", title, "--json").stdout
        task_id = json.loads(out)["id"]
        ws = root / "hermes" / "kanban" / "workspaces" / task_id
        _hermes(env, root, "set-workspace", task_id, str(ws), check=False)
        return task_id, ws

    def clone_with_local_commit(dest: Path) -> Path:
        repo = dest / "clone"
        _run(["git", "clone", "-q", str(origin), str(repo)], cwd=root, env=env)
        _run(["git", "config", "user.email", "probe@example.com"], cwd=repo, env=env)
        _run(["git", "config", "user.name", "probe"], cwd=repo, env=env)
        (repo / "unpushed.txt").write_text("only here\n", encoding="utf-8")
        _run(["git", "add", "unpushed.txt"], cwd=repo, env=env)
        _run(["git", "commit", "-qm", "never pushed"], cwd=repo, env=env)
        return repo

    report: dict = {"hermes_home": str(env["HERMES_HOME"]), "cases": {}}

    # Case A: local commit, nothing pushed -> directory must survive.
    task_a, ws_a = card("probe unsaved")
    repo_a = clone_with_local_commit(ws_a)
    _hermes(env, root, "complete", task_a, "--result", "probe done")
    events_a = _events(env, root, task_a)
    report["cases"]["unsaved_local_commit"] = {
        "workspace_exists": ws_a.is_dir(),
        "commit_still_there": (repo_a / "unpushed.txt").exists(),
        "preserved_event": [e for e in events_a if PRESERVED == e.get("kind")],
    }

    # Case B: clean clone, fully pushed -> directory must be removed.
    task_b, ws_b = card("probe clean")
    _run(["git", "clone", "-q", str(origin), str(ws_b / "clone")], cwd=root, env=env)
    _hermes(env, root, "complete", task_b, "--result", "probe done")
    report["cases"]["clean_pushed"] = {
        "workspace_exists": ws_b.exists(),
        "preserved_event": [e for e in _events(env, root, task_b) if PRESERVED == e.get("kind")],
    }

    ok = (
        report["cases"]["unsaved_local_commit"]["workspace_exists"]
        and report["cases"]["unsaved_local_commit"]["commit_still_there"]
        and len(report["cases"]["unsaved_local_commit"]["preserved_event"]) == 1
        and report["cases"]["clean_pushed"]["workspace_exists"] is False
        and report["cases"]["clean_pushed"]["preserved_event"] == []
    )
    report["verdict"] = "pass" if ok else "FAIL"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not ok:
        print(f"\nartifacts kept at {root}", file=sys.stderr)
        return 1
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
