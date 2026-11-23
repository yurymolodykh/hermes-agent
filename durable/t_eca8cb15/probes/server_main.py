"""Server side of the two-process probe harness: runs the real dashboard app under
uvicorn on a free port, prints "READY <port> <token>", and serves until EOF on stdin.

Kept in its own process so the measuring client never shares a GIL with the server.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

REPO = sys.argv[1]
HOME = Path(sys.argv[2])
N_SESSIONS = int(sys.argv[3]) if len(sys.argv) > 3 else 200
MESSAGES = int(sys.argv[4]) if len(sys.argv) > 4 else 8

os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
sys.path.insert(0, REPO)


def main() -> int:
    from hermes_state import SessionDB

    db = SessionDB(db_path=HOME / "state.db")
    for i in range(N_SESSIONS):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(MESSAGES):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant",
                              content=f"message body {i}-{j} with some words to index")
    db.close()

    import uvicorn
    from hermes_cli import web_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port,
                                           log_level="warning", log_config=None))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    print(f"READY {port} {getattr(web_server, '_SESSION_TOKEN', '')}", flush=True)
    sys.stdin.readline()  # hold until the client says stop
    server.should_exit = True
    th.join(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
