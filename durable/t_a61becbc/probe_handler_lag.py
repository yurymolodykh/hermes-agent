"""In-process loop-lag probe of the handler body itself (no HTTP, no client noise).

The HTTP stands cannot isolate this: an 8 MiB JSON body costs ~130 ms of loop time in the
*parser*, identical in both modes, and it drowns the number the change actually moves. This
probe removes the transport and calls exactly the function the seam wraps, from a real event
loop, while a 10 ms ticker measures loop availability:

  inline   — HANDLER_OFFLOAD disabled: the call runs on the loop (pre-change behaviour)
  threaded — the shipped path: the call runs on the worker pool

The metric is the GAP between consecutive ticker wake-ups: a ticker asking for 10 ms and being
woken 160 ms later means the loop was unavailable for 150 ms. Inline mode produces one long
gap (the whole burst runs without an await point); threaded mode keeps ticking throughout.

Usage: python3 probe_handler_lag.py --out durable/t_a61becbc/probe-handler-lag.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])


async def _measure(mode: str, workdir: Path, size: int, parallel: int) -> dict:
    from hermes_cli.web_routers.handler_offload import HANDLER_OFFLOAD, off_loop
    from hermes_cli.web_routers.files import _fs_write_text_blocking

    HANDLER_OFFLOAD.reset()
    HANDLER_OFFLOAD.resolve()
    HANDLER_OFFLOAD._enabled = mode == "threaded"
    HANDLER_OFFLOAD._settled = True
    HANDLER_OFFLOAD._workers = max(8, parallel)
    if HANDLER_OFFLOAD._enabled:
        HANDLER_OFFLOAD._arm()
    assert HANDLER_OFFLOAD.snapshot()["mode"] == mode

    text = "x" * size
    ticks: list[tuple[float, bool]] = []
    recording = [False]

    async def ticker():
        while True:
            await asyncio.sleep(0.01)
            ticks.append((time.perf_counter(), recording[0]))

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.08)

    recording[0] = True
    started = time.perf_counter()
    await asyncio.gather(*[
        off_loop(_fs_write_text_blocking, workdir / f"{mode}-{i}.txt", text)
        for i in range(parallel)
    ])
    burst = time.perf_counter() - started
    recording[0] = False
    await asyncio.sleep(0.08)
    task.cancel()

    # Gap between consecutive tick timestamps while recording — the loop's unavailability.
    stamps = [t for t, rec in ticks if rec]
    inside = [t for t, _ in ticks if started <= t <= started + burst]
    gaps = [round((b - a) * 1000, 1) for a, b in zip([started] + inside, inside + [started + burst])]
    ordered = sorted(gaps)
    n = len(ordered)
    out = {
        "mode": mode,
        "parallel": parallel,
        "payload_mib": size // (1024 * 1024),
        "burst_wall_ms": round(burst * 1000, 1),
        "ticks_inside_burst": len(inside),
        "loop_unavailable": {
            "max_gap_ms": ordered[-1] if ordered else 0,
            "p99_gap_ms": ordered[int(n * 0.99)] if ordered else 0,
            "p50_gap_ms": ordered[n // 2] if ordered else 0,
            "gaps_ms": gaps,
        },
        "offload_counters": HANDLER_OFFLOAD.snapshot(),
    }
    HANDLER_OFFLOAD.shutdown()
    return out


async def _run(args) -> dict:
    home = Path(tempfile.mkdtemp(prefix="t-a61-hlag-"))
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_TEST_ISOLATION"] = "1"
    os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
    rows = []
    for mode in ("inline", "threaded", "inline", "threaded"):
        rows.append(await _measure(mode, home, args.mib * 1024 * 1024, args.parallel))
        print(f"{mode}: max loop gap {rows[-1]['loop_unavailable']['max_gap_ms']} ms", flush=True)
    return {"parallel": args.parallel, "mib": args.mib, "runs": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--mib", type=int, default=8)
    ap.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()
    sys.path.insert(0, REPO)
    out = asyncio.run(_run(args))
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
