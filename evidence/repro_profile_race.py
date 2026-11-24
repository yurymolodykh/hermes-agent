"""Two cards (separate projects) of ONE profile taking checkpoints at the same moment.

Live shape of the defect: a profile has one ``.checkpoints.lock`` for every card it runs, so two
concurrent workers of the same profile contended for it and the loser was refused instantly.
Each take prints one JSON line, so a refusal is visible as ``ok=False`` *with* a failure record;
``ok=False`` with ``failure=None`` only means there was nothing new to snapshot, which is correct
behaviour and not a lost checkpoint.

Usage:  repro_profile_race.py <base> race <rounds>
        repro_profile_race.py <base> take <project> <go-file> <tag>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def take(base: Path, project: Path, go: Path, tag: str) -> None:
    import tools.checkpoint_manager as C

    C.CHECKPOINT_BASE = base
    mgr = C.CheckpointManager(enabled=True, max_snapshots=5, max_total_size_mb=0)
    while not go.exists():
        time.sleep(0.001)
    (project / f"{tag}.txt").write_text(f"{tag} {time.time()}", encoding="utf-8")
    t0 = time.monotonic()
    ok = mgr.ensure_checkpoint(str(project), f"race {tag}")
    print(json.dumps({"tag": tag, "ok": ok, "ms": round((time.monotonic() - t0) * 1000, 1),
                      "failure": mgr._last_failure}, ensure_ascii=False), flush=True)


def race(base: Path, rounds: int) -> None:
    oks = refused = nothing = 0
    for n in range(rounds):
        go = base.parent / f".race-go-{n}"
        go.unlink(missing_ok=True)
        procs = []
        for i in range(2):
            project = base.parent / f"proj-{i}"
            project.mkdir(parents=True, exist_ok=True)
            procs.append(subprocess.Popen(
                [sys.executable, __file__, str(base), "take", str(project), str(go), f"r{n}p{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
        time.sleep(0.15)
        go.touch()
        for p in procs:
            out = p.communicate()[0].strip()
            if not out:
                print(f"CHILD-FAILED rc={p.returncode}", flush=True)
                continue
            row = json.loads(out.splitlines()[-1])
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if row["ok"]:
                oks += 1
            elif row["failure"]:
                refused += 1
            else:
                nothing += 1
        go.unlink(missing_ok=True)
    print(f"RESULT rounds={rounds} takes={rounds * 2} ok={oks} refused={refused} "
          f"nothing_to_snapshot={nothing}", flush=True)


if __name__ == "__main__":
    base = Path(sys.argv[1])
    base.mkdir(parents=True, exist_ok=True)
    if sys.argv[2] == "take":
        take(base, Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
    else:
        race(base, int(sys.argv[3]))
