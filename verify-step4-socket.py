#!/usr/bin/env python3
"""Живая проба сокета шага 4 (МПД Т5, критерий 4): сокет A не получает событий сессий B.

Что делает (всё на изолированном HERMES_HOME, живой ~/.hermes не трогается):

1. Создаёт state.db с сессиями владельца, person/alice и person/bob.
2. Поднимает настоящий ``SessionAuthority`` + ``AuthorityConnection`` — ровно тот слой,
   через который проходит кадр сокета ``/api/ws`` (``_authority_connection`` →
   ``AuthorityConnection``), с грантом, который выдаёт ``socket_operator``.
3. Сокет A(alice): пытается ``session.resume`` чужой сессии B — печатает ответ;
   затем становится зрителем СВОЕЙ сессии.
4. Публикует событие в сессию B и в сессию A, печатает, что реально дошло до
   транспорта сокета A.
5. Сокет владельца: resume сессии B — ожидается успех (владелец видит свои сессии).

Запуск: <python-с-pytest-путём> verify-step4-socket.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent


class RecordingTransport:
    """Транспорт сокета: пишет каждый кадр в общий журнал пробы."""

    def __init__(self, label, journal):
        self.label, self.journal = label, journal

    def write(self, frame):
        self.journal.append((self.label, frame.get("method"),
                             (frame.get("params") or {}).get("type"),
                             (frame.get("params") or {}).get("session_id")))
        return True

    def close(self):
        pass


async def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="mpd4-ws-"))
    home = scratch / ".hermes"
    home.mkdir(parents=True)
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(REPO))

    import hermes_acl_identity
    import hermes_acl_scope

    (home / "config.yaml").write_text(
        "acl:\n  enabled: true\n  multi_user: true\n  identity_trust: untrusted\n",
        encoding="utf-8")
    hermes_acl_scope.acl_enabled = lambda: True
    hermes_acl_scope.identity_trust = lambda: "untrusted"
    hermes_acl_identity.multi_user_enabled = lambda: True

    from gateway.owner_identity import host_owner_subject
    OWNER_LOGIN = "owner"

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        if not login:
            return fallback
        return host_owner_subject() if login == OWNER_LOGIN else f"person/{login}"

    hermes_acl_identity.resolve_subject = _resolve

    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_cli.web_routers.chat_ws import socket_operator
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    ALICE, BOB = "person/alice", "person/bob"
    OWNER = host_owner_subject()

    db = SessionDB(db_path=home / "state.db")
    journal = []
    failures = []
    alice_conn = owner_conn = None
    try:
        for sid, owner in (("alice-s1", ALICE), ("bob-s1", BOB), ("owner-s1", OWNER)):
            db.create_session(sid, source="local", user_id=owner)
            db.append_message(sid, role="user", content=f"{owner} needle")
        epoch = begin_runtime_epoch(db, instance_id="probe")
        authority = SessionAuthority(SimpleNamespace(), profile_id="probe",
                                     instance_id="probe", db=db, epoch=epoch)
        for sid, owner in (("alice-s1", ALICE), ("bob-s1", BOB), ("owner-s1", OWNER)):
            live = LiveSession(SessionSource(platform=Platform.LOCAL, chat_id=sid,
                                             user_id=owner, chat_type="dm"), sid + "-route")
            authority.sessions[sid] = live

        def _identity(login):
            return {"user_id": login, "provider": "basic", "profile_id": "probe",
                    "instance_id": "probe"}

        print("== ЖИВАЯ ПРОБА СОКЕТА ШАГ 4 (МПД Т5) ==")
        print(f"HERMES_HOME пробы: {home}")
        print(f"владелец (subject): {OWNER}")
        print()

        # ── грант оператора, который выдаёт сам сокет ───────────────────────
        alice_operator = socket_operator(_identity("alice"))
        owner_operator = socket_operator(_identity(OWNER_LOGIN))
        print(f"-- грант сокета (socket_operator): alice={alice_operator}  owner={owner_operator}")
        if alice_operator is not False or owner_operator is not True:
            failures.append("socket_operator: alice должен получить False, владелец True")

        alice_conn = AuthorityConnection(authority, RecordingTransport("A(alice)", journal),
                                         _identity("alice"), operator=alice_operator)
        owner_conn = AuthorityConnection(authority, RecordingTransport("owner", journal),
                                         _identity(OWNER_LOGIN), operator=owner_operator)
        print(f"-- actor сокета A: subject={alice_conn.actor.subject} "
              f"capabilities={sorted(alice_conn.actor.capabilities)}")
        print()

        # ── A пытается открыть чужую сессию B по прямому id ─────────────────
        print("-- сокет A(alice) → session.resume чужой сессии B --")
        for sid in ("bob-s1", "owner-s1"):
            reply = await alice_conn.dispatch(
                {"id": 1, "method": "session.resume", "params": {"session_id": sid}})
            reason = (reply.get("error") or {}).get("data", {}).get("reason")
            print(f"  A→{sid}: {'ok' if 'result' in reply else 'refused'} reason={reason}")
            if "result" in reply:
                failures.append(f"A открыл чужую сессию {sid}")
            if reason not in ("permission_denied", "not_found"):
                failures.append(f"A→{sid}: неожиданная причина {reason!r}")

        # ── A зритель СВОЕЙ сессии ──────────────────────────────────────────
        own = await alice_conn.dispatch(
            {"id": 2, "method": "session.resume", "params": {"session_id": "alice-s1"}})
        print(f"-- A(alice) своя сессия alice-s1: "
              f"{'ok' if 'result' in own else reply and (own.get('error') or {}).get('data')}")
        if "result" not in own:
            failures.append("A не открыл свою сессию alice-s1")
        print()

        # ── публикация: событие B не должно дойти до сокета A ───────────────
        print("-- публикация событий и что дошло до сокета A --")
        journal.clear()
        for sid, owner in (("bob-s1", BOB), ("alice-s1", ALICE)):
            authority.sessions[sid].event_stream.publish(
                sid, {"text": f"{owner} live needle"}, event_type="message.complete")
        for row in journal:
            print(f"  доставлено сокету {row[0]}: type={row[2]} session_id={row[3]}")
        leaked = [row for row in journal if row[0] == "A(alice)" and row[3] == "bob-s1"]
        got_own = [row for row in journal if row[0] == "A(alice)" and row[3] == "alice-s1"]
        print(f"  утечка события B в сокет A: {'ДА' if leaked else 'нет'}"
              f"   своё событие дошло: {'да' if got_own else 'НЕТ'}")
        if leaked:
            failures.append("сокет A получил событие сессии B")
        if not got_own:
            failures.append("сокет A не получил событие своей сессии (проба ничего не меряет)")

        # ── владелец видит B ────────────────────────────────────────────────
        print()
        print("-- сокет владельца → session.resume сессии B --")
        owner_reply = await owner_conn.dispatch(
            {"id": 3, "method": "session.resume", "params": {"session_id": "bob-s1"}})
        owner_ok = "result" in owner_reply
        print(f"  owner→bob-s1: {'ok' if owner_ok else 'refused'}")
        if not owner_ok:
            failures.append("владелец не открыл сессию B (свои сессии должны работать)")

        # ── журнал пробы ────────────────────────────────────────────────────
        print()
        print("-- журнал пробы (сокет → тип → сессия) --")
        if not journal:
            print("  (пусто: публикация не дошла ни до кого)")
        print()
        print("ИТОГ: " + ("OK — сокет A не видит событий B, владелец видит" if not failures
                         else "FAIL — " + "; ".join(failures)))
        return 0 if not failures else 1
    finally:
        for conn in (alice_conn, owner_conn):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
