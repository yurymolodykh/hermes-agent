"""Run the whole before/after matrix and write evidence JSON.

Scenarios = the three shapes a dashboard session actually sends (see server_app.py): streamed
deltas, control cards, the model list. Each is measured with permessage-deflate ON (the
pre-change default for a remote/tunnelled peer) and OFF, on wire bytes (relay-counted) and on
server CPU time (/proc utime+stime, sender side).

Usage: python run_matrix.py --out evidence.json
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
PORTS = {"on": 18301, "off": 18302, "tuned": 18303}
MODES = ("delta-coalesced", "delta", "card", "models", "panel")


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().split()
    return (int(parts[13]) + int(parts[14])) / 100.0


async def wait_ready(port: int) -> None:
    for _ in range(120):
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/bench?mode=card&repeat=0",
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
    wall = time.perf_counter() - t0
    return {"frames": sum(r[0] for r in results), "app_bytes": sum(r[1] for r in results),
            "wall_s": round(wall, 3)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--window", type=int, default=9)
    ap.add_argument("--memlevel", type=int, default=4)
    args = ap.parse_args()

    argv = {
        "on": ["--deflate", "on"],
        "off": ["--deflate", "off"],
        "tuned": ["--deflate", "on", "--window", str(args.window), "--memlevel", str(args.memlevel)],
    }
    servers = {d: subprocess.Popen(
        [sys.executable, f"{HERE}/server_app.py", "--port", str(PORTS[d]), *a],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for d, a in argv.items()}
    report: dict = {"clients": args.clients, "rounds": args.rounds, "scenarios": {}}
    try:
        for d in servers:
            await wait_ready(PORTS[d])

        for mode in MODES:
            entry: dict = {}
            for d in ("on", "off", "tuned"):
                pid, port = servers[d].pid, PORTS[d]
                # wire bytes: one client, relay-counted (drive.py prints its own JSON)
                out = subprocess.run(
                    [sys.executable, f"{HERE}/drive.py", "--port", str(port), "--mode", mode],
                    capture_output=True, text=True, check=True)
                wire = json.loads(out.stdout.strip().splitlines()[-1])
                # sender CPU: warm once, then measure `rounds` bursts of N concurrent clients
                await burst(port, mode, 2, args.clients)
                samples = []
                for _ in range(args.rounds):
                    before = cpu_seconds(pid)
                    b = await burst(port, mode, 2, args.clients)
                    after = cpu_seconds(pid)
                    samples.append({"cpu_s": round(after - before, 4), **b,
                                    "cpu_ms_per_frame": round((after - before) * 1000 / b["frames"], 4)})
                entry[d] = {"wire": wire, "cpu": samples}
            on, off, tuned = entry["on"], entry["off"], entry["tuned"]
            wire_on, wire_off = on["wire"]["wire_s2c"], off["wire"]["wire_s2c"]
            entry["summary"] = {
                "wire_s2c_deflate_on": wire_on,
                "wire_s2c_deflate_off": wire_off,
                "traffic_increase_off_pct": round((wire_off - wire_on) / wire_on * 100, 1),
                "traffic_saving_on_pct": round((wire_off - wire_on) / wire_off * 100, 1),
                "cpu_ms_per_frame_on_median": sorted(s["cpu_ms_per_frame"] for s in on["cpu"])[len(on["cpu"]) // 2],
                "cpu_ms_per_frame_off_median": sorted(s["cpu_ms_per_frame"] for s in off["cpu"])[len(off["cpu"]) // 2],
                "cpu_wall_median_on": sorted(s["wall_s"] for s in on["cpu"])[len(on["cpu"]) // 2],
                "cpu_wall_median_off": sorted(s["wall_s"] for s in off["cpu"])[len(off["cpu"]) // 2],
                "app_bytes": on["wire"]["app_bytes"],
                "frames": on["wire"]["frames"],
                "tuned_window": args.window,
                "tuned_memlevel": args.memlevel,
                "wire_s2c_tuned": tuned["wire"]["wire_s2c"],
                "traffic_vs_on_tuned_pct": round(
                    (tuned["wire"]["wire_s2c"] - wire_on) / wire_on * 100, 1),
                "cpu_ms_per_frame_tuned_median": sorted(
                    s["cpu_ms_per_frame"] for s in tuned["cpu"])[len(tuned["cpu"]) // 2],
                "cpu_wall_median_tuned": sorted(
                    s["wall_s"] for s in tuned["cpu"])[len(tuned["cpu"]) // 2],
            }
            report["scenarios"][mode] = entry
            print(json.dumps({"mode": mode, **entry["summary"]}, ensure_ascii=False))
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
