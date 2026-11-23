"""Sample the bench server's thread stacks under load and report what share is in deflate.

This is the same instrument the card's context used for the live gateway profile (py-spy), run
against the bench server so the "how much of the loop's CPU is permessage-deflate" number is a
counted share of samples, not an impression. py-spy records only what the process is running, so
the deflate share below is *of busy time on the loop thread*, and the loop's own share of wall
time is reported separately.

Usage: python profile_share.py --deflate {on,off} --seconds 15 --clients 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

import websockets

HERE = "/home/hermes/.hermes/hermes-agent/.worktrees/t_5bb8406e/bench-ws-deflate"
PY_SPY = "/home/hermes/.local/bin/py-spy"


async def _spawn(*argv, **kw):
    """subprocess.Popen blocks the event loop while the kernel forks; hand it to a thread."""
    return await asyncio.to_thread(subprocess.Popen, list(argv), **kw)


async def _run(*argv, **kw):
    """subprocess.run waits for the child; on the loop that stalls every other connection."""
    kw.setdefault("check", False)
    return await asyncio.to_thread(subprocess.run, list(argv), **kw)


async def client(port: int, mode: str) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{port}/bench?mode={mode}&repeat=100000",
                                 compression="deflate", max_size=None) as ws:
        while True:
            try:
                await ws.recv()
            except websockets.ConnectionClosed:
                return


async def loader(port: int, mode: str, clients: int) -> None:
    await asyncio.gather(*(client(port, mode) for _ in range(clients)))


def analyse(raw: str) -> dict:
    total = deflate = send = 0
    for line in open(raw):
        stack, _, n = line.rstrip("\n").rpartition(" ")
        try:
            count = int(n)
        except ValueError:
            continue
        total += count
        frames = [f.strip() for f in stack.split(";")]
        if any("permessage_deflate" in f or "zlib" in f.split("(")[0] for f in frames):
            deflate += count
        elif any("websockets" in f and "send" in f for f in frames):
            send += count
    return {"samples": total, "deflate": deflate, "ws_send_other": send,
            "deflate_share_of_busy_pct": round(100 * deflate / total, 2) if total else 0.0}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="on",
                    help="server_app.py variant name: on|off|level1|level4|memlevel1|notakeover")
    ap.add_argument("--extra", default="",
                    help="extra server_app.py flags, space-separated (e.g. '--level 1')")
    ap.add_argument("--mode", default="delta-coalesced")
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--rate", type=int, default=100)
    args = ap.parse_args()

    index = ("on", "off", "level1", "level4", "memlevel1", "notakeover").index(args.variant)
    port = 18401 + index
    extra = args.extra.split() if args.extra else []
    srv = await _spawn(sys.executable, f"{HERE}/server_app.py", "--port", str(port), *extra,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raw = f"/tmp/bench-{args.variant}-{os.getpid()}.raw"
    try:
        for _ in range(120):
            try:
                async with websockets.connect(f"ws://127.0.0.1:{port}/bench?mode=card&repeat=0",
                                             compression="deflate", max_size=None) as ws:
                    while True:
                        try:
                            await ws.recv()
                        except websockets.ConnectionClosed:
                            break
                break
            except OSError:
                await asyncio.sleep(0.25)

        load = asyncio.create_task(loader(port, args.mode, args.clients))
        await asyncio.sleep(2)  # let the load ramp past connection setup
        t0 = time.perf_counter()
        # ptrace_scope=1 lets only a parent attach, and py-spy here is a sibling of the server, so
        # the attach runs under sudo (py-spy then writes the file as root, hence the chmod below).
        proc = await _run(
            "sudo", "-n", PY_SPY, "record", "-d", str(args.seconds), "-r", str(args.rate),
            "-f", "raw", "-o", raw, "--pid", str(srv.pid), "--nonblocking", "--threads",
            capture_output=True, text=True, timeout=args.seconds + 120)
        await _run("sudo", "-n", "chmod", "644", raw, capture_output=True, timeout=60)
        wall = time.perf_counter() - t0
        sys.stderr.write((proc.stdout + proc.stderr)[-600:] + "\n")
        load.cancel()
        try:
            await load
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass
        result = {"variant": args.variant, "extra": extra, "mode": args.mode,
                  "clients": args.clients,
                  "profile_seconds": round(wall, 2), **analyse(raw)}
        print(json.dumps(result))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    asyncio.run(main())
