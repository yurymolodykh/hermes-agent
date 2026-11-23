"""CPU per frame with enough work per sample to clear /proc's 10 ms jiffy quantization.

The matrix's bursts ran ~50-70 ms, so a 10 ms jiffy is 15-20% of each sample: every per-frame
delta smaller than that was noise. Here each sample sends ~10x more frames (repeat=20), so the
sample spans ~1 s of CPU and the quantization error drops to ~1%.

usage: python cpu_precise.py --variants on control w13 w15 off --mode models --samples 7
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
ARGV = {
    "on": ["--deflate", "on"],
    "control": ["--deflate", "on", "--baseline"],
    "w11": ["--deflate", "on", "--window", "11"],
    "w13": ["--deflate", "on", "--window", "13"],
    "w15": ["--deflate", "on", "--window", "15"],
    "level1": ["--deflate", "on", "--level", "1"],
    "off": ["--deflate", "off"],
}


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().split()
    return (int(parts[13]) + int(parts[14])) / 100.0


def _write_json(path: str, data: dict) -> None:
    """Serialise off the event loop: the file is small but a blocking write must not stall it."""
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


async def wait_ready(port: int) -> None:
    for _ in range(160):
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/bench?mode=card&repeat=1",
                                         compression="deflate", max_size=None) as ws:
                while True:
                    try:
                        await ws.recv()
                    except websockets.ConnectionClosed:
                        break
            return
        except OSError:
            await asyncio.sleep(0.25)
    raise RuntimeError(f"bench server on {port} never became ready")


async def burst(port: int, mode: str, repeat: int, clients: int) -> dict:
    async def one() -> tuple[int, int]:
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

    t0 = time.perf_counter()
    res = await asyncio.gather(*(one() for _ in range(clients)))
    return {"frames": sum(r[0] for r in res), "app_bytes": sum(r[1] for r in res),
            "wall_s": round(time.perf_counter() - t0, 4)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", nargs="+", default=["on", "control", "w13", "w15", "off"])
    ap.add_argument("--mode", default="models")
    ap.add_argument("--samples", type=int, default=7)
    ap.add_argument("--repeat", type=int, default=20)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--out", default="cpu-precise.json")
    args = ap.parse_args()

    ports = {v: 18700 + i for i, v in enumerate(args.variants)}
    servers = {v: subprocess.Popen([sys.executable, f"{HERE}/server_app.py",
                                    "--port", str(ports[v]), *ARGV[v]],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for v in args.variants}
    result: dict = {"mode": args.mode, "repeat": args.repeat, "clients": args.clients,
                    "variants": {}}
    try:
        for v in servers:
            await wait_ready(ports[v])
            await burst(ports[v], args.mode, 2, args.clients)  # warm
        runs = {v: [] for v in servers}
        walls = {v: [] for v in servers}
        # interleave so thermal/frequency drift is shared, not attributed to one variant
        for _ in range(args.samples):
            for v, proc in servers.items():
                before = cpu_seconds(proc.pid)
                b = await burst(ports[v], args.mode, args.repeat, args.clients)
                after = cpu_seconds(proc.pid)
                runs[v].append({"cpu_s": round(after - before, 4), "frames": b["frames"],
                                "cpu_ms_per_frame": round((after - before) * 1000 / b["frames"], 5)})
                walls[v].append(b["wall_s"])
        base = statistics.median([r["cpu_ms_per_frame"] for r in runs["on"]])
        for v, rs in runs.items():
            cpus = [r["cpu_ms_per_frame"] for r in rs]
            med = statistics.median(cpus)
            result["variants"][v] = {
                "argv": ARGV[v],
                "frames_per_sample": rs[0]["frames"],
                "cpu_ms_per_frame": cpus,
                "median": med,
                "stdev": round(statistics.stdev(cpus), 5),
                "stdev_pct": round(statistics.stdev(cpus) / med * 100, 1),
                "vs_on_pct": round((med - base) / base * 100, 1),
                "wall_median": statistics.median(walls[v]),
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        await asyncio.to_thread(_write_json, args.out, result)
    finally:
        for p in servers.values():
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    asyncio.run(main())
