"""Baseline/after measurement for the state.db read-only open seam (t_f8450188).

Measures the exact code path /api/status and the dashboard session routes use:
``_open_session_db_for_profile(profile, read_only=True)`` -> one read -> ``close()``.

Usage:  python bench_readonly_open.py [--reps N] [--concurrency N]
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="t-f8450188-bench-"))
os.environ["HERMES_HOME"] = str(_TMP / "home")
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ.setdefault("HERMES_STATE_DB_GUARD_BYPASS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_state import SessionDB  # noqa: E402


def _seed(path: Path, sessions: int = 200) -> None:
    db = SessionDB(db_path=path)
    for i in range(sessions):
        sid = f"s{i}"
        db.create_session(session_id=sid, source="cli", model="m")
        for j in range(4):
            db.append_message(sid, role="user" if j % 2 == 0 else "assistant", content=f"msg {i}-{j}")
    db.close()


def _one_request(home_db: Path, fn) -> float:
    from hermes_cli.web_server_sessions import _open_session_db_at_path

    t0 = time.perf_counter()
    db = _open_session_db_at_path(home_db, read_only=True)
    try:
        fn(db)
    finally:
        db.close()
    return time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=120)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    home = _TMP / "home"
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "state.db"
    _seed(db_path)

    def work(db):
        sessions = db.list_sessions_rich(limit=50, compact_rows=True)
        now = time.time()
        return sum(1 for s in sessions
                   if s.get("ended_at") is None
                   and (now - s.get("last_active", s.get("started_at", 0))) < 300)

    # Warm up (imports, config cache, first open)
    for _ in range(5):
        _one_request(db_path, work)

    serial = [_one_request(db_path, work) for _ in range(args.reps)]
    serial_ms = statistics.median(serial) * 1000

    # Concurrent burst: 8 simultaneous "requests", each its own thread (anyio worker pool shape)
    burst_times: list[float] = []
    for _ in range(5):
        barrier = threading.Barrier(args.concurrency)
        results: list[float] = [0.0] * args.concurrency

        def runner(idx: int) -> None:
            barrier.wait()
            results[idx] = _one_request(db_path, work)

        threads = [threading.Thread(target=runner, args=(i,)) for i in range(args.concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        burst_times.append(max(results))

    print(f"serial median open+read+close : {serial_ms:.3f} ms")
    print(f"serial p95                    : {sorted(serial)[int(len(serial) * 0.95)] * 1000:.3f} ms")
    print(f"burst of {args.concurrency} wall (worst of 5): {max(burst_times) * 1000:.3f} ms")
    print(f"burst median                  : {statistics.median(burst_times) * 1000:.3f} ms")

    try:
        from hermes_state_readonly_pool import stats as pool_stats
        print("pool stats:", pool_stats())
    except Exception:
        print("pool stats: (no pool module)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
