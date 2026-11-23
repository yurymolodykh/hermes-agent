"""Concurrency curve: is a handler serialized by the event loop?

For each route, run n = 1/2/4/8 truly concurrent requests and report per-request
latency. A handler that blocks the loop scales ~linearly with n (total wall time
constant, each request ~n x the solo latency); an offloaded one stays flat.

Usage: python3 bench_curve.py --out durable/t_a61becbc/curve-before.json [--sessions 200]
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
import os, socket, sys, threading
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
with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port,
                                       log_level="warning", log_config=None))
th = threading.Thread(target=server.run, daemon=True); th.start()
print(f"READY {port} {getattr(web_server, '_SESSION_TOKEN', '')}", flush=True)
sys.stdin.readline()
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

    tmp = Path(tempfile.mkdtemp(prefix="t-a61-curve-"))
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
                            stderr=subprocess.STDOUT, text=True, env=env)
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
    else:
        print("server never came up")
        proc.kill()
        return 1

    routes = {
        "status": "/api/status",
        "sessions": "/api/sessions?limit=50",
        "messages": "/api/sessions/s0/messages?limit=500&include_compacted=1",
        "search": "/api/sessions/search?q=body&limit=20",
    }
    out = {"repo": REPO, "sessions": args.sessions, "messages": args.messages, "routes": {}}
    for name, path in routes.items():
        timed(base + path)  # warm
        timed(base + path)
        row = {}
        for n in (1, 2, 4, 8):
            lat = burst([(lambda p=path: timed(base + p)) for _ in range(n)])
            row[n] = {"p50_ms": round(sorted(lat)[len(lat) // 2], 1),
                      "max_ms": round(max(lat), 1),
                      "wall_ms": round(sum(lat) / n * 1.0, 1)}
        solo = row[1]["max_ms"] or 1.0
        row["slowdown_8x"] = round(row[8]["max_ms"] / solo, 2)
        out["routes"][name] = row
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    proc.stdin.write("stop\n")
    proc.stdin.flush()
    proc.wait(timeout=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
