#!/usr/bin/env python3
"""Живая проба шага 4 (МПД Т5): субъект A не получает сессии субъекта B.

Что делает проба (всё — на КОПИИ, живой `~/.hermes` не трогается):

1. Создаёт изолированный `HERMES_HOME` в scratch с одним `state.db`.
2. Кладёт три сессии: owner (`uid:<n>`), `person/alice`, `person/bob` — с секретными
   маркерами в тексте.
3. Поднимает реальное FastAPI-приложение из `hermes_cli.web_routers.sessions` и ходит по
   КАЖДОМУ пути из §2.2 П6 от имени alice (проверенная сессия дашборда), затем от имени
   владельца.
4. Печатает журнал «путь → статус → утечка?» и сводку.

Запуск: <test-venv>/bin/python verify-step4-live.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="mpd4-probe-"))
    home = scratch / ".hermes"
    home.mkdir(parents=True)
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(REPO))

    import hermes_acl_identity
    import hermes_acl_scope
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway.owner_identity import host_owner_subject
    from hermes_state import SessionDB

    # --- включить МПД ровно так, как это сделает владелец: конфиг + модули
    (home / "config.yaml").write_text("acl:\n  enabled: true\n  multi_user: true\n"
                                      "  identity_trust: untrusted\n", encoding="utf-8")
    hermes_acl_scope.acl_enabled = lambda: True
    hermes_acl_scope.identity_trust = lambda: "untrusted"
    hermes_acl_identity.multi_user_enabled = lambda: True

    OWNER_LOGIN = "owner"

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        if not login:
            return fallback
        return host_owner_subject() if login == OWNER_LOGIN else f"person/{login}"

    hermes_acl_identity.resolve_subject = _resolve

    ALICE, BOB = "person/alice", "person/bob"
    OWNER = host_owner_subject()
    SECRETS = {"alice-s1": "ALICE-ONLY-aa11", "bob-s1": "BOB-ONLY-bb22", "owner-s1": "OWNER-ONLY-cc33"}

    db = SessionDB(db_path=home / "state.db")
    try:
        for sid, owner in (("alice-s1", ALICE), ("bob-s1", BOB), ("owner-s1", OWNER)):
            db.create_session(sid, source="desktop", user_id=owner)
            db._conn.execute("UPDATE sessions SET started_at=?, title=?, model=? WHERE id=?",
                             (int(time.time()), f"chat {owner}", "m", sid))
            db.append_message(sid, role="user", content=SECRETS[sid])
            db.append_message(sid, role="assistant", content=f"reply {sid}")
            db._conn.commit()
            db.end_session(sid, "done")
    finally:
        db.close()

    from hermes_cli.web_routers.sessions import list_router, manage_router, search_router

    class Login:
        def __init__(self, login):
            self.user_id, self.provider, self.issuer = login, "basic", ""

    def client(as_login):
        """A fresh app per caller: middleware cannot be added once an app has started."""
        app = FastAPI()

        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.session = Login(as_login)
            return await call_next(request)

        for router in (list_router, search_router, manage_router):
            app.include_router(router)
        c = TestClient(app)
        return c

    def own_msg_id(sid):
        d = SessionDB(db_path=home / "state.db")
        try:
            return d.get_messages(sid)[0]["id"]
        finally:
            d.close()

    alice_row = own_msg_id("alice-s1")
    bob_row = own_msg_id("bob-s1")

    # §2.2 П6 — 11 путей, названных в анализе, плюс остальные ручки того же файла.
    READS = [
        ("GET", "/api/sessions?limit=50", ""),                       # П6-1 список
        ("GET", "/api/sessions/search?q=ONLY", ""),                   # П6-2 поиск
        ("GET", "/api/sessions/stats", ""),                           # П6-3 статистика
        ("GET", "/api/sessions/{id}", ""),                            # П6-4 деталь
        ("GET", "/api/sessions/{id}/mutation-snapshot", ""),          # П6-5 снапшот
        ("GET", "/api/sessions/{id}/latest-descendant", ""),          # П6-6 наследник
        ("GET", "/api/sessions/{id}/messages", ""),                   # П6-7 сообщения
        ("GET", "/api/sessions/{id}/timeline", ""),                   # П6-8 таймлайн
        ("GET", "/api/sessions/{id}/messages/around?row_id={row}", ""),  # П6-9 страница
        ("GET", "/api/sessions/{id}/export", ""),                     # П6-10 экспорт
        ("GET", "/api/sessions/empty/count", ""),                     # П6-11 пустые
    ]

    log, leaks = [], 0
    with client("alice") as cli:
        for method, path, _q in READS:
            url = path.replace("{id}", "bob-s1").replace("{row}", str(bob_row))
            resp = cli.request(method, url)
            body = resp.text
            leak = SECRETS["bob-s1"] in body or '"bob-s1"' in body
            leaks += leak
            log.append(f"  A→B  {resp.status_code}  leak={'YES' if leak else 'no '}  {url}")

        # Свой путь: должен работать (нет ложных отказов).
        own = [cli.get("/api/sessions/alice-s1"),
               cli.get("/api/sessions/alice-s1/messages"),
               cli.get("/api/sessions/alice-s1/timeline"),
               cli.get(f"/api/sessions/alice-s1/messages/around?row_id={alice_row}"),
               cli.get("/api/sessions/alice-s1/export")]
        own_ok = all(r.status_code == 200 for r in own)

        # Мутации по чужому id.
        muts = [("DELETE", "/api/sessions/bob-s1"),
                ("PATCH", "/api/sessions/bob-s1"),
                ("POST", "/api/sessions/bulk-delete")]
        mut_log = []
        for method, url in muts:
            payload = {"ids": ["bob-s1"], "title": "pwned",
                       "request_id": "r", "expected_revision": 1} if method != "DELETE" else None
            r = cli.request(method, url, json=payload)
            mut_log.append(f"  A→B  {r.status_code}  {method} {url}")

        store_wide = [("POST", "/api/sessions/prune", {"older_than_days": 1}),
                      ("POST", "/api/sessions/owner-backfill", {})]
        sw_log = []
        for method, url, payload in store_wide:
            r = cli.request(method, url, json=payload)
            sw_log.append(f"  A    {r.status_code}  {method} {url}")

    with client(OWNER_LOGIN) as cli:
        owner_list = cli.get("/api/sessions?limit=50").json()
        owner_ids = [row["id"] for row in owner_list["sessions"]]
        owner_prune = cli.post("/api/sessions/prune", json={"older_than_days": 1, "dry_run": True})

    still_there = SessionDB(db_path=home / "state.db")
    try:
        bob_alive = still_there.get_session("bob-s1") is not None
        bob_title = (still_there.get_session("bob-s1") or {}).get("title")
    finally:
        still_there.close()

    print("== ЖИВАЯ ПРОБА ШАГ 4 (МПД Т5) ==")
    print(f"HERMES_HOME пробы: {home}")
    print(f"владелец (subject): {OWNER}")
    print("\n-- A(alice) → сессия B(bob): чтение --")
    print("\n".join(log))
    print("\n-- A(alice): свои пути (200 ожидаемы) --")
    print(f"  own_ok={own_ok}  " + " ".join(f"{r.status_code}" for r in own))
    print("\n-- A(alice) → мутации чужого id --")
    print("\n".join(mut_log))
    print("\n-- A(alice) → общехранилищные операции --")
    print("\n".join(sw_log))
    print("\n-- владелец --")
    print(f"  список владельца: {owner_ids}")
    print(f"  prune dry-run: {owner_prune.status_code}")

    ok = (leaks == 0 and own_ok and bob_alive and bob_title != "pwned"
          and owner_ids == ["owner-s1"] and owner_prune.status_code == 200)
    print("\nИТОГ: " + ("OK — утечек нет, свои пути работают, чужое не тронуто"
                       if ok else "ПРОВАЛ — см. журнал"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
