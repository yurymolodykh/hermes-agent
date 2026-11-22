"""Probe for t_f8450188: count real sqlite connects on the dashboard read path.

Runs N sequential + M concurrent ``_open_session_db_at_path(read_only=True)`` cycles over a
temp HERMES_HOME and reports how many times a NEW sqlite connection was actually opened.
Run with HOME and HERMES_HOME pointed away from the real hermes home.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="t-f8450188-poolprobe-"))
_HOME = _TMP / "home"
_HOME.mkdir(parents=True, exist_ok=True)
(_HOME / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
    "synchronous: FULL\nsessions:\n  auto_archive: false\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(_HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ.setdefault("HERMES_STATE_DB_GUARD_BYPASS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3  # noqa: E402

from hermes_state import SessionDB  # noqa: E402

_connects = {"n": 0}
_orig_connect = sqlite3.connect


def _counting_connect(*a, **kw):
    _connects["n"] += 1
    return _orig_connect(*a, **kw)


def seed(db_path: Path) -> None:
    db = SessionDB(db_path=db_path)
    for i in range(60):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(4):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant", content=f"m{i}-{j}")
    db.close()


def read_once(db_path: Path) -> int:
    d = _open(db_path, read_only=True)
    try:
        rows = d.list_sessions_rich(limit=50, compact_rows=True)
        now = time.time()
        return sum(1 for x in rows
                   if x.get("ended_at") is None
                   and (now - x.get("last_active", x.get("started_at", 0))) < 300)
    finally:
        d.close()


from hermes_cli.web_server_sessions import _open_session_db_at_path as _open  # noqa: E402


def main() -> int:
    baseline = "--baseline" in sys.argv
    db_path = _HOME / "state.db"
    seed(db_path)
    sqlite3.connect = _counting_connect  # count AFTER the seed writer is closed

    from hermes_cli.read_handle_pool import get_pool
    get_pool().clear()

    if baseline:
        import hermes_cli.read_handle_pool as rhp

        class _NoPool:
            def acquire(self, key, identity):
                return None

            def release(self, key, db, identity):
                db.close()
                return True

            def discard(self, db):
                db.close()

            def clear(self):
                pass

            def drop_path(self, key):
                pass

        rhp.get_pool = lambda: _NoPool()

    # 1) sequential: pool must warm once and then stop opening connections
    _connects["n"] = 0
    per = []
    for _ in range(20):
        t = time.perf_counter()
        read_once(db_path)
        per.append((time.perf_counter() - t) * 1000)
    per.sort()
    seq_ms = per[len(per) // 2]
    seq_connects = _connects["n"]

    # 2) concurrent: 8 workers x 5 rounds
    _connects["n"] = 0
    errs = []

    def worker():
        try:
            for _ in range(5):
                read_once(db_path)
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conc_wall = (time.perf_counter() - t0) * 1000
    conc_connects = _connects["n"]

    idle = sum(len(v) for v in get_pool()._idle.values())
    fd = len(os.listdir("/proc/self/fd"))
    print(f"sequential_40_reads: connects={seq_connects} per_read_ms={seq_ms:.2f}")
    print(f"concurrent_8x5:      connects={conc_connects} wall_ms={conc_wall:.0f} errors={len(errs)}")
    print(f"idle_pooled={idle} open_fds={fd}")
    if errs:
        print("first_error:", errs[0])
    get_pool().clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
