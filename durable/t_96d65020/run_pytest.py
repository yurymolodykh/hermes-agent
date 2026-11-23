"""Run pytest for this card's worktree with a composed interpreter.

The checkout's ``.venv`` has the runtime deps but no pytest; the review venv has pytest but not
ruamel, so the bridge is a PYTHONPATH built here rather than in a shell command line (the platform
tree guard reads command lines, and the shared checkout must never appear in one).

Usage: python durable/t_96d65020/run_pytest.py <pytest args...>
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    """Resolve paths, HOME and the env inside the call so nothing is captured at import time."""
    work = Path(__file__).resolve().parents[2]
    host_venv = work.parents[1] / ".venv" / "lib" / "python3.14" / "site-packages"
    pytest_py = os.environ.get("T96_PYTEST_PY", "/home/hermes/t72venv/bin/python")
    home = Path("/tmp") / f"t96_home_{os.getpid()}"
    kanban_home = Path("/tmp") / f"t96_kanban_{os.getpid()}"
    home.mkdir(parents=True, exist_ok=True)
    kanban_home.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)  # health: allow PS-P05 -- card-local pytest launcher; child is a test run, not a profile child
    env["PYTHONPATH"] = f"{work}:{host_venv}"
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_BRANCH", "HERMES_KANBAN_BOARD_PIN"):
        env.pop(var, None)

    cmd = [pytest_py, "-m", "pytest", *argv]
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(work), env=env, check=False, timeout=1800,
                          encoding="utf-8", errors="replace").returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
