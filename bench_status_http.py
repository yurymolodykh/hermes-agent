"""Baseline/after: 8 concurrent /api/status against an in-process app (t_f8450188).

Runs a real uvicorn server in a thread on a free port over a temp HERMES_HOME and hits
the real /api/status route over HTTP, so threadpool contention is exercised the way the
dashboard sees it.
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

_TMP = Path(tempfile.mkdtemp(prefix="t-f8450188-http-"))
_HOME = _TMP / "home"
_HOME.mkdir(parents=True, exist_ok=True)
(_HOME / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
    "  synchronous: FULL\nsessions:\n  auto_archive: false\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(_HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ.setdefault("HERMES_STATE_DB_GUARD_BYPASS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_state import SessionDB


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    db = SessionDB(db_path=_HOME / "state.db")
    for i in range(120):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(4):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant", content=f"m{i}-{j}")
    db.close()

    import uvicorn
    from hermes_cli import web_server

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port,
                                          log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}/api/status"
    for _ in range(200):
        try:
            urllib.request.urlopen(url, timeout=2).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        print("server never came up")
        return 1

    def one() -> float:
        t0 = time.perf_counter()
        with urllib.request.urlopen(url, timeout=float(os.environ.get("REQ_TIMEOUT","30"))) as r:
            json.loads(r.read())
        return time.perf_counter() - t0

    for _ in range(5):
        one()

    serial = sorted(one() for _ in range(15))
    print(f"serial /api/status median : {serial[len(serial) // 2] * 1000:.1f} ms")

    import faulthandler
    faulthandler.dump_traceback_later(float(os.environ.get("DUMP_AFTER","20")), repeat=False)
    bursts = []
    for _ in range(int(os.environ.get("BURSTS", "5"))):
        barrier = threading.Barrier(8)
        out = [0.0] * 8

        def run(i: int) -> None:
            barrier.wait()
            out[i] = one()

        ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        bursts.append(out)

    worst = max(max(b) for b in bursts)
    per_burst_max = [max(b) for b in bursts]
    print("8x concurrent: per-request max per burst ms:",
          ", ".join(f"{v * 1000:.0f}" for v in per_burst_max))
    print(f"8x concurrent worst request   : {worst * 1000:.0f} ms  (limit 500)")
    server.should_exit = True
    th.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
