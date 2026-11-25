#!/usr/bin/env python3
"""Live demo for L2-c part B: probe10 (exchange overwrite) before and after the fix.

Real files router (FastAPI TestClient), real bindings registry on disk, real profile homes,
real exchange directory. No mocks of the policy.

Run from the card worktree:
    .vpy/bin/python live_demo_exchange_authorship.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="l2c-partb-"))
HOME = WORK / ".hermes"
HOME.mkdir()
os.environ["HERMES_HOME"] = str(HOME)
os.environ.pop("HERMES_DASHBOARD_FILES_ROOT", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermes_acl_bindings as hb  # noqa: E402
import hermes_acl_identity  # noqa: E402
import hermes_acl_scope  # noqa: E402
import hermes_constants  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from gateway.owner_identity import host_owner_subject  # noqa: E402

hermes_constants.get_hermes_home = lambda: HOME

# Real profile homes for alice and bob (the registry's own layout).
profiles_root = HOME / "profiles"
(HOME / "config.yaml").write_text("{}\n", encoding="utf-8")
for name in ("01-alice", "01-bob"):
    (profiles_root / name).mkdir(parents=True)
    (profiles_root / name / "config.yaml").write_text("{}\n", encoding="utf-8")

registry = {
    "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
    "persons": [
        {"slug": "person/alice", "display_name": "Alice", "shared": False,
         "profiles": ["01-alice"], "subjects": [], "owner": "person/alice", "source": "admin"},
        {"slug": "person/bob", "display_name": "Bob", "shared": False,
         "profiles": ["01-bob"], "subjects": [], "owner": "person/bob", "source": "admin"},
    ],
}
path = HOME / "acl" / "profile-bindings.json"
path.parent.mkdir(parents=True)
path.write_text(json.dumps(registry), encoding="utf-8")
os.chmod(path, 0o600)
hb.reset_cache()

hermes_acl_scope.acl_enabled = lambda: True
hermes_acl_identity.multi_user_enabled = lambda: True
hermes_acl_identity.resolve_subject = lambda value, fallback="": (
    host_owner_subject() if str(value or "").strip() == "owner"
    else f"person/{str(value or '').strip()}" if str(value or "").strip() else fallback)

exchange = HOME / "shared"
exchange.mkdir()

from hermes_cli.web_routers import files as files_router  # noqa: E402


class _Session:
    def __init__(self, user_id):
        self.user_id, self.provider, self.issuer = user_id, "basic", ""


def client_for(login):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = _Session(login)
        return await call_next(request)

    app.include_router(files_router.router)
    return TestClient(app)


def upload(client, target: Path, text: str, overwrite=True):
    payload = "data:text/plain;base64," + base64.b64encode(text.encode()).decode()
    return client.post("/api/files/upload", json={
        "path": str(target), "data_url": payload, "overwrite": overwrite})


print("=" * 78)
print("PROBE10: bob prepares shared/team.docx, alice uploads the same path (overwrite=true)")
print("=" * 78)
team = exchange / "team.docx"
team.write_text("bob's prepared deliverable", encoding="utf-8")

resp = upload(client_for("alice"), team, "ALICE CLOBBERED IT")
print(f"alice upload -> HTTP {resp.status_code}")
print(f"  shared/team.docx content   : {team.read_text(encoding='utf-8')!r}")
print(f"  response path              : {resp.json().get('path')}")
alice_file = exchange / "person_alice" / "team.docx"
print(f"  shared/person_alice/team.docx exists: {alice_file.is_file()}"
      + (f" content={alice_file.read_text(encoding='utf-8')!r}" if alice_file.is_file() else ""))
bob_text = "bob's prepared deliverable"
print(f"  verdict: bob's file INTACT = {team.read_text(encoding='utf-8') == bob_text}")

print()
print("=" * 78)
print("alice rewrites her OWN file (overwrite=true) — must succeed")
print("=" * 78)
resp = upload(client_for("alice"), alice_file, "alice v2")
print(f"alice upload alice_file v2 -> HTTP {resp.status_code}, content={alice_file.read_text(encoding='utf-8')!r}")

print()
print("=" * 78)
print("alice writes into bob's folder — must be refused")
print("=" * 78)
resp = upload(client_for("alice"), exchange / "person_bob" / "planted.txt", "planted")
print(f"alice upload bob folder -> HTTP {resp.status_code}: {resp.json().get('detail')}")
print(f"  file created: {(exchange / 'person_bob' / 'planted.txt').exists()}")

print()
print("=" * 78)
print("authorship journal backstop: a migrated file bob authored, inside alice's folder")
print("=" * 78)
planted = exchange / "person_alice" / "migrated.docx"
bob_migrated_text = "bob's migrated deliverable"
planted.write_text(bob_migrated_text, encoding="utf-8")
import hermes_acl_results as results_acl  # noqa: E402

bob_request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
bob_request.state.session = _Session("bob")
results_acl.record_exchange_authorship(bob_request, planted)
print(f"  journal author of {planted.name}: {results_acl.exchange_author(planted)!r}")
resp = upload(client_for("alice"), planted, "clobbered")
print(f"  alice upload -> HTTP {resp.status_code}: {resp.json().get('detail')}")
print(f"  content intact = {planted.read_text(encoding='utf-8') == bob_migrated_text}")

print()
print("=" * 78)
print("delete: alice tries to delete bob's folder file — must be refused")
print("=" * 78)
bobs = exchange / "person_bob" / "kept.txt"
bobs.parent.mkdir(parents=True, exist_ok=True)
bobs.write_text("bob keeps this", encoding="utf-8")
resp = client_for("alice").request("DELETE", "/api/files", json={"path": str(bobs)})
print(f"  DELETE -> HTTP {resp.status_code}: {resp.json().get('detail')}")
print(f"  file survives = {bobs.is_file()}")

print()
print("=" * 78)
print("NO-OP: multi_user=false — the exchange root takes the write, as before")
print("=" * 78)
hermes_acl_identity.multi_user_enabled = lambda: False
hermes_acl_scope.acl_enabled = lambda: False
root_file = exchange / "legacy.txt"
resp = upload(client_for("alice"), root_file, "pre-change behaviour")
print(f"  alice upload to exchange root -> HTTP {resp.status_code}, content={root_file.read_text(encoding='utf-8')!r}")
print(f"  shared/person_alice created by this write: {(exchange / 'person_alice' / 'legacy.txt').exists()}")

print()
print("=" * 78)
print("NO-OP: host owner — same, even with multi_user ON")
print("=" * 78)
hermes_acl_identity.multi_user_enabled = lambda: True
hermes_acl_scope.acl_enabled = lambda: True
owner_file = exchange / "owner.txt"
resp = upload(client_for("owner"), owner_file, "owner's file")
print(f"  owner upload to exchange root -> HTTP {resp.status_code}, content={owner_file.read_text(encoding='utf-8')!r}")
print(f"  journal row for it: {results_acl.exchange_author(owner_file)!r} (empty = not journalled)")

print()
print(f"journal: {exchange / '.authorship.jsonl'}")
for line in (exchange / ".authorship.jsonl").read_text(encoding="utf-8").splitlines():
    print("  " + line)

shutil.rmtree(WORK, ignore_errors=True)
