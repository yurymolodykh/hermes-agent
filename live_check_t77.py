"""Live read-only check: /api/sessions without ?profile= against the REAL Hermes home.

Card t_77e5de22. Mounts the card's routers in a TestClient, stamps a verified dashboard
login (as the auth gate does), and counts rows per caller. Opens every store READ-ONLY;
nothing is written (no DELETE route is touched).

    .venv/bin/python /path/to/live_check_t77.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("REAL_HERMES_HOME", "/home/hermes/.hermes")).resolve()
os.environ["HERMES_HOME"] = str(HOME)          # the real home, as the gateway sees it
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from fastapi import FastAPI                            # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402


class Session:
    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


def client_for(login: str | None):
    from hermes_cli.web_routers.sessions import list_router, manage_router, search_router

    app = FastAPI()
    for router in (list_router, search_router, manage_router):
        app.include_router(router)
    client = TestClient(app)
    if login:
        @client.app.middleware("http")
        async def _stamp(request, call_next):
            request.state.session = Session(login)
            return await call_next(request)
    return client


def owners_of(db_path: Path, ids) -> dict:
    """user_id of each id in *db_path* (read-only); {'missing': [...]} for ids absent there."""
    import sqlite3

    if not Path(db_path).exists():
        return {"missing": sorted(ids)}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = {}
        for sid in ids:
            row = conn.execute("SELECT user_id FROM sessions WHERE id = ?", (sid,)).fetchone()
            out[sid] = None if row is None else row[0]
        return out
    finally:
        conn.close()


def report(login: str | None, query: str):
    with client_for(login) as cli:
        response = cli.get(query)
    body = response.json()
    rows = body.get("sessions") or []
    ids = [r["id"] for r in rows]
    print(f"\n=== login={login!r} {query}")
    print(f"    status={response.status_code} total={body.get('total')} rows={len(ids)} "
          f"profiles={sorted({r.get('profile') for r in rows})}")
    if ids:
        print(f"    ids={json.dumps(ids, ensure_ascii=False)[:400]}")
    return ids, body


def main() -> int:
    ok = True

    for login, profile, label in (
        ("maria.safronova", "01-maria-safronova", "Maria"),
        ("kurilenko", "01-kurilenko", "Kurilenko"),
    ):
        ids, _ = report(login, "/api/sessions?limit=50")
        prof_db = HOME / "profiles" / profile / "state.db"
        root_db = HOME / "state.db"
        in_profile = owners_of(prof_db, ids)
        in_root = owners_of(root_db, ids)
        profile_owned = {k: v for k, v in in_profile.items() if v}
        print(f"    {label}: in profiles/{profile}/state.db = {len(profile_owned)}/{len(ids)}; "
              f"owners={sorted(set(profile_owned.values()))}")
        print(f"    {label}: also present in ROOT state.db = "
              f"{sum(1 for v in in_root.values() if v)}/{len(ids)}")
        if len(profile_owned) != len(ids):
            print(f"    !! {label}: some rows are NOT in their profile store: "
                  f"{sorted(set(ids) - set(profile_owned))}")
            ok = False
        foreign = [k for k, v in profile_owned.items() if v != f"person/{login}"]
        if foreign:
            print(f"    !! {label}: rows owned by someone else: {foreign}")
            ok = False

    ids, body = report("maria.safronova", "/api/sessions?limit=50&profile=01-orchestrator")
    if ids or body.get("total"):
        print("    !! negative control failed: foreign profile served rows")
        ok = False
    else:
        print("    negative control OK: 0 sessions from 01-orchestrator")

    # A ticket of somebody else's terminal must stay 404 (no destructive call, GET only).
    with client_for("maria.safronova") as cli:
        for path in ("/api/sessions?profile=01-orchestrator&limit=5",):
            response = cli.get(path)
            print(f"    GET {path} -> {response.status_code}")

    ids, _ = report("maria.safronova", "/api/sessions?limit=50&profile=01-maria-safronova")
    print("\nRESULT:", "OK" if ok else "PROBLEMS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
