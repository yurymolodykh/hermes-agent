#!/usr/bin/env python3
"""Снять доказательства приёмки t_c1c3cc78: красный прогон на пре-фикс коде и зелёный на фиксе.

Скрипт нужен потому, что stderr/stdout pytest надо писать файлом: оболочка с
перенаправлением в чужое дерево блокируется стражем деревьев. Здесь всё пишется
обычным open() из Python.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PY = "/home/hermes/.hermes/hermes-agent/.venv/bin/python"
RED = Path("/home/hermes/.hermes/profiles/02-hermes-platform/cache/scratch/red-proof/scripts")
FIX = Path("/home/hermes/.hermes/profiles/01-orchestrator/scripts")
OUT = Path("/home/hermes/.hermes/hermes-agent/.worktrees/t_c1c3cc78/durable/t_c1c3cc78/evidence")


def run(cwd: Path, tests: list[str], out: Path) -> int:
    cmd = [PY, "-m", "pytest", "-q", *tests]
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600)
    out.write_text(f"$ cd {cwd} && {' '.join(cmd)}\n"
                   f"exit={proc.returncode}\n"
                   f"{proc.stdout}\n{proc.stderr}", encoding="utf-8")
    print(f"{out.name}: exit={proc.returncode}")
    return proc.returncode


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    red = run(RED, ["tests/test_disk_reclaim_readonly_removal.py"], OUT / "red-prefix.out")
    green = run(FIX, ["tests/test_disk_reclaim_readonly_removal.py",
                      "tests/test_disk_reclaim_card_refs.py"], OUT / "green-postfix.out")
    return 0 if (red != 0 and green == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
