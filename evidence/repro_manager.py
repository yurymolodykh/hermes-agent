"""Воспроизведение на реальном CheckpointManager: два воркера одного профиля.

Режимы:
  python repro_manager.py <base> <project> take <startfile>   — один цикл снятия чекпойнта
  python repro_manager.py <base> <project> race <rounds>      — раунды: 2 процесса стартуют одновременно
  python repro_manager.py <base> <project> maintenance        — длительность prune_checkpoints под локом
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tools.checkpoint_manager as C  # noqa: E402


def _manager(base: Path) -> "C.CheckpointManager":
    C.CHECKPOINT_BASE = base
    return C.CheckpointManager(enabled=True, max_snapshots=5, max_total_size_mb=0)


def take(base: Path, project: Path, start: Path, tag: str) -> None:
    mgr = _manager(base)
    while not start.exists():
        time.sleep(0.001)
    (project / f"{tag}.txt").write_text(f"{tag} {time.time()}", encoding="utf-8")
    t0 = time.monotonic()
    ok = mgr.ensure_checkpoint(str(project), f"race {tag}")
    dt = time.monotonic() - t0
    print(f"TAKE tag={tag} ok={ok} {dt*1000:.1f}ms failure={mgr._last_failure}", flush=True)


def race(base: Path, project: Path, rounds: int) -> None:
    refused = oks = 0
    for n in range(rounds):
        start = base.parent / f".race-start-{os.getpid()}-{n}"
        start.unlink(missing_ok=True)
        procs = [
            subprocess.Popen(
                [sys.executable, __file__, str(base), str(project), "take", str(start), f"r{n}p{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for i in range(2)
        ]
        time.sleep(0.08)
        start.touch()
        for p in procs:
            out = p.communicate()[0]
            print(out.strip(), flush=True)
            oks += out.count("ok=True")
            refused += out.count("PruneError")
        start.unlink(missing_ok=True)
    print(f"RESULT rounds={rounds} takes={rounds*2} ok={oks} refused={refused}", flush=True)


def maintenance(base: Path, project: Path) -> None:
    from tools import checkpoint_maintenance
    t0 = time.monotonic()
    res = checkpoint_maintenance.prune_checkpoints(
        retention_days=0, checkpoint_base=base, max_total_size_mb=1)
    print(f"MAINTENANCE {time.monotonic()-t0:.2f}s result={res}", flush=True)


if __name__ == "__main__":
    base = Path(sys.argv[1])
    project = Path(sys.argv[2])
    mode = sys.argv[3]
    base.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    if mode == "take":
        take(base, project, Path(sys.argv[4]), sys.argv[5])
    elif mode == "race":
        race(base, project, int(sys.argv[4]))
    else:
        maintenance(base, project)
