
"""Baseline proof: what these routes did BEFORE step 5 (run against the pre-step tree)."""
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
HOME = Path(tempfile.mkdtemp(prefix="mpd5-red-", dir="/tmp"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ.setdefault("HERMES_DASHBOARD_SESSION_TOKEN", "x")
ALICE_ROOT, BOB_ROOT = "01-alice", "01-bob"
SHARED = ("02-hermes-platform",)
(HOME / "config.yaml").write_text("acl:\n  enabled: true\n  multi_user: true\n", encoding="utf-8")
import hermes_acl_bindings as hb
p = HOME / "acl" / "profile-bindings.json"; p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({"schema": hb.SCHEMA, "written_at": "x", "written_by": "probe", "persons": [
  {"slug": "person/alice", "display_name": "A", "shared": False, "profiles": [ALICE_ROOT],
   "subjects": [], "owner": "person/alice", "source": "admin"},
  {"slug": "person/bob", "display_name": "B", "shared": False, "profiles": [BOB_ROOT],
   "subjects": [], "owner": "person/bob", "source": "admin"},
  {"slug": "shared/service", "display_name": "S", "shared": True, "profiles": list(SHARED),
   "subjects": [], "owner": "admin", "source": "admin"}]}), encoding="utf-8")
os.chmod(p, 0o600)
for name in (ALICE_ROOT, BOB_ROOT, *SHARED):
    d = HOME / "profiles" / name; d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("{}\n", encoding="utf-8")
    (d / "SOUL.md").write_text(f"# {name}\n", encoding="utf-8")
hb.reset_cache()
import hermes_acl_identity
from gateway.owner_identity import host_owner_subject
real = hermes_acl_identity.resolve_subject
def _resolve(value, fallback=""):
    login = str(value or "").strip()
    return real(value, fallback=fallback) if not login else f"person/{login}"
hermes_acl_identity.resolve_subject = _resolve
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_cli.web_routers.profiles import router, sessions_router
class S:
    def __init__(self, u): self.user_id, self.provider, self.issuer = u, "basic", ""
def client(login):
    app = FastAPI()
    @app.middleware("http")
    async def _s(request, call_next):
        request.state.session = S(login); return await call_next(request)
    app.include_router(sessions_router); app.include_router(router)
    return TestClient(app)
alice = client("alice")
names = [r["name"] for r in alice.get("/api/profiles").json()["profiles"]]
print("GET /api/profiles as alice ->", names)
print("LEAK 1: alice lists bob's root:", BOB_ROOT in names)
r = alice.get(f"/api/profiles/{BOB_ROOT}/soul")
print(f"GET /api/profiles/{BOB_ROOT}/soul as alice -> {r.status_code}", r.text[:60])
print("LEAK 2: alice reads bob's SOUL:", r.status_code == 200)
r = alice.put(f"/api/profiles/{BOB_ROOT}/soul", json={"content": "# hijacked\n"})
print(f"PUT /api/profiles/{BOB_ROOT}/soul as alice -> {r.status_code}")
print("LEAK 3: alice writes bob's SOUL:",
      (HOME / "profiles" / BOB_ROOT / "SOUL.md").read_text(encoding="utf-8") == "# hijacked\n")
r = alice.post("/api/profiles", json={"name": "07-in-bobs-namespace"})
print(f"POST /api/profiles {{name:07-in-bobs-namespace}} as alice -> {r.status_code}")
print("LEAK 4: alice creates a profile at will:", r.status_code == 200)
