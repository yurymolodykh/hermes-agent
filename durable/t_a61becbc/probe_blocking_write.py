"""Direct probe: does an 8 MB ``POST /api/fs/write-text`` stall the serving event loop?

The concurrency curve in ``bench_curve.py`` cannot answer this: it measures routes whose cost is
SQLite or cached reads, and the loop-lag monitor it lacks. This probe drives ONLY the handler the
card moved off the loop and asks the loop itself, from inside the server process, whether it
stalled.

Server process = the real ``hermes_cli.web_server.app`` under uvicorn on a temp HERMES_HOME, with
a 10 ms loop-lag monitor task (same shape as ``bench_loop.py``). Client:

  start monitor -> POST /api/fs/write-text (8 MiB payload) -> meanwhile GET /api/status every
  20 ms -> stop monitor

Two runs of the same probe are the A/B:

  HERMES_HANDLER_OFFLOAD=1  off-loop  (the shipped path)
  HERMES_HANDLER_OFFLOAD=0  inline    (the pre-change path, i.e. the emergency rollback)

So the same file both measures the fix and proves the kill switch: with the switch off the loop
must stall again.

Usage: python3 probe_blocking_write.py --out durable/t_a61becbc/probe-write-after.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])
TOKEN = ""

_SERVER = r'''
import asyncio, json, os, socket, sys, threading, time
from pathlib import Path
REPO, HOME = sys.argv[1], Path(sys.argv[2])
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
sys.path.insert(0, REPO)

import uvicorn
from hermes_cli import web_server

LAG = []
RECORDING = [False]

async def monitor():
    tick = 0.01
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(tick)
        over = (time.perf_counter() - t0 - tick) * 1000.0
        if RECORDING[0]:
            LAG.append(over)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]

server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port,
                                       log_level="warning", log_config=None))

_orig = server.startup
async def _startup(sockets=None):
    await _orig(sockets)
    asyncio.create_task(monitor())
server.startup = _startup

th = threading.Thread(target=server.run, daemon=True); th.start()
target = HOME / "probe-target.txt"
target.write_text("seed", encoding="utf-8")
print(f"READY {port} {getattr(web_server, '_SESSION_TOKEN', '')} {target}", flush=True)
for line in sys.stdin:
    cmd = line.strip()
    if cmd == "start":
        LAG.clear(); RECORDING[0] = True
    elif cmd == "stop":
        RECORDING[0] = False
        vals = sorted(LAG)
        n = len(vals)
        print("LAG " + json.dumps({
            "samples": n,
            "p50_ms": round(vals[n // 2], 2) if n else 0,
            "p99_ms": round(vals[int(n * 0.99)] if n else 0, 2),
            "max_ms": round(vals[-1], 2) if n else 0,
            "over_50ms": sum(1 for v in vals if v > 50),
            "over_200ms": sum(1 for v in vals if v > 200),
        }), flush=True)
    elif cmd == "quit":
        break
server.should_exit = True
th.join(timeout=20)
'''


def _post(url: str, payload: dict) -> tuple[int, float, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            text = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        code = exc.code
    return code, (time.perf_counter() - t0) * 1000, text[:400]


def _get(url: str) -> tuple[int, float]:
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        code = exc.code
    return code, (time.perf_counter() - t0) * 1000


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--mib", type=int, default=8)
    ap.add_argument("--parallel", type=int, default=8,
                    help="concurrent write-text requests (8 MiB each by default)")
    ap.add_argument("--offload", default=None, help="HERMES_HANDLER_OFFLOAD for the server (1/0)")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="t-a61-probe-"))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: letovo/deepseek-v4-flash\n"
        "database:\n  cache_size: -8000\n  synchronous: FULL\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    if args.offload is not None:
        env["HERMES_HANDLER_OFFLOAD"] = args.offload
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, REPO, str(home)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
    line = proc.stdout.readline().strip()
    if not line.startswith("READY"):
        print("server failed:", line, proc.stdout.read()[:4000])
        proc.kill()
        return 1
    parts = line.split(" ", 3)
    port, TOKEN, target = parts[1], parts[2], parts[3]
    base = f"http://127.0.0.1:{port}"
    for _ in range(900):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.05)

    content = "x" * (args.mib * 1024 * 1024)
    payload = {"path": target, "content": content}

    # warm: a small write so imports/connections are settled before the measured one
    _post(base + "/api/fs/write-text", {"path": target, "content": "warm"})
    _get(base + "/api/status")
    mode = ""
    status_polls: list[float] = []

    proc.stdin.write("start\n"); proc.stdin.flush()
    started = time.perf_counter()

    results: list[tuple[int, float, str]] = []
    barrier = threading.Barrier(args.parallel)

    def writer(i: int):
        barrier.wait()
        results.append(_post(base + "/api/fs/write-text", payloads[i]))

    payloads = [payload] * args.parallel
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(args.parallel)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        code_s, ms = _get(base + "/api/status")
        status_polls.append(ms)
        time.sleep(0.02)
    for t in threads:
        t.join()
    wall = (time.perf_counter() - started) * 1000

    def _read_lag() -> dict:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                raise SystemExit("server closed stdout before reporting lag")
            if raw.startswith("LAG "):
                return json.loads(raw.strip().removeprefix("LAG "))

    proc.stdin.write("stop\n"); proc.stdin.flush()
    lag = _read_lag()

    # which serving mode did the server actually settle on?
    try:
        code_s, ms, st_body = _get_json(base + "/api/status")
        mode = (st_body.get("handler_offload") or {}).get("mode", "")
    except Exception:
        pass

    ordered = sorted(status_polls)
    lat = sorted(r[1] for r in results)
    out = {
        "label": args.label,
        "offload_env": args.offload,
        "serving_mode": mode,
        "payload_mib": args.mib,
        "parallel": args.parallel,
        "writes": {
            "n": len(results),
            "statuses": sorted({r[0] for r in results}),
            "p50_ms": round(lat[len(lat) // 2], 1) if lat else 0,
            "max_ms": round(lat[-1], 1) if lat else 0,
            "errors": [r[2] for r in results if r[0] != 200][:3],
            "burst_wall_ms": round(wall, 1),
        },
        "status_polls": {
            "n": len(ordered),
            "p50_ms": round(ordered[len(ordered) // 2], 1) if ordered else 0,
            "p99_ms": round(ordered[int(len(ordered) * 0.99)], 1) if ordered else 0,
            "max_ms": round(ordered[-1], 1) if ordered else 0,
        },
        "loop_lag": lag,
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    proc.stdin.write("quit\n"); proc.stdin.flush()
    proc.wait(timeout=60)
    return 0


def _get_json(url: str):
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, (time.perf_counter() - t0) * 1000, json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
