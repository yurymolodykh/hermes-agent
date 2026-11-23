"""Per-request cost attribution for the three t_77d98767 hotspots (call count + inclusive time).

Runs the real app in-process, wraps the hot functions with counters, drives the same workload the
acceptance criteria name (8 concurrent /api/status), and prints per-function call counts and the
wall time spent inside them. Usage: python probe_hotspot_cost.py <tree-root>
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

TREE = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent)
_TMP = Path(tempfile.mkdtemp(prefix="t77-cost-"))
_HOME = _TMP / "home"
_HOME.mkdir(parents=True, exist_ok=True)
(_HOME / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
    "  synchronous: FULL\nsessions:\n  auto_archive: false\n", encoding="utf-8")
(_HOME / ".env").write_text("".join(f"KEY_{i}=value-{i}-{'x' * 60}\n" for i in range(200)),
                            encoding="utf-8")
os.environ["HERMES_HOME"] = str(_HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ.setdefault("HERMES_STATE_DB_GUARD_BYPASS", "1")
sys.path.insert(0, TREE)

STATS: dict[str, list] = {}


def wrap(mod, name: str, label: str) -> None:
    orig = getattr(mod, name)
    rec = STATS.setdefault(label, [0, 0.0])

    def inner(*a, **kw):
        t = time.perf_counter()
        try:
            return orig(*a, **kw)
        finally:
            rec[0] += 1
            rec[1] += time.perf_counter() - t

    inner.__wrapped__ = orig
    setattr(mod, name, inner)


def main() -> int:
    from hermes_state import SessionDB

    db = SessionDB(db_path=_HOME / "state.db")
    for i in range(120):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(4):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant", content=f"m{i}-{j}")
    db.close()

    import agent.secret_scope as ss
    import hermes_state_readpool as rp
    import tools.skills_tool as st

    wrap(ss, "load_env_file", "secret_scope.load_env_file")
    wrap(st, "_dir_tree_signature", "skills_tool._dir_tree_signature")
    wrap(st, "_skills_scan_signature", "skills_tool._skills_scan_signature")
    wrap(rp, "_open_fd_count", "readpool._open_fd_count")
    wrap(rp, "_fd_headroom_ok", "readpool._fd_headroom_ok")
    # The gateway config layer resolves the profile .env on the status path too.
    try:
        import gateway.config_env as ce
        for cand in ("_apply_env_overrides", "env_overrides"):
            if hasattr(ce, cand):
                wrap(ce, cand, f"gateway.config_env.{cand}")
    except Exception as exc:  # pragma: no cover - diagnostic only
        print("config_env wrap skipped:", exc)

    import uvicorn

    from hermes_cli import web_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
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
            time.sleep(0.1)
    else:
        print("server never came up")
        return 1
    for _ in range(5):
        urllib.request.urlopen(url, timeout=30).read()
    for k in STATS:
        STATS[k][0] = 0
        STATS[k][1] = 0.0

    def one() -> None:
        with urllib.request.urlopen(url, timeout=60) as r:
            json.loads(r.read())

    t0 = time.perf_counter()
    for _ in range(100):
        one()
    bursts = 5
    for _ in range(bursts):
        barrier = threading.Barrier(8)
        ts = [threading.Thread(target=lambda: (barrier.wait(), one())) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    wall = time.perf_counter() - t0
    requests = 100 + bursts * 8
    print(f"workload: {requests} requests in {wall:.2f}s ({requests / wall:.0f} req/s)")
    for label, (n, spent) in sorted(STATS.items(), key=lambda kv: -kv[1][1]):
        print(f"{label:44s} calls={n:6d}  calls/req={n / requests:7.2f}  "
              f"spent={spent * 1000:8.1f} ms  {spent / wall * 100:6.2f}% of wall  "
              f"{spent / n * 1e6 if n else 0:8.1f} us/call")
    server.should_exit = True
    th.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
