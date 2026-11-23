"""SQLite thread-safety probe: does anything hand a connection across threads?

Card requirement #3: a session DB connection must be opened AND closed inside the worker
function (``sessions.py::_with_db`` is the pattern). If a ``SessionDB`` object crosses from the
loop into a worker, sqlite3 raises ``ProgrammingError: SQLite objects created in a thread can
only be used in that same thread``.

This drives the read routes that DO run off the loop (sessions list / messages / search) with
8 concurrent requests each and then asserts the pattern did not appear anywhere in the server's
own output — the server's stderr is captured, not discarded.

Usage: python3 probe_sqlite_threads.py --out durable/t_a61becbc/probe-sqlite-threads.json
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
                                       log_level="info", log_config=None))
th = threading.Thread(target=server.run, daemon=True); th.start()
print(f"READY {port} {getattr(web_server, '_SESSION_TOKEN', '')}", flush=True)
sys.stdin.readline()
server.should_exit = True
th.join(timeout=20)
'''


def _get(url: str) -> tuple[int, float]:
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        resp.read()
        return resp.status, (time.perf_counter() - t0) * 1000


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="t-a61-sqlite-"))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: letovo/deepseek-v4-flash\n"
        "database:\n  cache_size: -8000\n  synchronous: FULL\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    env["HERMES_HANDLER_OFFLOAD"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, REPO, str(home), "200", "12"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
    collected: list[str] = []

    def drain():
        for line in proc.stdout:
            collected.append(line.rstrip("\n"))

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    first = ""
    for _ in range(3000):
        if collected:
            first = collected[0]
            if first.startswith("READY"):
                break
        time.sleep(0.01)
    if not first.startswith("READY"):
        print("server failed:", first, "".join(collected)[:3000])
        proc.kill()
        return 1
    _parts = first.split(" ", 2)
    port, TOKEN = _parts[1], _parts[2]
    base = f"http://127.0.0.1:{port}"
    for _ in range(900):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.05)

    routes = {
        "sessions": "/api/sessions?limit=50",
        "messages": "/api/sessions/s0/messages?limit=500&include_compacted=1",
        "search": "/api/sessions/search?q=body&limit=20",
    }
    out = {"parallel": args.parallel, "routes": {}}
    for name, path in routes.items():
        _get(base + path)
        results: list[tuple[int, float]] = []
        barrier = threading.Barrier(args.parallel)

        def worker(p=path):
            barrier.wait()
            results.append(_get(base + p))

        threads = [threading.Thread(target=worker) for _ in range(args.parallel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        codes = sorted({c for c, _ in results})
        lat = sorted(ms for _, ms in results)
        # Any non-200 is itself a finding (5xx on a read route means the worker path broke).
        out["routes"][name] = {"codes": codes, "p50_ms": round(lat[len(lat) // 2], 1),
                              "max_ms": round(lat[-1], 1)}
        print(name, out["routes"][name], flush=True)

    proc.stdin.write("stop\n"); proc.stdin.flush()
    proc.wait(timeout=60)
    reader.join(timeout=20)
    log = "\n".join(collected)
    markers = ["SQLite objects created in a thread", "ProgrammingError",
               "check_same_thread", "Operation on closed database", "database is locked"]
    out["log_markers"] = {m: log.count(m) for m in markers}
    out["log_lines"] = len(collected)
    out["sqlite_thread_error"] = any(out["log_markers"][m] for m in markers)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 1 if out["sqlite_thread_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
