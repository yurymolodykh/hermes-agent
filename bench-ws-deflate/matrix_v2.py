"""Variant matrix: default deflate (level 6) vs off vs level-1 vs level-1+window-9.

Measures the two things the card asks for per variant:
  * wire_s2c        — bytes actually on the socket, counted by a TCP relay (drive.py)
  * cpu_ms_per_frame — sender CPU from /proc/<pid>/{utime,stime} under N concurrent clients

Usage: python matrix_v2.py --out matrix-v2.json [--rounds 5] [--clients 8]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time

import websockets

HERE = "/home/hermes/.hermes/hermes-agent/.worktrees/t_5bb8406e/bench-ws-deflate"
PORTS = {"on": 18501, "control": 18502, "level1": 18503, "level4": 18504, "off": 18505,
         "notakeover": 18506, "memlevel1": 18507}
MODES = ("delta-coalesced", "card", "models", "panel")
ARGV = {
    # bare uvicorn: the production default for a gated listener
    "on": ["--deflate", "on"],
    # control: the SAME settings re-declared through the override factory. If this does not match
    # "on", the override path itself is unfaithful and no tuned variant can be trusted.
    "control": ["--deflate", "on", "--baseline"],
    "level1": ["--deflate", "on", "--level", "1"],
    "level4": ["--deflate", "on", "--level", "4"],
    "off": ["--deflate", "off"],
    # per-message window reset: the documented memory/CPU lever (websockets compression topics)
    "notakeover": ["--deflate", "on", "--no-context-takeover"],
    # smallest zlib memory footprint at the default window and level
    "memlevel1": ["--deflate", "on", "--memlevel", "1"],
}


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().split()
    return (int(parts[13]) + int(parts[14])) / 100.0


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


async def single_client(port: int, mode: str, repeat: int) -> tuple[int, int]:
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


async def burst(port: int, mode: str, repeat: int, clients: int) -> dict:
    t0 = time.perf_counter()
    results = await asyncio.gather(*(single_client(port, mode, repeat) for _ in range(clients)))
    return {"frames": sum(r[0] for r in results), "app_bytes": sum(r[1] for r in results),
            "wall_s": round(time.perf_counter() - t0, 3)}


def median(xs: list[float]) -> float:
    return sorted(xs)[len(xs) // 2]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()

    servers = {d: subprocess.Popen([sys.executable, f"{HERE}/server_app.py",
                                    "--port", str(PORTS[d]), *a],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for d, a in ARGV.items()}
    report: dict = {"clients": args.clients, "rounds": args.rounds, "scenarios": {}}
    try:
        for d in servers:
            await wait_ready(PORTS[d])

        for mode in MODES:
            entry: dict = {}
            for d in servers:
                pid, port = servers[d].pid, PORTS[d]
                out = subprocess.run([sys.executable, f"{HERE}/drive.py",
                                      "--port", str(port), "--mode", mode],
                                     capture_output=True, text=True, check=True)
                wire = json.loads(out.stdout.strip().splitlines()[-1])
                await burst(port, mode, args.repeat, args.clients)  # warm
                samples = []
                for _ in range(args.rounds):
                    before = cpu_seconds(pid)
                    b = await burst(port, mode, args.repeat, args.clients)
                    after = cpu_seconds(pid)
                    samples.append({"cpu_s": round(after - before, 4), **b,
                                    "cpu_ms_per_frame": round((after - before) * 1000 / b["frames"], 4)})
                entry[d] = {"wire": wire, "cpu": samples}
            summary = {}
            base_wire = entry["on"]["wire"]["wire_s2c"]
            base_cpu = median([s["cpu_ms_per_frame"] for s in entry["on"]["cpu"]])
            for d, v in entry.items():
                wire = v["wire"]["wire_s2c"]
                summary[d] = {
                    "wire_s2c": wire,
                    "traffic_vs_default_pct": round((wire - base_wire) / base_wire * 100, 1),
                    "cpu_ms_per_frame_median": median([s["cpu_ms_per_frame"] for s in v["cpu"]]),
                    "cpu_vs_default_pct": round(
                        (median([s["cpu_ms_per_frame"] for s in v["cpu"]]) - base_cpu) / base_cpu * 100, 1)
                    if base_cpu else 0.0,
                    "wall_median": median([s["wall_s"] for s in v["cpu"]]),
                    "frames": v["wire"]["frames"],
                    "app_bytes": v["wire"]["app_bytes"],
                }
            report["scenarios"][mode] = {"variants": entry, "summary": summary}
            print(json.dumps({"mode": mode, "summary": summary}, ensure_ascii=False), flush=True)
    finally:
        for p in servers.values():
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"written {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
