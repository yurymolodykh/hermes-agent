"""Pool-exhaustion probe: does a burst wider than the worker pool queue, or fail?

Card requirement #2: "всплеск 32+ запросов, метрика queue depth/таймауты". This drives the real
web app over HTTP with a burst of N concurrent ``POST /api/fs/write-text`` calls (N > workers,
N > 64 to also exceed the CPU-count-derived pool on a small box) and reads back the seam's own
counters from ``/api/status``:

  * ``max_queue_depth`` must rise above 0 — proof the pool really is bounded and the excess
    waits, rather than a thread per request;
  * every request must still answer 200 — no 503, no timeout, no dropped connection;
  * ``workers`` must stay at the configured size.

Usage: python3 probe_pool.py --out durable/t_a61becbc/probe-pool.json
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
import os, socket, sys, threading
from pathlib import Path
REPO, HOME = sys.argv[1], Path(sys.argv[2])
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
sys.path.insert(0, REPO)
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


def _post(url: str, payload: dict, timeout: float = 300):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return resp.status, (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, (time.perf_counter() - t0) * 1000
    except Exception as exc:  # noqa: BLE001 - a timeout/refusal is a finding
        return f"exc:{type(exc).__name__}", (time.perf_counter() - t0) * 1000


def _status(base: str) -> dict:
    req = urllib.request.Request(base + "/api/status")
    if TOKEN:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--burst", type=int, default=64)
    ap.add_argument("--kb", type=int, default=256)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="t-a61-pool-"))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: letovo/deepseek-v4-flash\n"
        "database:\n  cache_size: -8000\n  synchronous: FULL\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    env["HERMES_HANDLER_OFFLOAD"] = "1"
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, REPO, str(home)],
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

    before = _status(base)["handler_offload"]
    payload = {"path": str(home / "target.txt"), "content": "y" * (args.kb * 1024)}
    results = []
    barrier = threading.Barrier(args.burst)

    def worker(i: int):
        barrier.wait()
        results.append(_post(base + "/api/fs/write-text", payload))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.burst)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    burst_wall = (time.perf_counter() - started) * 1000

    after = _status(base)["handler_offload"]
    codes: dict[str, int] = {}
    for status, _ in results:
        codes[str(status)] = codes.get(str(status), 0) + 1
    lat = sorted(ms for _, ms in results)
    out = {
        "burst": args.burst,
        "payload_kb": args.kb,
        "workers": after.get("workers"),
        "codes": codes,
        "lat_p50_ms": round(lat[len(lat) // 2], 1) if lat else 0,
        "lat_max_ms": round(lat[-1], 1) if lat else 0,
        "burst_wall_ms": round(burst_wall, 1),
        "offload_before": before,
        "offload_after": after,
        "queued": after.get("max_queue_depth", 0) > 0,
        "all_ok": set(codes) == {"200"},
        "target_ok": (home / "target.txt").is_file(),
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    proc.stdin.write("stop\n"); proc.stdin.flush()
    proc.wait(timeout=60)
    return 0 if (out["all_ok"] and out["queued"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
