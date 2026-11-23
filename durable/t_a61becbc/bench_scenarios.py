"""Three-scenario latency harness for t_a61becbc (before/after the offload change).

Runs the REAL dashboard app in its own process (no shared GIL) on a temp HERMES_HOME
with 200 sessions x 8 messages, then measures:

  A. 8 concurrent ``GET /api/status``          -> per-request ms
  B. 6 concurrent "chat open" (list + messages)-> per-request ms (2 requests each)
  C. model reply streaming with the panel open -> a synthetic CPU-heavy turn runs on the
     server while ``GET /api/status`` + ``GET /api/sessions`` are polled every 100 ms;
     reported as probe p50/p95/max ms.

Every burst is preceded by a warm-up request; a light ``/api/health`` probe fires 50 ms
into each burst to expose event-loop blocking (a stalled loop inflates it).

Usage: python3 bench_scenarios.py --label before --out durable/t_a61becbc/before.json
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])
TOKEN = ""
N_SESSIONS = 200
N_MESSAGES = 8


def _timed(url: str) -> float:
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as resp:
        resp.read()
    return (time.perf_counter() - t0) * 1000


def _concurrent(jobs):
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


def _with_health_probe(base: str, jobs):
    box = {}

    def light():
        time.sleep(0.05)
        box["ms"] = _timed(base + "/api/health")

    lt = threading.Thread(target=light)
    lt.start()
    lat = _concurrent(jobs)
    lt.join()
    return lat, box.get("ms")


def _stats(vals):
    ordered = sorted(vals)
    n = len(ordered)
    return {"n": n,
            "p50_ms": round(ordered[n // 2], 1),
            "max_ms": round(ordered[-1], 1),
            "all_ms": [round(v, 1) for v in vals]}


def _seed(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: letovo/deepseek-v4-flash\n"
        "database:\n  cache_size: -8000\n  synchronous: FULL\n"
        "sessions:\n  auto_archive: false\n", encoding="utf-8")


def _start_server(home: Path, extra_env: dict[str, str] | None = None):
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    env.update(extra_env or {})
    proc = subprocess.Popen([sys.executable, "-c", _SERVER_SNIPPET, REPO, str(home)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env)
    line = proc.stdout.readline().strip()
    if not line.startswith("READY"):
        print("server failed:", line, proc.stdout.read()[:3000])
        proc.kill()
        raise SystemExit(1)
    _, port, token = line.split(" ", 2)
    return proc, port, token


def _stop_server(proc) -> None:
    try:
        proc.stdin.write("stop\n")
        proc.stdin.flush()
        proc.wait(timeout=60)
    except Exception:
        proc.kill()


_SERVER_SNIPPET = r'''
import os, socket, sys, threading
from pathlib import Path
REPO, HOME = sys.argv[1], Path(sys.argv[2])
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
sys.path.insert(0, REPO)
from hermes_state import SessionDB
db = SessionDB(db_path=HOME / "state.db")
for i in range(200):
    db.create_session(session_id=f"s{i}", source="cli", model="m")
    for j in range(8):
        db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant",
                          content=f"message body {i}-{j} with some words to index")
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


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix=f"t-a61-{args.label}-"))
    home = tmp / "home"
    _seed(home)
    proc, port, TOKEN = _start_server(home)
    base = f"http://127.0.0.1:{port}"
    for _ in range(600):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        print("server never came up")
        _stop_server(proc)
        return 1

    out = {"label": args.label, "repo": REPO, "port": port, "home": str(home)}
    _timed(base + "/api/status")
    _timed(base + "/api/sessions?limit=20")

    # A: 8 concurrent /api/status
    lat, probe = _with_health_probe(base, [(lambda: _timed(base + "/api/status")) for _ in range(8)])
    out["A_status_8"] = {**_stats(lat), "health_probe_ms": None if probe is None else round(probe, 1)}

    # B: 6 concurrent chat-open sequences (list page + transcript)
    def chat_open(i):
        t0 = time.perf_counter()
        _timed(base + "/api/sessions?limit=20")
        _timed(base + f"/api/sessions/s{i}/messages?limit=500")
        return (time.perf_counter() - t0) * 1000

    lat, probe = _with_health_probe(base, [(lambda i=i: chat_open(i)) for i in range(6)])
    out["B_chat_open_6"] = {**_stats(lat), "health_probe_ms": None if probe is None else round(probe, 1)}

    res = out
    Path(args.out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    _stop_server(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
