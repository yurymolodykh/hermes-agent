"""Two-process scaling harness: the dashboard runs in its OWN process (no shared GIL
with the measuring client), on a temp HERMES_HOME.

For each read route at concurrency 1/2/4/8/16 measure per-request latency and a light
/api/health probe fired 50 ms into the burst (event-loop responsiveness).

Usage: python3 probe_client.py [--wrap offloop]   -> writes JSON to $OUT
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "/home/hermes/.hermes/hermes-agent/.worktrees/t_eca8cb15"
TMP = Path(tempfile.mkdtemp(prefix="t-eca8-h-"))
HOME = TMP / "home"
HOME.mkdir(parents=True, exist_ok=True)
(HOME / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\n"
    "database:\n  cache_size: -8000\n  synchronous: FULL\n"
    "sessions:\n  auto_archive: false\n", encoding="utf-8")

TOKEN = ""


def timed(url: str) -> float:
    req = urllib.request.Request(url)
    if TOKEN and "/api/" in url:
        req.add_header("X-Hermes-Session-Token", TOKEN)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000


def concurrent(jobs):
    barrier = threading.Barrier(len(jobs))
    out = [0.0] * len(jobs)

    def run(i):
        barrier.wait()
        out[i] = jobs[i]()

    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(jobs))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def with_probe(url, jobs):
    box = {}

    def light():
        time.sleep(0.05)
        box["ms"] = timed(url)

    lt = threading.Thread(target=light)
    lt.start()
    lat = concurrent(jobs)
    lt.join()
    return lat, box.get("ms")


def main() -> int:
    global TOKEN
    wrap = "offloop" in sys.argv
    server_py = HERE / ("server_offloop.py" if wrap else "server_main.py")
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    proc = subprocess.Popen([sys.executable, str(server_py), REPO, str(HOME)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    line = proc.stdout.readline().strip()
    if not line.startswith("READY"):
        print("server failed:", line, proc.stdout.read()[:2000])
        proc.kill()
        return 1
    _, port, TOKEN = line.split(" ", 2)
    base = f"http://127.0.0.1:{port}"
    for _ in range(400):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        print("server never came up")
        proc.kill()
        return 1

    urllib.request.urlopen(base + "/api/status", timeout=120).read()
    res = {"mode": "offloop" if wrap else "current", "token_present": bool(TOKEN)}

    def curve(name, make):
        rows = []
        for n in (1, 2, 4, 8, 16):
            lat, probe = with_probe(base + "/api/health", [make(i) for i in range(n)])
            rows.append({"n": n,
                          "each_p50_ms": round(sorted(lat)[len(lat) // 2], 1),
                          "each_max_ms": round(max(lat), 1),
                          "health_probe_ms": None if probe is None else round(probe, 1)})
        res[name] = rows

    curve("status", lambda i: (lambda: timed(base + "/api/status")))
    curve("sessions", lambda i: (lambda: timed(base + "/api/sessions?limit=20")))
    curve("messages", lambda i: (lambda: timed(base + f"/api/sessions/s{i}/messages?limit=500")))
    curve("search", lambda i: (lambda: timed(base + "/api/sessions/search?q=message&limit=20")))

    res["home"] = str(HOME)
    res["port"] = port
    proc.stdin.write("stop\n")
    proc.stdin.flush()
    proc.wait(timeout=30)
    out = os.environ.get("OUT", str(TMP / "result.json"))
    Path(out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
