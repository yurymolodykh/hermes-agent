"""CPU cost of the dashboard read path (t_f8450188): seconds of CPU per 100 reads.

The gateway idles at 66% of one core because every dashboard poll pays the per-request
overhead again. This measures exactly that overhead: CPU time the process burns to serve
N `_open_session_db_at_path(read_only=True)` + list + close cycles, on a warm store.

Run against the after-tree directly; run against the base with BASE_TREE=<dir>.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="t_f8450188-cpu-"))
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

from hermes_state import SessionDB  # noqa: E402


def main() -> int:
    db_path = _HOME / "state.db"
    db = SessionDB(db_path=db_path)
    for i in range(120):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(4):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant", content=f"m{i}-{j}")
    db.close()

    from hermes_cli.web_server_sessions import _open_session_db_at_path

    def one() -> None:
        d = _open_session_db_at_path(db_path, read_only=True)
        try:
            s = d.list_sessions_rich(limit=50, compact_rows=True)
            now = time.time()
            sum(1 for x in s if x.get("ended_at") is None
                and (now - x.get("last_active", x.get("started_at", 0))) < 300)
        finally:
            d.close()

    for _ in range(20):  # warm
        one()

    n = 200
    wall0 = time.perf_counter()
    cpu0 = time.process_time()
    for _ in range(n):
        one()
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - wall0

    try:
        from hermes_cli.read_handle_pool import get_pool
        get_pool().clear()
    except ModuleNotFoundError:  # base tree has no pool
        pass

    print(f"reads={n} cpu_s={cpu:.3f} wall_s={wall:.3f} "
          f"cpu_ms_per_read={cpu / n * 1000:.3f} wall_ms_per_read={wall / n * 1000:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
