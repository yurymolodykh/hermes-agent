#!/usr/bin/env python3
"""Живой прогон уборщика: dry-run, затем --apply, затем readback обоих каталогов.

Пишет доказательства в durable/t_c1c3cc78/evidence/. Запускается интерпретатором
venv, потому что uборщик живёт в профиле 01-orchestrator (носитель крона).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PY = "/home/hermes/.hermes/hermes-agent/.venv/bin/python"
SCRIPT = Path("/home/hermes/.hermes/profiles/01-orchestrator/scripts/disk_reclaim_cron.py")
OUT = Path("/home/hermes/.hermes/hermes-agent/.worktrees/t_c1c3cc78/durable/t_c1c3cc78/evidence")
TARGETS = [
    "/home/hermes/.hermes/profiles/02-hermes-platform/cache/scratch/tmpi_f19jp8",
    "/home/hermes/.hermes/profiles/09-independent-reviewer/cache/scratch/t9be97024",
]


def run(mode: str, extra: list[str]) -> dict:
    cmd = [PY, str(SCRIPT), "--json", *extra]
    if mode == "apply":
        cmd.insert(3, "--apply")
    env = dict(os.environ)
    env["HERMES_KANBAN_TASK"] = "t_c1c3cc78"
    proc = subprocess.run(cmd, cwd=str(SCRIPT.parent), capture_output=True, text=True,
                          timeout=1800, env=env)
    data = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
    return {"cmd": " ".join(cmd), "exit": proc.returncode, "data": data,
            "stderr": proc.stderr[-2000:]}


def brief(res: dict) -> dict:
    d = res["data"]
    return {
        "cmd": res["cmd"], "exit": res["exit"],
        "mode": d.get("mode"), "source": d.get("source"),
        "removed": len(d.get("candidates") or []),
        "refused": len(d.get("refused") or []),
        "freed_bytes": d.get("freed_bytes"),
        "free_gib": d.get("free_gib"),
        "targets_removed": [c["path"] for c in (d.get("candidates") or []) if c["path"] in TARGETS],
        "targets_refused": [{"path": c["path"], "why": c.get("refused")}
                            for c in (d.get("refused") or []) if c["path"] in TARGETS],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    which = sys.argv[1] if len(sys.argv) > 1 else "dry"
    if which == "dry":
        res = run("dry", ["--ignore-card-ref", "t_c1c3cc78"])
        (OUT / "live-dry.json").write_text(json.dumps(res["data"], ensure_ascii=False, indent=1),
                                           encoding="utf-8")
        print(json.dumps(brief(res), ensure_ascii=False, indent=1))
        return 0
    if which == "apply":
        res = run("apply", ["--ignore-card-ref", "t_c1c3cc78",
                            "--manifest-out", str(OUT / "live-apply-manifest.json")])
        (OUT / "live-apply.json").write_text(json.dumps(res["data"], ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        print(json.dumps(brief(res), ensure_ascii=False, indent=1))
        return 0
    if which == "check":
        for t in TARGETS:
            print(("ABSENT " if not Path(t).exists() else "PRESENT ") + t)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
