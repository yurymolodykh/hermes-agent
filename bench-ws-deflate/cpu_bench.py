"""Measure server-side CPU spent sending real frames, per config (deflate on/off).

Runs the bench server under repeated panel traffic from N concurrent clients and reads the
server process's own CPU time from /proc, so the number is the sender's cost (the loop thread
plus the zlib work uvicorn does there), not the client's.

Usage: python cpu_bench.py --port N --deflate {on,off} [--clients 8] [--rounds 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time

import websockets

HERE = "/home/hermes/.hermes/hermes-agent/.worktrees/t_5bb8406e/bench-ws-deflate"


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().split()
    return (int(parts[13]) + int(parts[14])) / 100.0  # utime+stime in clock ticks


async def one_client(port: int, mode: str, repeat: int) -> tuple[int, int]:
    frames = app = 0
    async with websockets.connect(f"ws://127.0.0.1:{port}/bench?mode={mode}&repeat={repeat}",
                                 compression="deflate", max_size=None) as ws:
        while True:
            try:
                msg = await ws.recv()
            except websockets.ConnectionClosed:
                break
            app += len(msg) if isinstance(msg, bytes) else len(msg.encode())
            frames += 1
    return frames, app


async def run_round(port: int, clients: int, mode: str, repeat: int) -> tuple[float, int, int]:
    t0 = time.perf_counter()
    results = await asyncio.gather(*(one_client(port, mode, repeat) for _ in range(clients)))
    elapsed = time.perf_counter() - t0
    return elapsed, sum(r[0] for r in results), sum(r[1] for r in results)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deflate", choices=("on", "off"), required=True)
    ap.add_argument("--mode", default="delta")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    port = 18201 if args.deflate == "on" else 18202
    srv = subprocess.Popen([sys.executable, f"{HERE}/server_app.py", "--port", str(port),
                            "--deflate", args.deflate],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                async with websockets.connect(f"ws://127.0.0.1:{port}/bench?mode=models&repeat=0",
                                             compression="deflate", max_size=None) as ws:
                    while True:
                        try:
                            await ws.recv()
                        except websockets.ConnectionClosed:
                            break
                break
            except OSError:
                time.sleep(0.25)
        rows = []
        for _ in range(args.rounds):
            # warm the code paths once, then measure (avoids one-off import/JIT noise)
            await run_round(port, args.clients, args.mode, args.repeat)
            before = cpu_seconds(srv.pid)
            elapsed, frames, app = await run_round(port, args.clients, args.mode, args.repeat)
            after = cpu_seconds(srv.pid)
            cpu = after - before
            rows.append({"cpu_s": round(cpu, 3), "wall_ms": round(elapsed * 1000, 1),
                         "frames": frames, "app_mb": round(app / 1e6, 3),
                         "cpu_ms_per_frame": round(cpu * 1000 / frames, 4),
                         "frames_per_s": round(frames / elapsed, 1)})
        print(json.dumps({"deflate": args.deflate, "mode": args.mode, "repeat": args.repeat, "clients": args.clients,
                          "rounds": rows,
                          "cpu_ms_per_frame_median": round(
                              statistics.median(r["cpu_ms_per_frame"] for r in rows), 4)}))
    finally:
        srv.terminate()
        srv.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
