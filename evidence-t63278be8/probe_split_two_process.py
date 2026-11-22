"""Probe Д-2: does a slow decomposer freeze the board for a sibling dispatcher?

Acceptance defect Д-2 of the auto-continue review: ``run_split_phase`` ran inside
``_dispatch_once_locked``, i.e. under the board's single-writer ``.dispatch.lock``,
while its decomposer is an aux LLM request of up to 180 s x 3 attempts. Everything
the dispatcher does — reclaim, promote, spawn, reaper — waits behind it.

This probe reproduces the reviewer's two-process measurement on a throwaway board:

* the CHILD runs one production tick whose decomposer is a stub that sleeps
  ``--sleep`` seconds (the "slow decomposer");
* the PARENT, once the child is provably inside its decomposer, puts one ready
  card on the board and runs its own full tick, recording ``skipped_locked``,
  whether the card got spawned, and how long the tick took.

Pre-fix the parent reports ``skipped_locked=True``, spawns nothing and blocks for
the whole decomposer. Post-fix it spawns immediately and only the child holds
``.split.lock``.

Also measured: how many times the split phase runs per tick (must be 1).

Run in the patched tree AND on the base revision; compare the JSON.
    PYTHONPATH=$PWD .venv/bin/python evidence-t63278be8/probe_split_two_process.py
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path


def _isolate() -> Path:
    """Throwaway home BEFORE importing kanban (module-level paths are baked)."""
    root = Path(tempfile.mkdtemp(prefix="probe-split-"))
    (root / ".hermes").mkdir()
    (root / ".hermes" / "config.yaml").write_text(
        "kanban:\n  failure_limit: 3\n  dispatch_interval_seconds: 60\n", encoding="utf-8"
    )
    os.environ["HERMES_HOME"] = str(root / ".hermes")
    os.environ["HERMES_KANBAN_HOME"] = str(root / ".hermes")
    os.environ.pop("HERMES_KANBAN_DB", None)
    os.environ.pop("HERMES_KANBAN_BOARD", None)
    # A Kanban worker inherits a write fence over ~/.hermes; this probe owns its board.
    os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    Path.home = lambda: root  # type: ignore[assignment]
    return root


def _child(db_path: str, state_fd: int, done_fd: int, sleep_seconds: float) -> None:
    """One PRODUCTION tick whose split phase is a slow decomposer."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    conn = kbc.connect()
    started = [time.monotonic()]

    def _slow_split(conn_, **_kwargs):
        os.write(state_fd, b"S")               # inside the decomposer
        time.sleep(sleep_seconds)              # health: allow HX013 -- this sleep IS the stall
        return [], []

    kbd._kac.run_split_phase = _slow_split
    os.write(state_fd, b"R")                   # about to tick
    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    payload = {
        "child_tick_seconds": round(time.monotonic() - started[0], 2),
        "child_skipped_locked": bool(getattr(result, "skipped_locked", False)),
    }
    os.write(done_fd, json.dumps(payload).encode() + b"\n")
    conn.close()
    os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=float, default=6.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = _isolate()
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    # Profile routing is not under test; accept any assignee.
    kbd._profile_exists_fn = lambda: (lambda name: True)

    kb.init_db()
    conn = kbc.connect()
    db_path = str(kb.kanban_db_path(board="default"))

    state_r, state_w = os.pipe()
    done_r, done_w = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - child process
        os.close(state_r)
        os.close(done_r)
        _child(db_path, state_w, done_w, args.sleep)

    os.close(state_w)
    os.close(done_w)
    out: dict = {"temp_root": str(root), "slow_decomposer_seconds": args.sleep}
    try:
        assert os.read(state_r, 1) == b"R", "the child could not start its tick"
        assert os.read(state_r, 1) == b"S", "the child never reached its decomposer"

        tid = kb.create_task(conn, title="waiting work", assignee="w")
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (f"/tmp/ws/{tid}", tid))
        conn.commit()

        spawned: list[str] = []
        started = time.monotonic()
        result = kbd.dispatch_once(
            conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id),
        )
        elapsed = time.monotonic() - started

        out.update(
            parent_tick_seconds=round(elapsed, 2),
            parent_skipped_locked=bool(result.skipped_locked),
            parent_spawned=spawned,
            spawned_during_split=bool(spawned),
        )
    finally:
        try:
            os.close(state_r)
        except OSError:
            pass
        child_report = {}
        try:
            raw = os.read(done_r, 65536).decode().strip()
            if raw:
                child_report = json.loads(raw)
        except OSError:
            pass
        out["child"] = child_report
        os.close(done_r)
        with __import__("contextlib").suppress(ProcessLookupError, OSError):
            os.kill(child, signal.SIGKILL)
        with __import__("contextlib").suppress(ChildProcessError, OSError):
            os.waitpid(child, 0)

    # Exactly one split phase per tick: count the calls of two tidy ticks.
    calls: list[int] = []

    def _counting_split(conn_, **_kwargs):
        calls.append(1)
        return [], []

    kbd._kac.run_split_phase = _counting_split
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    out["split_calls_for_two_ticks"] = len(calls)
    conn.close()

    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    else:
        print(f"slow decomposer           : {args.sleep}s")
        print(f"parent tick seconds       : {out['parent_tick_seconds']}")
        print(f"parent skipped_locked     : {out['parent_skipped_locked']}")
        print(f"spawned during the split  : {out['spawned_during_split']} {out['parent_spawned']}")
        print(f"split calls for 2 ticks   : {out['split_calls_for_two_ticks']}")
        print(f"child tick                : {out['child']}")

    ok = (
        out.get("parent_skipped_locked") is False
        and out.get("spawned_during_split") is True
        and out.get("parent_tick_seconds", 1e9) < args.sleep / 2
        and out.get("split_calls_for_two_ticks") == 2
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
