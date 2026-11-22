#!/usr/bin/env python3
"""Живой прогон приёмки для карточки t_819d3353 на настоящем HTTP-сервере.

Поднимает uvicorn на свободном порту loopback с РЕАЛЬНЫМ роутером профилей
(`hermes_cli.web_routers.profiles`), в изолированном HERMES_HOME, и делает настоящие
запросы. Сессия дашборда подставляется middleware (как в тестах роутера), чтобы можно было
показать и владельца, и чужого человека: auth-гейт демона подменял бы субъекта.

Печатает доказательства по пунктам приёмки (а)–(д).
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # evidence/ -> the checkout being probed
sys.path.insert(0, str(ROOT))

HOME = Path(tempfile.mkdtemp(prefix="soulprobe-"))
# A Hermes home is only accepted when it carries the marker files; without them
# ``get_default_hermes_root`` falls back to the machine's real home and a probe would
# create profiles in production.
(HOME / "config.yaml").write_text("{}\n", encoding="utf-8")
(HOME / ".env").write_text("", encoding="utf-8")
(HOME / "state.db").write_text("", encoding="utf-8")
os.environ["HERMES_HOME"] = str(HOME)
# ``_get_profiles_root`` is deliberately HOME-anchored, so a probe must move HOME too —
# otherwise a "temporary" home still creates profiles under the operator's real root.
os.environ["HOME"] = str(HOME / "user")
Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)

ALICE, BOB = "person/alice", "person/bob"
# The dashboard session carries the LOGIN (provider "basic"); the subject is derived from it.
ALICE_LOGIN, BOB_LOGIN = "alice", "bob"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_app(login: str):
    from fastapi import FastAPI
    from hermes_cli.web_routers.profiles import router, sessions_router

    app = FastAPI()

    class _Session:
        def __init__(self, uid):
            self.user_id, self.provider, self.issuer = uid, "basic", ""

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.session = _Session(login)
        return await call_next(request)

    app.include_router(sessions_router)
    app.include_router(router)
    return app


def serve(login: str, port: int):
    import uvicorn

    config = uvicorn.Config(build_app(login), host="127.0.0.1", port=port, log_level="error")
    uvicorn.Server(config).run()


class Server:
    def __init__(self, login: str):
        self.port = free_port()
        self.thread = threading.Thread(target=serve, args=(login, self.port), daemon=True)  # health: allow HX012 -- probe server thread carries no profile scope

    def __enter__(self):
        self.thread.start()
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/profiles", timeout=1)
                return f"http://127.0.0.1:{self.port}"
            except Exception:  # health: allow BLE001 -- poll until the probe server answers
                time.sleep(0.1)
        raise SystemExit("сервер не поднялся")

    def __exit__(self, *exc):
        return False


def call(base: str, method: str, path: str, payload=None):
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:  # a non-JSON error page still has to surface as its status
            return e.code, {"raw": body[:400]}


def write_registry(home: Path) -> None:
    import hermes_acl_bindings as hb

    path = home / "acl" / "profile-bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": hb.SCHEMA, "written_at": "2026-11-21T00:00:00+00:00", "written_by": "admin",
        "persons": [
            {"slug": ALICE, "display_name": "Alice", "shared": False, "profiles": ["01-alice"],
             "subjects": [], "owner": ALICE, "source": "admin"},
            {"slug": BOB, "display_name": "Bob", "shared": False, "profiles": ["01-bob"],
             "subjects": [], "owner": BOB, "source": "admin"},
            {"slug": "shared/service", "display_name": "Shared", "shared": True,
             "profiles": ["02-hermes-platform"], "subjects": [], "owner": "admin", "source": "admin"},
        ],
    }), encoding="utf-8")
    hb.reset_cache()


def write_users(home: Path) -> None:
    """A real dashboard users file: two ordinary people, no ``role: owner``.

    Without it ``resolve_subject`` falls back to the caller's legacy subject, i.e. the host owner —
    and the foreign-namespace check would pass for the wrong reason.
    """
    lines = []
    for login in ("alice", "bob"):
        lines.append(json.dumps({
            "login": login, "display_name": login.title(), "password_hash": "x",
            "role": "user", "aliases": [], "telegram_id": "", "disabled": False, "gen": 1,
            "created_at": "2026-11-21T00:00:00+00:00",
        }))
    path = home / "users.json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    out: dict[str, object] = {"home": str(HOME)}

    # ── (а)(б) создание 01-testuser владельцем ──
    with Server(ALICE_LOGIN) as base:
        # acl off (по умолчанию) — субъект владелец, как на однохостовой машине
        status, body = call(base, "POST", "/api/profiles",
                            {"name": "01-testuser", "no_skills": True})
        out["create_status"] = status
        out["create_body"] = body
        soul_path = Path(body["path"]) / "SOUL.md"
        text = soul_path.read_text(encoding="utf-8")
        out["soul_lines"] = len(text.splitlines())
        out["soul_chars"] = len(text)
        out["soul_role_line"] = next(l for l in text.splitlines() if "персональный ассистент" in l)
        markers = ["01-orchestrator", "Kanban", "карточк", "РГЗ", "upgreat", "needs_source"]
        out["forbidden_hits"] = {m: text.count(m) for m in markers}
        out["questionnaire_path"] = body.get("questionnaire_path", "")
        out["questionnaire_exists"] = Path(body.get("questionnaire_path") or "/x").is_file()

        # ── (в) заполненный опросник ──
        before = soul_path.read_text(encoding="utf-8")
        before_sha = hashlib.sha256(before.encode()).hexdigest()
        status, applied = call(base, "POST", "/api/profiles/01-testuser/soul/apply-questionnaire", {
            "address_form": "ты",
            "address_style": "без обращения",
            "answer_length": "кратко",
            "boundaries": ["no_external_uploads"],
            "terms": "МПД = мультипрофильный доступ",
        })
        after = soul_path.read_text(encoding="utf-8")
        out["apply_status"] = status
        out["apply_body"] = applied
        out["apply_before_sha"] = before_sha
        out["apply_after_sha"] = hashlib.sha256(after.encode()).hexdigest()
        out["apply_diff"] = __import__("difflib").unified_diff(
            before.splitlines(), after.splitlines(), "before", "after", lineterm="", n=1)
        out["apply_diff"] = "\n".join(out["apply_diff"])
        mem = Path(body["path"]) / "memories" / "MEMORY.md"
        out["memory_entries"] = mem.read_text(encoding="utf-8") if mem.is_file() else "(нет)"

        # ── (г) пустой опросник ──
        before2 = soul_path.read_bytes()
        sha_before2 = hashlib.sha256(before2).hexdigest()
        status, empty = call(base, "POST", "/api/profiles/01-testuser/soul/apply-questionnaire", {})
        sha_after2 = hashlib.sha256(soul_path.read_bytes()).hexdigest()
        out["empty_status"] = status
        out["empty_body"] = empty
        out["empty_sha_before"] = sha_before2
        out["empty_sha_after"] = sha_after2
        out["empty_identical"] = sha_before2 == sha_after2

    # ── (д) чужой namespace под multi_user + self_service ──
    write_registry(HOME)
    write_users(HOME)
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    cfg.setdefault("acl", {}).update({"enabled": True, "multi_user": True, "profile_self_service": True})
    cfg.setdefault("dashboard", {}).setdefault("basic_auth", {})["users_file"] = str(HOME / "users.json")
    save_config(cfg)
    import hermes_acl_bindings as hb

    hb.reset_cache()
    with Server(BOB_LOGIN) as base:
        status, body = call(base, "POST", "/api/profiles",
                            {"name": "01-alice", "no_skills": True})
        out["foreign_create_status"] = status
        out["foreign_create_body"] = body
        status2, body2 = call(base, "POST", "/api/profiles",
                              {"name": "01-bob", "no_skills": True})
        out["own_create_status"] = status2
        out["own_create_body"] = body2

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
