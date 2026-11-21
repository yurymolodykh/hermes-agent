"""Count the OWNER's reachable files on the SAME fixture, before and after the step-6 boundary.

Criterion 4 asks for "own files still read exactly as before — compare the NUMBER of reachable
files, not just one". This builds the same throwaway home the live probe uses (one profile home per
person, one skill, one memory bucket, one card each) and counts, as the HOST OWNER and as alice:

* entries the ``/api/files`` listing returns for the registry root,
* every path the listing can descend into (a recursive walk through the same route).

Run it on the branch and on the base tree; the OWNER columns must be identical.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="mpd6count.", dir="/tmp")) / ".hermes"
HOME.mkdir(parents=True)
(HOME / "config.yaml").write_text("{}\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(HOME)
for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_ATTACHMENTS_ROOT",
            "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DASHBOARD_FILES_ROOT",
            "HERMES_KANBAN_BOARD", "HERMES_PROFILE", "HERMES_KANBAN_TASK",
            "HERMES_DELEGATED_CHILD_CONTEXT"):
    os.environ.pop(key, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_scope

ALICE, BOB, OWNER = "person/alice", "person/bob", "owner"
ALICE_ROOT, BOB_ROOT = "01-alice", "01-bob"

hermes_acl_scope.acl_enabled = lambda: True
hermes_acl_scope.identity_trust = lambda: "untrusted"
hermes_acl_identity.multi_user_enabled = lambda: True


def _resolve(value, fallback=""):
    login = str(value or "").strip()
    if not login:
        return fallback
    if login == OWNER:
        from gateway.owner_identity import host_owner_subject

        return host_owner_subject()
    return f"person/{login}"


hermes_acl_identity.resolve_subject = _resolve

registry_path = HOME / "acl" / "profile-bindings.json"
registry_path.parent.mkdir(parents=True)
registry_path.write_text(json.dumps({
    "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
    "persons": [
        {"slug": ALICE, "display_name": "Alice", "shared": False, "profiles": [ALICE_ROOT],
         "subjects": [], "owner": ALICE, "source": "admin"},
        {"slug": BOB, "display_name": "Bob", "shared": False, "profiles": [BOB_ROOT],
         "subjects": [], "owner": BOB, "source": "admin"},
    ],
}), encoding="utf-8")
os.chmod(registry_path, 0o600)
hb.reset_cache()

from hermes_cli import profiles as profiles_mod
from hermes_constants import get_hermes_home

# HERMES_HOME is pinned above, before any Hermes import, so this module-level read resolves the
# throwaway home on purpose.
default_home = get_hermes_home()  # health: allow HX005 -- a probe, not shipped code
profiles_root = default_home / "profiles"
(default_home / "config.yaml").write_text("{}\n", encoding="utf-8")
homes = {"default": default_home}
for name in (ALICE_ROOT, BOB_ROOT):
    home = profiles_root / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    homes[name] = home
profiles_mod._get_default_hermes_home = lambda: default_home
profiles_mod._get_profiles_root = lambda: profiles_root

# Ten files per person, so the count is not trivially 1.
for name in (ALICE_ROOT, BOB_ROOT):
    for index in range(10):
        (homes[name] / f"file{index}.txt").write_text(f"{name} {index}", encoding="utf-8")


class Session:
    def __init__(self, user_id):
        self.user_id, self.provider, self.issuer = user_id, "basic", ""


LOGIN = {"value": "alice"}


def build_app():
    from hermes_cli.web_routers import files as files_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = Session(LOGIN["value"])
        return await call_next(request)

    app.include_router(files_router.router)
    return TestClient(app)


CLIENT = build_app()


def walk(root: Path, login: str) -> int:
    """Count every file reachable from *root* through ``GET /api/files``."""
    LOGIN["value"] = login
    total = 0
    stack = [root]
    seen = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        response = CLIENT.get("/api/files", params={"path": str(current)})
        if response.status_code != 200:
            continue
        for entry in response.json()["entries"]:
            path = Path(entry["path"])
            if entry.get("is_directory") or entry.get("isDirectory"):
                stack.append(path)
            else:
                total += 1
    return total


label = sys.argv[1] if len(sys.argv) > 1 else "tree"
owner_all = walk(profiles_root, OWNER)
owner_alice = walk(homes[ALICE_ROOT], OWNER)
alice_own = walk(homes[ALICE_ROOT], "alice")
alice_foreign = walk(homes[BOB_ROOT], "alice")
alice_whole = walk(profiles_root, "alice")
print(f"{label}: owner/profiles_root={owner_all} owner/01-alice={owner_alice} "
      f"alice/01-alice={alice_own} alice/01-bob={alice_foreign} alice/profiles_root={alice_whole}")
