"""Before/after bench for t_77d98767: py-spy on the real dashboard app under 8 concurrent
/api/status, plus per-hotspot frame shares and process CPU.

Attach-based sampling is denied (yama ptrace_scope=1, py-spy is a sibling of the server), so
py-spy LAUNCHES the server (``--``) and stays its ancestor; this process only drives the load.

    ../../.venv/bin/python bench_obv.py <tree-root> [out.json] [window_s]
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
    "secret_scope.load_env_file": ("secret_scope.py", "load_env_file"),
    "skills_tool._dir_tree_signature": ("skills_tool.py", "_dir_tree_signature"),
    "readpool._open_fd_count": ("hermes_state_readpool.py", "_open_fd_count"),
    "wal._on_disk_journal_mode": ("hermes_state_wal.py", "_on_disk_journal_mode"),
}

_SEED = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["BENCH_TREE"])
from hermes_state import SessionDB
home = Path(os.environ["BENCH_HOME"])
db = SessionDB(db_path=home / "state.db")
for i in range(120):
    db.create_session(session_id=f"s{i}", source="cli", model="m")
    for j in range(4):
        db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant", content=f"m{i}-{j}")
db.close()
'''

_SERVER = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["BENCH_TREE"])
import uvicorn
from hermes_cli import web_server
Path(os.environ["BENCH_HOME"], "server.pid").write_text(str(os.getpid()))
uvicorn.run(web_server.app, host="127.0.0.1", port=int(os.environ["BENCH_PORT"]), log_level="warning")
'''

CONFIG = (
    "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
    "  synchronous: FULL\nsessions:\n  auto_archive: false\n"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ready(url: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=3).read()
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(fields[13]) + int(fields[14])) / ticks


def run(tree: str, window: int = 15, burst: int = 8) -> dict:
    home = Path(tempfile.mkdtemp(prefix="t77-bench-"))
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    port = _free_port()
    env = dict(os.environ)
    env.update({"HERMES_HOME": str(home), "HERMES_TEST_ISOLATION": "1",
                "HERMES_STATE_DB_GUARD_BYPASS": "1", "BENCH_HOME": str(home),
                "BENCH_TREE": tree, "BENCH_PORT": str(port)})
    (home / "seed.py").write_text(_SEED, encoding="utf-8")
    (home / "server.py").write_text(_SERVER, encoding="utf-8")
    seed = subprocess.run([sys.executable, str(home / "seed.py")], env=env, cwd=tree,
                          capture_output=True, text=True)
    if seed.returncode:
        raise SystemExit("seed failed: " + seed.stderr[-3000:])

    profile = home / "profile.txt"
    spy = subprocess.Popen(
        ["py-spy", "record", "-d", str(window), "-f", "raw", "-o", str(profile), "--",
         sys.executable, str(home / "server.py")],
        env=env, cwd=tree, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}/api/status"
    if not _ready(url, 90):
        spy.kill()
        raise SystemExit("server never came up")
    pid = int((home / "server.pid").read_text())

    cpu0 = _cpu_seconds(pid)
    t0 = time.perf_counter()
    stop = threading.Event()
    lat: list[float] = []

    def worker() -> None:
        while not stop.is_set():
            s = time.perf_counter()
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    json.loads(r.read())
                lat.append((time.perf_counter() - s) * 1000)
            except Exception:
                pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(burst)]
    for t in threads:
        t.start()
    while time.perf_counter() - t0 < window + 2:
        if spy.poll() is not None:
            break
        time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join(timeout=60)
    wall = time.perf_counter() - t0
    cpu1 = _cpu_seconds(pid)
    try:
        spy.wait(timeout=30)
    except subprocess.TimeoutExpired:
        spy.kill()
    err = (spy.stderr.read() or "")[-1500:] if spy.stderr else ""
    if err.strip():
        print("py-spy stderr:", err, file=sys.stderr)
    if not profile.exists():
        raise SystemExit("no profile written")
    lat.sort()

    counts: dict[str, int] = {}
    total = 0
    for line in profile.read_text().splitlines():
        stack, _, cnt = line.rpartition(" ")
        try:
            n = int(cnt)
        except ValueError:
            continue
        total += n
        for name, (fname, func) in HOTSPOTS.items():
            if any(fname in frame and func in frame for frame in stack.split(";")):
                counts[name] = counts.get(name, 0) + n
    cpu_s = max(cpu1 - cpu0, 0.0)
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
        "hotspot_share_pct": {k: round(v / total * 100, 2) for k, v in counts.items()},
        "hotspot_share_sum_pct": round(sum(counts.values()) / total * 100, 2),
    }
    return out


if __name__ == "__main__":
    tree = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    win = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    result = run(tree, window=win)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if out_path:
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
