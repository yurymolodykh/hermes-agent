"""Event-loop lag probe: WHO blocks the serving loop.

Runs the real dashboard app in its own process with a loop-lag monitor task
(10 ms cadence, records every overshoot). The client fires a burst of N
concurrent requests at one route; the monitor's max/p99 overshoot is the
measurable "the loop stalled" signal, independent of request latency.

Usage: python3 bench_loop.py --out durable/t_a61becbc/loop-before.json
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
import urllib.request
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])
TOKEN = ""

_SERVER = r'''
import asyncio, json, os, socket, sys, threading, time
from pathlib import Path
REPO, HOME, N, M = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
sys.path.insert(0, REPO)
from hermes_state import SessionDB
db = SessionDB(db_path=HOME / "state.db")
for i in range(N):
    db.create_session(session_id=f"s{i}", source="cli", model="m")
    for j in range(M):
        db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant",
                          content=f"message body {i}-{j} with enough words to make the FTS row real")
db.close()

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
print(f"READY {port} {getattr(web_server, '_SESSION_TOKEN', '')}", flush=True)
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


def timed(url: str) -> float:
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        resp.read()
    return (time.perf_counter() - t0) * 1000


def burst(jobs):
    barrier = threading.Barrier(len(jobs))
    out = [0.0] * len(jobs)

    def run(i):
        barrier.wait()
        out[i] = jobs[i]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(jobs))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sessions", type=int, default=400)
    ap.add_argument("--messages", type=int, default=12)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="t-a61-loop-"))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: letovo/deepseek-v4-flash\n"
        "database:\n  cache_size: -8000\n  synchronous: FULL\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, REPO, str(home),
                             str(args.sessions), str(args.messages)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
    line = proc.stdout.readline().strip()
    if not line.startswith("READY"):
        print("server failed:", line, proc.stdout.read()[:3000])
        proc.kill()
        return 1
    _, port, TOKEN = line.split(" ", 2)
    base = f"http://127.0.0.1:{port}"
    for _ in range(900):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.05)

    routes = {
        "status": "/api/status",
        "sessions": "/api/sessions?limit=50",
        "messages": "/api/sessions/s0/messages?limit=500&include_compacted=1",
        "search": "/api/sessions/search?q=body&limit=20",
        "skills": "/api/skills",
        "checkpoints": "/api/ops/checkpoints",
    }
    out = {"repo": REPO, "routes": {}}
    for name, path in routes.items():
        timed(base + path)
        timed(base + path)
        proc.stdin.write("start\n"); proc.stdin.flush()
        lat = burst([(lambda p=path: timed(base + p)) for _ in range(8)])
        proc.stdin.write("stop\n"); proc.stdin.flush()
        lag = json.loads(proc.stdout.readline().strip().removeprefix("LAG "))
        out["routes"][name] = {
            "lat_p50_ms": round(sorted(lat)[4], 1),
            "lat_max_ms": round(max(lat), 1),
            "loop_lag": lag,
        }
        print(name, out["routes"][name], flush=True)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    proc.stdin.write("quit\n"); proc.stdin.flush()
    proc.wait(timeout=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
