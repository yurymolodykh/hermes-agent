"""Live read-only check: shared-profile sessions against the REAL Hermes home.

Card t_bd1ca283. Mounts the card's routers in a TestClient, stamps a verified dashboard login
(as the auth gate does), and counts rows per caller/profile. Every store is opened READ-ONLY;
nothing is written (no mutation route is touched).

    .venv/bin/python tests/hermes_cli/tools/live_check_tbd1ca283.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("REAL_HERMES_HOME", "/home/hermes/.hermes")).resolve()
os.environ["HERMES_HOME"] = str(HOME)          # the real home, as the gateway sees it
# repo root = three levels up from tests/hermes_cli/tools/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from fastapi import FastAPI
from fastapi.testclient import TestClient


class Session:
    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


def client_for(login: str | None):
    from hermes_cli.web_routers.profiles import sessions_router
    from hermes_cli.web_routers.sessions import list_router, manage_router, search_router

    app = FastAPI()
    for router in (list_router, search_router, manage_router, sessions_router):
        app.include_router(router)
    client = TestClient(app)
    if login:
        @client.app.middleware("http")
        async def _stamp(request, call_next):
            request.state.session = Session(login)
            return await call_next(request)
    return client


def report(login, query):
    with client_for(login) as cli:
        response = cli.get(query)
    try:
        body = response.json()
    except ValueError:
        body = {}
    rows = body.get("sessions") or []
    ids = [r["id"] for r in rows]
    print(f"\n=== login={login!r} {query}")
    print(f"    status={response.status_code} total={body.get('total')} rows={len(ids)} "
          f"profiles={sorted({r.get('profile') for r in rows})}")
    if ids:
        print(f"    ids={json.dumps(ids, ensure_ascii=False)[:400]}")
    return ids, body, response.status_code


def main() -> int:
    ok = True

    def expect(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"    [{'OK' if good else 'FAIL'}] {label}: got {got!r}, want {want!r}")

    # 1. A verified employee lists a shared profile's store: rows must come back.
    print("\n--- shared profiles are readable by a verified person")
    for shared in ("03-software-engineer", "06-contest-methodologist", "02-hermes-platform"):
        ids, body, status = report("maria.safronova", f"/api/sessions?limit=50&profile={shared}")
        expect(f"{shared} status", status, 200)
        expect(f"{shared} keeps its own profile", sorted({r.get("profile") for r in body.get("sessions") or []}),
               [shared])
        if not ids:
            ok = False
            print(f"    [FAIL] {shared}: no rows for a verified person")
        else:
            _detail, _, dstatus = report("maria.safronova",
                                        f"/api/sessions/{ids[0]}?profile={shared}")
            expect(f"{shared} row opens by id", dstatus, 200)

    # 2. A foreign PERSONAL profile stays closed, and its id opens nothing.
    print("\n--- personal profiles stay isolated")
    for personal in ("01-kurilenko", "01-orchestrator"):
        ids, _, status = report("maria.safronova", f"/api/sessions?limit=50&profile={personal}")
        expect(f"{personal} rows", ids, [])

    # 2b. The per-profile fan-out route the dashboard sidebar uses must agree with the list.
    print("\n--- /api/profiles/sessions fan-out")
    for shared in ("03-software-engineer", "06-contest-methodologist"):
        _ids, body, status = report("maria.safronova", f"/api/profiles/sessions?profile={shared}&limit=50")
        expect(f"fan-out {shared} status", status, 200)
        rows = body.get("sessions") or []
        if rows:
            expect(f"fan-out {shared} rows stay in profile",
                   sorted({row.get("profile") for row in rows}), [shared])
        else:
            ok = False
            print(f"    [FAIL] fan-out {shared}: no rows for a verified person")

    # 3. Identity not established: shared rows are NOT published anonymously.
    print("\n--- no identity => nothing")
    ids, _, status = report(None, "/api/sessions?limit=50&profile=03-software-engineer")
    expect("unverified shared rows", ids, [])

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
