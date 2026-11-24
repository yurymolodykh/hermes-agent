"""Проводник воспроизведения: конкуренция за store_lock одного профиля.

Запуск: python repro_store_lock.py <base> <hold-seconds> <wait-seconds>
Первый процесс держит лок hold-seconds и печатает, что взял.
Второй пытается взять лок так, как это делает checkpoint_manager.ensure_checkpoint
(store_lock -> _lock(wait=False)), и печатает результат + длительность.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.checkpoint_pruning import PruneError, store_lock, store_lock_path


def holder(base: Path, hold: float) -> None:
    with store_lock(base):
        p = store_lock_path(base)
        print(f"HOLDER pid={os.getpid()} lock={p} holding {hold}s", flush=True)
        time.sleep(hold)
    print("HOLDER released", flush=True)


def waiter(base: Path) -> None:
    p = store_lock_path(base)
    t0 = time.monotonic()
    try:
        with store_lock(base):
            dt = time.monotonic() - t0
            print(f"WAITER pid={os.getpid()} ACQUIRED after {dt:.3f}s", flush=True)
    except PruneError as exc:
        dt = time.monotonic() - t0
        print(f"WAITER pid={os.getpid()} FAILED after {dt:.3f}s: {exc}", flush=True)
        print("WAITER lock-holder-fds:", holder_fds(p), flush=True)


def holder_fds(path: Path) -> list[str]:
    out = []
    target = str(path.resolve())
    for pid_dir in Path("/proc").glob("[0-9]*"):
        try:
            for entry in os.scandir(pid_dir / "fd"):
                if os.readlink(entry.path) == target:
                    try:
                        cmd = (pid_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode()
                    except OSError:
                        cmd = "?"
                    out.append(f"pid {pid_dir.name} ({cmd.strip()})")
                    break
        except OSError:
            pass
    return out


if __name__ == "__main__":
    base = Path(sys.argv[1])
    hold = float(sys.argv[2])
    if len(sys.argv) > 3 and sys.argv[3] == "hold":
        holder(base, hold)
    else:
        waiter(base)
