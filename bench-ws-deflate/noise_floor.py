"""Noise floor: the SAME settings measured twice ("on" vs "control").

`on` is bare uvicorn; `control` re-declares byte-for-byte the same deflate settings through the
override factory. Every difference between them is measurement noise, not compression cost. If a
tuned variant's CPU delta does not clear this floor, that delta is not evidence.

Usage: python noise_floor.py --pairs 6 --mode delta-coalesced
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys

import websockets

HERE = "/home/hermes/.hermes/hermes-agent/.worktrees/t_5bb8406e/bench-ws-deflate"
PORTS = {"on": 18601, "control": 18602, "off": 18603}
ARGV = {"on": ["--deflate", "on"], "control": ["--deflate", "on", "--baseline"],
        "off": ["--deflate", "off"]}


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().split()
    return (int(parts[13]) + int(parts[14])) / 100.0


def _write_json(path: str, data: dict) -> None:
    """Serialise off the event loop: the file is small but a blocking write must not stall it."""
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


async def _spawn(*argv, **kw):
    """subprocess.Popen blocks the event loop while the kernel forks; hand it to a thread."""
    return await asyncio.to_thread(subprocess.Popen, list(argv), **kw)


async def _run(*argv, **kw):
    """subprocess.run waits for the child; on the loop that stalls every other connection."""
    kw.setdefault("check", False)
    return await asyncio.to_thread(subprocess.run, list(argv), **kw)


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

    import time
    t0 = time.perf_counter()
    res = await asyncio.gather(*(one() for _ in range(clients)))
    return {"frames": sum(r[0] for r in res), "wall_s": round(time.perf_counter() - t0, 4)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=6)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--mode", default="delta-coalesced")
    ap.add_argument("--out", default="noise-floor.json")
    args = ap.parse_args()

    servers = {}
    for d, a in ARGV.items():
        servers[d] = await _spawn(sys.executable, f"{HERE}/server_app.py",
                                  "--port", str(PORTS[d]), *a,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples: dict[str, list[float]] = {d: [] for d in servers}
    walls: dict[str, list[float]] = {d: [] for d in servers}
    try:
        for d in servers:
            await wait_ready(PORTS[d])
        for d in servers:  # warm every server
            await burst(PORTS[d], args.mode, 2, args.clients)
        # interleave the variants pair by pair so drift (thermal, CPU freq) hits all of them alike
        for _ in range(args.pairs):
            for d, proc in servers.items():
                before = cpu_seconds(proc.pid)
                b = await burst(PORTS[d], args.mode, 2, args.clients)
                after = cpu_seconds(proc.pid)
                samples[d].append(round((after - before) * 1000 / b["frames"], 4))
                walls[d].append(b["wall_s"])
        report = {}
        for d in servers:
            s = samples[d]
            report[d] = {"cpu_ms_per_frame": s, "median": statistics.median(s),
                         "stdev": round(statistics.stdev(s), 4) if len(s) > 1 else 0.0,
                         "min": min(s), "max": max(s), "wall_median": statistics.median(walls[d])}
        on, ctrl = report["on"], report["control"]
        spread = max(abs(a - b) for a, b in zip(samples["on"], samples["control"]))
        report["_noise_floor"] = {
            "mode": args.mode, "pairs": args.pairs,
            "median_diff_pct": round((ctrl["median"] - on["median"]) / on["median"] * 100, 1),
            "max_pair_diff_pct": round(spread / on["median"] * 100, 1),
            "stdev_pct_on": round(on["stdev"] / on["median"] * 100, 1),
            "stdev_pct_control": round(ctrl["stdev"] / ctrl["median"] * 100, 1),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        await asyncio.to_thread(_write_json, args.out, report)
    finally:
        for p in servers.values():
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    asyncio.run(main())
