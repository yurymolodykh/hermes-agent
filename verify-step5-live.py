#!/usr/bin/env python3
"""Live probe for МПД step 5 — profiles: own and shared.

Runs the real dashboard routes against a disposable HERMES_HOME with ``acl.multi_user: true``
and a real bindings registry, then prints the HTTP codes and bodies the acceptance criteria ask
for. Nothing here touches the operator's real home, registry or profiles.

Criteria exercised:

2. ``GET /api/profiles`` as subject A → no ``01-B`` in the answer.
3. ``POST /api/profiles`` with a foreign name → HTTP refusal, not 200 (code + body printed).
4. Shared profiles 02..15 are visible to BOTH subjects.
5. The host owner still sees every profile.

Plus the fan-out/route seams this step closes:
* ``GET /api/profiles/{01-B}/soul`` as A → 404, byte-identical to an absent name;
* ``GET /api/profiles/sessions?profile=01-B`` as A → 404;
* ``PUT /api/profiles/{01-B}/soul`` as A → 404 and the file is unchanged;
* ``PUT /api/profiles/{02-shared}/soul`` as A → 403 (visible, not writable);
* ``POST /api/profiles/{01-A}/export`` as A → 403 (owner-only).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# /tmp, NOT the agent scratch: a home under ``~/.hermes`` resolves its ROOT to ``~/.hermes``
# (``_hermes_root_of``), so the probe would read the operator's real profile roster.
HOME = Path(tempfile.mkdtemp(prefix="mpd5-live-", dir="/tmp"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ.setdefault("HERMES_DASHBOARD_SESSION_TOKEN", "mpd5-probe-token")

ALICE, BOB, OWNER_LOGIN = "alice", "bob", "owner"
ALICE_ROOT, BOB_ROOT = "01-alice", "01-bob"
SHARED = ("02-hermes-platform", "03-software-engineer")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))


def seed() -> None:
    """A HERMES_HOME with a real registry and five profile directories."""
    config = HOME / "config.yaml"
    config.write_text("acl:\n  enabled: true\n  multi_user: true\n  identity_trust: untrusted\n",
                      encoding="utf-8")
    (HOME / "profiles").mkdir(parents=True, exist_ok=True)

    import hermes_acl_bindings as hb

    path = HOME / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "probe",
        "persons": [
            {"slug": "person/alice", "display_name": "Alice", "shared": False,
             "profiles": [ALICE_ROOT], "subjects": [], "owner": "person/alice", "source": "admin"},
            {"slug": "person/bob", "display_name": "Bob", "shared": False,
             "profiles": [BOB_ROOT], "subjects": [], "owner": "person/bob", "source": "admin"},
            {"slug": "shared/service", "display_name": "Shared", "shared": True,
             "profiles": list(SHARED), "subjects": [], "owner": "admin", "source": "admin"},
        ],
    }), encoding="utf-8")
    os.chmod(path, 0o600)

    for name in (ALICE_ROOT, BOB_ROOT, *SHARED):
        d = HOME / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(f"profile:\n  display_name: {name}\n", encoding="utf-8")
        (d / "SOUL.md").write_text(f"# {name}\n", encoding="utf-8")
    hb.reset_cache()


def _patch_identity_rule():
    """The step-3 rule: ``role: owner`` keeps the owner subject, everyone else person/<login>."""
    import hermes_acl_scope
    import hermes_acl_identity
    import hermes_acl_profiles as prof_acl
    from gateway.owner_identity import host_owner_subject

    real = hermes_acl_identity.resolve_subject

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        if not login:
            return real(value, fallback=fallback)
        return host_owner_subject() if login == OWNER_LOGIN else f"person/{login}"

    hermes_acl_identity.resolve_subject = _resolve
    # multi_user / acl_enabled read config: the seeded config.yaml already says true.
    assert hermes_acl_scope.acl_enabled(), "probe config did not switch acl on"
    assert prof_acl.multi_user_enabled(), "probe config did not switch multi_user on"


class _Session:
    def __init__(self, user_id, provider="basic", issuer=""):
        self.user_id, self.provider, self.issuer = user_id, provider, issuer


def client_as(login: str):
    """A fresh app whose every request carries *login* as the verified dashboard session."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.profiles import router, sessions_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = _Session(login, "basic", "")
        return await call_next(request)

    app.include_router(sessions_router)
    app.include_router(router)
    return TestClient(app)


def main() -> int:
    print(f"probe home: {HOME}")
    seed()
    _patch_identity_rule()

    # --- criterion 2: listing is scoped -----------------------------------
    alice = client_as(ALICE)
    body = alice.get("/api/profiles").json()
    names = [row["name"] for row in body["profiles"]]
    print(f"GET /api/profiles as alice -> {names}")
    check("2a. subject A does not see 01-B", BOB_ROOT not in names, f"answer={names}")
    check("2b. subject A sees its own root", ALICE_ROOT in names)

    # --- criterion 4: shared visible to both -------------------------------
    bob_names = [row["name"] for row in client_as(BOB).get("/api/profiles").json()["profiles"]]
    print(f"GET /api/profiles as bob -> {bob_names}")
    check("4a. shared profiles visible to A", set(SHARED) <= set(names))
    check("4b. shared profiles visible to B", set(SHARED) <= set(bob_names))
    check("4c. A and B see different personal roots",
          ALICE_ROOT in names and BOB_ROOT not in names
          and BOB_ROOT in bob_names and ALICE_ROOT not in bob_names)

    # --- criterion 5: owner keeps the roster -------------------------------
    owner_names = [row["name"] for row in client_as(OWNER_LOGIN).get("/api/profiles").json()["profiles"]]
    print(f"GET /api/profiles as owner -> {owner_names}")
    check("5. owner sees every profile",
          set(owner_names) == {"default", ALICE_ROOT, BOB_ROOT, *SHARED}, f"answer={owner_names}")

    # --- criterion 3: create outside the set refused -----------------------
    r = alice.post("/api/profiles", json={"name": BOB_ROOT})
    print(f"POST /api/profiles {{name:{BOB_ROOT}}} as alice -> {r.status_code} {r.text}")
    check("3a. creating a foreign root is refused", r.status_code == 403)
    r2 = alice.post("/api/profiles", json={"name": ALICE_ROOT})
    print(f"POST /api/profiles {{name:{ALICE_ROOT}}} as alice -> {r2.status_code} {r2.text}")
    check("3b. default policy is admin-only (own root refused too)",
          r2.status_code == 403, "acl.profile_self_service is off by default")
    r3 = client_as(OWNER_LOGIN).post("/api/profiles", json={"name": "07-owner-made"})
    print(f"POST /api/profiles {{name:07-owner-made}} as owner -> {r3.status_code}")
    check("3c. the owner may still create", r3.status_code == 200)

    # --- the route seams ---------------------------------------------------
    bob_soul = HOME / "profiles" / BOB_ROOT / "SOUL.md"
    r = alice.get(f"/api/profiles/{BOB_ROOT}/soul")
    r_absent = alice.get("/api/profiles/99-nobody/soul")
    print(f"GET /api/profiles/{BOB_ROOT}/soul as alice -> {r.status_code} {r.text}")
    check("s1. a foreign SOUL is 404", r.status_code == 404)
    # Only the profile NAME differs in the body (the refusal quotes what was asked for); the
    # status and the shape are the same, which is what stops the answer being an oracle.
    check("s2. and shaped exactly like an absent name",
          r.status_code == r_absent.status_code == 404
          and set(r.json()) == set(r_absent.json()) == {"detail"}
          and r.json()["detail"].replace(BOB_ROOT, "<name>")
          == r_absent.json()["detail"].replace("99-nobody", "<name>"),
          f"absent -> {r_absent.status_code} {r_absent.text}")
    check("s3. the foreign content never leaks", "01-bob" not in r.text.replace(BOB_ROOT, ""))

    r = alice.put(f"/api/profiles/{BOB_ROOT}/soul", json={"content": "# hijacked\n"})
    print(f"PUT /api/profiles/{BOB_ROOT}/soul as alice -> {r.status_code} {r.text}")
    check("s4. writing a foreign SOUL is 404", r.status_code == 404)
    check("s5. and the file is untouched", bob_soul.read_text(encoding="utf-8") == "# 01-bob\n")

    r = alice.put(f"/api/profiles/{SHARED[0]}/soul", json={"content": "# mine now\n"})
    print(f"PUT /api/profiles/{SHARED[0]}/soul as alice -> {r.status_code} {r.text}")
    check("s6. a shared profile is visible but read-only", r.status_code == 403)

    r = alice.put(f"/api/profiles/{ALICE_ROOT}/soul", json={"content": "# alice edited\n"})
    print(f"PUT /api/profiles/{ALICE_ROOT}/soul as alice -> {r.status_code}")
    check("s7. the person writes their own profile", r.status_code == 200)

    r = alice.get("/api/profiles/sessions", params={"profile": BOB_ROOT})
    print(f"GET /api/profiles/sessions?profile={BOB_ROOT} as alice -> {r.status_code} {r.text}")
    check("s8. a foreign ?profile= fan-out is 404", r.status_code == 404)
    r = alice.get("/api/profiles/sessions", params={"profile": ALICE_ROOT})
    check("s9. the own ?profile= fan-out is 200", r.status_code == 200, f"{r.status_code}")

    r = alice.get("/api/profiles/sessions/sidebar", params={"recents_profile": BOB_ROOT})
    print(f"GET /api/profiles/sessions/sidebar?recents_profile={BOB_ROOT} as alice -> {r.status_code}")
    check("s10. a foreign sidebar scope is 404", r.status_code == 404)

    r = alice.post(f"/api/profiles/{ALICE_ROOT}/export", json={"output": str(HOME / "x.tar.gz")})
    print(f"POST /api/profiles/{ALICE_ROOT}/export as alice -> {r.status_code} {r.text}")
    check("s11. export stays owner-only", r.status_code == 403)

    r = alice.delete(f"/api/profiles/{ALICE_ROOT}")
    print(f"DELETE /api/profiles/{ALICE_ROOT} as alice -> {r.status_code} {r.text}")
    check("s12. delete stays owner-only", r.status_code == 403)
    check("s13. and the directory survives", (HOME / "profiles" / ALICE_ROOT).is_dir())

    # --- legacy mode: acl off changes nothing ------------------------------
    (HOME / "config.yaml").write_text("acl:\n  enabled: false\n  multi_user: false\n", encoding="utf-8")
    import importlib

    import hermes_acl_scope
    import hermes_acl_identity
    importlib.reload(hermes_acl_scope)
    importlib.reload(hermes_acl_identity)
    legacy_names = [row["name"] for row in client_as(ALICE).get("/api/profiles").json()["profiles"]]
    print(f"acl off — GET /api/profiles as alice -> {legacy_names}")
    check("l1. acl off lists the whole roster",
          set(legacy_names) == {"default", ALICE_ROOT, BOB_ROOT, *SHARED, "07-owner-made"},
          f"answer={legacy_names}")
    r = client_as(ALICE).get(f"/api/profiles/{BOB_ROOT}/soul")
    check("l2. acl off keeps foreign reads", r.status_code == 200, f"{r.status_code}")

    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"ИТОГ: {'OK' if not failed else 'FAIL'} — {len(results) - len(failed)}/{len(results)} checks")
    if failed:
        for name in failed:
            print(f"  failed: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
