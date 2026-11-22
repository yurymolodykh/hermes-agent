"""Hammer the read-only attach path so py-spy can attribute its cost (t_f8450188)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="t-f8450188-spin-"))
os.environ["HERMES_HOME"] = str(_TMP / "home")
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ.setdefault("HERMES_STATE_DB_GUARD_BYPASS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_state import SessionDB  # noqa: E402


def main() -> int:
    home = _TMP / "home"
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "state.db"
    db = SessionDB(db_path=db_path)
    for i in range(200):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(4):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant", content=f"msg {i}-{j}")
    db.close()

    from hermes_cli.web_server_sessions import _open_session_db_at_path

    deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 20.0)
    n = 0
    while time.time() < deadline:
        d = _open_session_db_at_path(db_path, read_only=True)
        try:
            s = d.list_sessions_rich(limit=50, compact_rows=True)
            now = time.time()
            sum(1 for x in s if x.get("ended_at") is None
                and (now - x.get("last_active", x.get("started_at", 0))) < 300)
        finally:
            d.close()
        n += 1
    print(f"iterations: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
