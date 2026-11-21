"""Live probe for МПД step 6 — "only your own results" (files, skills, memory, card attachments).

Runs the REAL dashboard routers over HTTP (``TestClient``) in a throwaway ``HERMES_HOME`` built
from scratch: a bindings registry with two people, one profile home per person, one skill per
person, one memory bucket per person, and a board with one card and one attachment per person.

Prints one line per probe — ручка → код → вердикт — and counts. Exit code 0 only when every
probe has the expected answer, so the SAME script is the baseline-red proof: on a tree without
the step-6 boundary it reports the leaks instead of the refusals.

Usage:  HERMES_HOME=$(mktemp -d) python verify-step6-live.py
(the throwaway home is set up by the script itself; nothing under the real ``~/.hermes`` is read)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

# ``dir="/tmp"`` on purpose: this process's TMPDIR points INSIDE the operator's ``~/.hermes``
# (the profile scratch dir), and a HERMES_HOME nested under the native home resolves to the
# native home — the probe would then read the operator's real registry.
HOME = Path(tempfile.mkdtemp(prefix="mpd6probe.", dir="/tmp")) / ".hermes"
HOME.mkdir(parents=True)
# A marker file makes this a REAL Hermes home, so ``get_default_hermes_root`` honours
# HERMES_HOME instead of falling back to the operator's ``~/.hermes``. Written BEFORE any
# hermes import, because the root is memoised on first use.
(HOME / "config.yaml").write_text("{}\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(HOME)
# The probe builds its OWN board, so it must not inherit this worker's card fence
# (``HERMES_KANBAN_TASK``/``HERMES_DELEGATED_CHILD_CONTEXT`` would forbid every write).
for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_ATTACHMENTS_ROOT",
            "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DASHBOARD_FILES_ROOT",
            "HERMES_KANBAN_BOARD", "HERMES_PROFILE", "HERMES_KANBAN_TASK",
            "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_WORKSPACES_DIR"):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_acl_bindings as hb
import hermes_acl_identity
import hermes_acl_scope

ALICE, BOB, OWNER = "person/alice", "person/bob", "owner"
ALICE_ROOT, BOB_ROOT = "01-alice", "01-bob"
SHARED = ("02-hermes-platform", "03-software-engineer")


# ── identity: multi-user ON, a password login becomes person/<login> ─────────
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

# ── the registry: alice → 01-alice, bob → 01-bob, 02/03 shared ───────────────
registry_path = HOME / "acl" / "profile-bindings.json"
registry_path.parent.mkdir(parents=True)
registry_path.write_text(json.dumps({
    "schema": hb.SCHEMA, "written_at": "2026-11-20T00:00:00+00:00", "written_by": "admin",
    "persons": [
        {"slug": ALICE, "display_name": "Alice", "shared": False, "profiles": [ALICE_ROOT],
         "subjects": [], "owner": ALICE, "source": "admin"},
        {"slug": BOB, "display_name": "Bob", "shared": False, "profiles": [BOB_ROOT],
         "subjects": [], "owner": BOB, "source": "admin"},
        {"slug": "shared/service", "display_name": "Shared", "shared": True,
         "profiles": list(SHARED), "subjects": [], "owner": "admin", "source": "admin"},
    ],
}), encoding="utf-8")
os.chmod(registry_path, 0o600)
hb.reset_cache()

# ── profile homes, one skill and one memory bucket each ─────────────────────
from hermes_cli import profiles as profiles_mod
from hermes_constants import get_hermes_home

# HERMES_HOME is pinned above, before any Hermes import, so this module-level read resolves the
# throwaway home on purpose.
default_home = get_hermes_home()  # health: allow HX005 -- a probe, not shipped code
profiles_root = default_home / "profiles"
homes = {"default": default_home}
(default_home / "config.yaml").write_text("{}\n")
for name in (ALICE_ROOT, BOB_ROOT, *SHARED):
    home = profiles_root / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n")
    homes[name] = home
profiles_mod._get_default_hermes_home = lambda: default_home
profiles_mod._get_profiles_root = lambda: profiles_root

SECRET = {ALICE_ROOT: "alice only", BOB_ROOT: "bob only"}
for name, text in SECRET.items():
    (homes[name] / "secret.txt").write_text(text, encoding="utf-8")
    skills = homes[name] / "skills" / f"{name}-skill"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: {name}\n---\n\n# {name}\n", encoding="utf-8")
    bucket = homes[name] / "memories" / "u" / f"person_{name.split('-')[1]}"
    bucket.mkdir(parents=True, exist_ok=True)
    (bucket / "MEMORY.md").write_text(f"{name} note", encoding="utf-8")
(homes[SHARED[0]] / "skills" / "shared-skill").mkdir(parents=True, exist_ok=True)
(homes[SHARED[0]] / "skills" / "shared-skill" / "SKILL.md").write_text(
    "---\nname: shared-skill\ndescription: shared\n---\n\n# shared\n", encoding="utf-8")

# ── the board: one card + one attachment per person ────────────────────────
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

kb.init_db()
conn = kbc.connect(db_path=kb.kanban_db_path())
cards = {}
try:
    for person in (ALICE, BOB):
        task_id = kb.create_task(conn, title=f"{person}'s card", created_by=person, assignee=person)
        dest = kb.task_attachments_dir(task_id)
        dest.mkdir(parents=True, exist_ok=True)
        blob = dest / "note.txt"
        blob.write_text(f"{person}'s private notes", encoding="utf-8")
        att_id = kb.add_attachment(
            conn, task_id, filename=blob.name, stored_path=str(blob.resolve()),
            content_type="text/plain", size=blob.stat().st_size,
            uploaded_by=person)
        cards[person] = {"task": task_id, "attachment": att_id, "path": blob}
finally:
    conn.close()


# ── the app: real routers, a login stamped by a middleware (the auth gate) ──
class Session:
    def __init__(self, user_id):
        self.user_id, self.provider, self.issuer = user_id, "basic", ""


LOGIN = {"value": "alice"}


def _load_plugin_router():
    spec = importlib.util.spec_from_file_location(
        "kanban_probe_plugin", ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


def build_app():
    from hermes_cli.web_routers import files as files_router
    from hermes_cli.web_routers import ops as ops_router
    from hermes_cli.web_routers import skills as skills_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = Session(LOGIN["value"])
        return await call_next(request)

    for module in (files_router, skills_router, ops_router):
        for name in ("hub_router", "router"):
            sub = getattr(module, name, None)
            if sub is not None:
                app.include_router(sub)
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


CLIENT = build_app()


def as_person(login):
    LOGIN["value"] = login


RESULTS = []


def probe(label, response, expect, forbid=None):
    body = response.text[:200].replace("\n", " ")
    ok = response.status_code == expect
    if ok and forbid:
        ok = forbid not in response.text
    RESULTS.append(ok)
    print(f"  [{'OK ' if ok else 'LEAK'}] {label}: {response.status_code} "
          f"(ждали {expect}) {body}")


print("== 1. файлы: свой и чужой ==")
as_person("alice")
own = homes[ALICE_ROOT] / "secret.txt"
foreign = homes[BOB_ROOT] / "secret.txt"
probe("GET /api/fs/read-text свой файл", CLIENT.get("/api/fs/read-text", params={"path": str(own)}),
      200, forbid="bob only")
probe("GET /api/fs/read-text чужой файл", CLIENT.get("/api/fs/read-text", params={"path": str(foreign)}),
      403, forbid="bob only")
probe("GET /api/fs/list чужой каталог",
      CLIENT.get("/api/fs/list", params={"path": str(homes[BOB_ROOT])}), 403)
probe("GET /api/fs/read-text обход ..",
      CLIENT.get("/api/fs/read-text",
                 params={"path": f"{homes[ALICE_ROOT]}/../01-bob/secret.txt"}), 403,
      forbid="bob only")
probe("GET /api/files/read чужой файл",
      CLIENT.get("/api/files/read", params={"path": str(foreign)}), 403, forbid="bob only")
probe("GET /api/files свой каталог",
      CLIENT.get("/api/files", params={"path": str(homes[ALICE_ROOT])}), 200)
probe("POST /api/fs/write-text запись чужого",
      CLIENT.post("/api/fs/write-text",
                  json={"path": str(homes[BOB_ROOT] / "planted.txt"), "content": "pwned"}), 403)
print(f"    planted.txt существует? {(homes[BOB_ROOT] / 'planted.txt').exists()} (ожидали False)")

print("== 2. скиллы ==")
probe("GET /api/skills (свой набор)", CLIENT.get("/api/skills"), 200)
names = [s["name"] for s in CLIENT.get("/api/skills").json()]
print(f"    навыки alice: {names}")
mine, theirs = f"{ALICE_ROOT}-skill", f"{BOB_ROOT}-skill"
RESULTS.append(mine in names and theirs not in names)
print(f"  [{'OK ' if RESULTS[-1] else 'LEAK'}] свой {mine} виден, чужой {theirs} — "
      f"{theirs in names} (ждали False)")
probe("GET /api/skills?profile=01-bob (чужой профиль)",
      CLIENT.get("/api/skills", params={"profile": BOB_ROOT}), 404)
probe("GET /api/skills/content чужой навык",
      CLIENT.get("/api/skills/content", params={"name": f"{BOB_ROOT}-skill"}), 404)
probe("GET /api/skills?profile=02-hermes-platform (общий)",
      CLIENT.get("/api/skills", params={"profile": SHARED[0]}), 200)

print("== 3. память ==")
status = CLIENT.get("/api/memory")
probe("GET /api/memory", status, 200)
counted = status.json().get("builtin_files", {}).get("memory")
expected = len(f"{ALICE_ROOT} note")
RESULTS.append(counted == expected)
print(f"  [{'OK ' if counted == expected else 'LEAK'}] считает только свой бакет: "
      f"{counted} == {expected}")
probe("GET /api/memory?profile=01-bob",
      CLIENT.get("/api/memory", params={"profile": BOB_ROOT}), 404)
reset = CLIENT.post("/api/memory/reset", json={"target": "all"})
probe("POST /api/memory/reset", reset, 200)
alice_bucket = homes[ALICE_ROOT] / "memories" / "u" / "person_alice" / "MEMORY.md"
bob_bucket = homes[BOB_ROOT] / "memories" / "u" / "person_bob" / "MEMORY.md"
kept_bob = bob_bucket.is_file()
RESULTS.append(not alice_bucket.exists() and kept_bob)
print(f"  [{'OK ' if RESULTS[-1] else 'LEAK'}] сброс только своего: alice={alice_bucket.exists()}, "
      f"bob={kept_bob} (ждали False/True)")

print("== 4. вложения карточек ==")
probe("GET вложение своей карточки",
      CLIENT.get(f"/api/plugins/kanban/attachments/{cards[ALICE]['attachment']}"), 200)
probe("GET вложение чужой карточки по id",
      CLIENT.get(f"/api/plugins/kanban/attachments/{cards[BOB]['attachment']}"), 404,
      forbid="bob")
probe("GET список вложений чужой карточки",
      CLIENT.get(f"/api/plugins/kanban/tasks/{cards[BOB]['task']}/attachments"), 404)
deleted = CLIENT.delete(f"/api/plugins/kanban/attachments/{cards[BOB]['attachment']}")
probe("DELETE вложение чужой карточки", deleted, 404)
RESULTS.append(cards[BOB]["path"].is_file())
print(f"  [{'OK ' if RESULTS[-1] else 'LEAK'}] чужой файл уцелел: {cards[BOB]['path'].is_file()}")

print("== 5. владелец хоста (положительный контроль) ==")
as_person(OWNER)
probe("owner: чужой файл", CLIENT.get("/api/fs/read-text", params={"path": str(foreign)}), 200)
probe("owner: /api/skills?profile=01-bob",
      CLIENT.get("/api/skills", params={"profile": BOB_ROOT}), 200)
probe("owner: вложение чужой карточки",
      CLIENT.get(f"/api/plugins/kanban/attachments/{cards[BOB]['attachment']}"), 200)

passed = sum(1 for r in RESULTS if r)
print(f"\nИТОГ: {passed}/{len(RESULTS)} проверок")
sys.exit(0 if passed == len(RESULTS) else 1)
