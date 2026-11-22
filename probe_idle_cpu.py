"""Idle gateway CPU with the dashboard polling /api/status (t_f8450188).

The gateway "idles" at a large share of one core because the dashboard polls the read API.
This runs the REAL app under uvicorn over a temp HERMES_HOME, polls /api/status once a second
from one thread, and reports process CPU seconds over the window as a percentage of one core.

Before/after: run with BASE_TREE=<merge-base worktree> to measure the unpatched tree.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="t_f8450188-idle-"))
_HOME = _TMP / "home"
_HOME.mkdir(parents=True, exist_ok=True)
(_HOME / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
    "  synchronous: FULL\nsessions:\n  auto_archive: false\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(_HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ.setdefault("HERMES_STATE_DB_GUARD_BYPASS", "1")

base_tree = os.environ.get("BASE_TREE")
sys.path.insert(0, base_tree or str(Path(__file__).resolve().parent))

from hermes_state import SessionDB


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    db_path = _HOME / "state.db"
    db = SessionDB(db_path=db_path)
    for i in range(120):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(4):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant",
                              content=f"m{i}-{j}")
    db.close()

    import uvicorn

    from hermes_cli import web_server

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port,
                                          log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}/api/status"
    for _ in range(300):
        try:
            urllib.request.urlopen(url, timeout=2).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        print("server never came up")
        return 1

    # warm the read path the way a dashboard does before the measurement window
    for _ in range(5):
        urllib.request.urlopen(url, timeout=10).read()

    stop = threading.Event()
    polls = {"n": 0}

    def poller() -> None:
        while not stop.is_set():
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    json.loads(r.read())
                polls["n"] += 1
            except Exception:
                pass
            stop.wait(1.0)

    pth = threading.Thread(target=poller, daemon=True)
    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    pth.start()
    window = float(os.environ.get("WINDOW_S", "30"))
    time.sleep(window)
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - wall0
    stop.set()
    pth.join(timeout=25)

    pct = cpu / wall * 100.0
    print(f"window_s={wall:.1f} polls={polls['n']} cpu_s={cpu:.2f} "
          f"idle_core_pct={pct:.1f}")

    server.should_exit = True
    th.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
