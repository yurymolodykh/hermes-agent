"""Before/after bench for t_77d98767: 15 s py-spy on a real gateway app under 8 concurrent
/api/status, plus the per-hotspot frame shares.

The app runs in its OWN subprocess (so py-spy sees only server work, and the client threads do
not dilute the profile). Usage:

    ../../.venv/bin/python bench_obv.py <tree-root> [out.json]

<tree-root> is the checkout whose ``hermes_cli.web_server`` and ``agent``/``tools`` modules are
imported: this worktree for "after", a merge-base export for "before".
"""
from __future__ import annotations

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

HOTSPOTS = {
    "secret_scope.load_env_file": ("agent/secret_scope.py", "load_env_file"),
    "skills_tool._dir_tree_signature": ("tools/skills_tool.py", "_dir_tree_signature"),
    "readpool._open_fd_count": ("hermes_state_readpool.py", "_open_fd_count"),
}

_SERVER = r'''
import json, os, sys, threading, time
from pathlib import Path
home = Path(os.environ["BENCH_HOME"]); home.mkdir(parents=True, exist_ok=True)
(home / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
    "  synchronous: FULL\nsessions:\n  auto_archive: false\n", encoding="utf-8")
sys.path.insert(0, os.environ["BENCH_TREE"])
from hermes_state import SessionDB
db = SessionDB(db_path=home / "state.db")
for i in range(120):
    db.create_session(session_id=f"s{i}", source="cli", model="m")
    for j in range(4):
        db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant", content=f"m{i}-{j}")
db.close()
import uvicorn
from hermes_cli import web_server
port = int(os.environ["BENCH_PORT"])
server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port, log_level="warning"))
print("ready", flush=True)
server.run()
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, proc: subprocess.Popen) -> bool:
    for _ in range(600):
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _cpu_times(pid: int) -> tuple[float, float]:
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    ticks = os.sysconf("SC_CLK_TCK")
    return int(fields[13]) / ticks, int(fields[14]) / ticks


def run(tree: str, window: float = 15.0, burst: int = 8) -> dict:
    home = Path(tempfile.mkdtemp(prefix="t77-bench-"))
    port = _free_port()
    env = dict(os.environ)
    env.update({"HERMES_HOME": str(home), "HERMES_TEST_ISOLATION": "1",
                "HERMES_STATE_DB_GUARD_BYPASS": "1", "BENCH_HOME": str(home),
                "BENCH_TREE": tree, "BENCH_PORT": str(port)})
    script = home / "server.py"
    script.write_text(_SERVER, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], env=env, cwd=tree,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}/api/status"
    if not _wait_ready(url, proc):
        proc.kill()
        raise SystemExit(f"server failed: {proc.stdout.read()[-4000:] if proc.stdout else ''}")
    for _ in range(5):
        urllib.request.urlopen(url, timeout=30).read()

    profile = home / "profile.txt"
    spy = subprocess.Popen(["py-spy", "record", "-d", str(window), "-f", "raw",
                            "-o", str(profile), "-p", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    times = []
    lat: list[float] = []
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    json.loads(r.read())
                lat.append((time.perf_counter() - t0) * 1000)
            except Exception:
                pass

    cpu0 = _cpu_times(proc.pid)
    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(burst)]
    for t in threads:
        t.start()
    while time.perf_counter() - t0 < window:
        time.sleep(0.2)
        if spy.poll() is not None:
            break
    stop.set()
    for t in threads:
        t.join(timeout=30)
    wall = time.perf_counter() - t0
    cpu1 = _cpu_times(proc.pid)
    spy.wait(timeout=30)
    times.append(wall)
    lat.sort()

    # Frame shares from the folded profile: "stack;frame count".
    ctx_counts: dict[str, int] = {}
    total = 0
    for line in profile.read_text().splitlines():
        stack, _, cnt = line.rpartition(" ")
        try:
            n = int(cnt)
        except ValueError:
            continue
        total += n
        for frame in stack.split(";"):
            for name, (fname, func) in HOTSPOTS.items():
                if fname in frame and func in frame:
                    ctx_counts[name] = ctx_counts.get(name, 0) + n
        if any(f[0] in stack for f in HOTSPOTS.values()):
            pass
    shares = {k: round(v / total * 100, 2) for k, v in ctx_counts.items()}
    cpu_s = (cpu1[0] - cpu0[0]) + (cpu1[1] - cpu0[1])
    out = {
        "tree": tree,
        "samples": total,
        "window_s": round(wall, 1),
        "requests": len(lat),
        "cpu_s": round(cpu_s, 2),
        "core_pct_of_one": round(cpu_s / wall * 100, 1),
        "latency_ms_median": round(lat[len(lat) // 2], 1) if lat else None,
        "latency_ms_p95": round(lat[int(len(lat) * 0.95)], 1) if lat else None,
        "latency_ms_max": round(lat[-1], 1) if lat else None,
        "hotspot_share_pct": shares,
        "hotspot_share_sum_pct": round(sum(shares.values()), 2),
    }
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    return out


if __name__ == "__main__":
    tree = sys.argv[1]
    out = run(tree)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
