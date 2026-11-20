#!/usr/bin/env python3
"""Живая проба сокета дашборда шага 4 (МПД Т5, критерии 4 и 5).

Что измеряется (всё на изолированном ``HERMES_HOME``, живой ``~/.hermes`` не трогается):

1. грант ``socket_operator``: второй человек — не оператор, владелец — оператор;
2. по каждой ручке сокета по прямому id: своя сессия открывается, чужая — нет;
3. списки: ``session.list`` и ``session.active_list`` у A содержат только A, у владельца — всё;
4. **критерий 4**: событие сессии B не доходит до сокета A (при этом событие своей сессии
   доходит — иначе «нет утечки» означало бы «проба ничего не измерила»), а владелец получает все;
5. **критерий 5**: владелец открывает каждую строку как раньше;
6. отказ неотличим от отсутствия: чужой id и несуществующий дают один и тот же кадр.

Запуск: venv-питон репозитория, ``python3 verify-step4-socket.py``.
Код возврата 0 — все проверки сошлись, 1 — есть расхождение (печатается списком).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent

OWNER_LOGIN = "owner"
ALICE = "person/alice"
BOB = "person/bob"
OWN_CHAT = "alice-chat"
FOREIGN_CHAT = "bob-chat"
MISSING_CHAT = "no-such-id"

# Параметры каждой ручки кроме ``session_id`` (закрытый набор, иначе ``invalid_params``).
VERBS = (
    ("session.info", {}),
    ("session.resume", {}),
    ("session.events.since", {"replay_epoch": "x", "last_sequence": 0}),
    ("session.mutate", {"request_id": "r", "expected_revision": 0,
                        "operation": "rename", "payload": {"title": "t"}}),
)


class _Nothing:
    """Заглушка раннера: любой запрошенный атрибут отвечает «ничего нет»."""

    def __call__(self, *a, **k):
        return None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    def __bool__(self):
        return False

    def __iter__(self):
        return iter(())

    def __contains__(self, item):
        return False


class _Runner(SimpleNamespace):
    """Раннер-заглушка: инвентарь сессии спрашивает у него десяток необязательных свойств."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _NOTHING


_NOTHING = _Nothing()


class RecordingTransport:
    """Транспорт сокета: каждый кадр пишется в общий журнал пробы."""

    def __init__(self, label, journal):
        self.label, self.journal = label, journal

    def write(self, frame):
        params = frame.get("params") or {}
        self.journal.append((self.label, frame.get("method"), params.get("type"),
                             params.get("session_id")))
        return True

    def close(self):
        pass


def _reason(reply):
    """Причина отказа из кадра JSON-RPC (или ``OK`` для успеха)."""
    if "result" in reply:
        return "OK"
    error = reply.get("error") or {}
    return str((error.get("data") or {}).get("reason") or error.get("message"))


def _wait_for(journal, predicate, timeout=2.0):
    """Веер пишет из своего потока: ждём кадр, а не спим наугад."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(predicate(row) for row in journal):
            break
        time.sleep(0.02)
    return [row for row in journal if predicate(row)]


def _ids(reply):
    if "result" not in reply:
        return []
    return sorted(row["id"] for row in reply["result"]["sessions"])


def _setup(home):
    """Изолированный дом в режиме МПД: флаги ACL и вход→субъект, как их читает прод-код."""
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

    def _resolve(value, fallback=""):
        login = str(value or "").strip()
        if not login:
            return fallback
        return host_owner_subject() if login == OWNER_LOGIN else f"person/{login}"

    hermes_acl_identity.resolve_subject = _resolve
    return host_owner_subject()


def _seed(home, owner_subject):
    """state.db с четырьмя строками (A, B, владелец, без владельца) и живыми маршрутами."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_authority import LiveSession, SessionAuthority
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    rows = {OWN_CHAT: ALICE, FOREIGN_CHAT: BOB,
            "owner-chat": owner_subject, "unowned-chat": None}
    db = SessionDB(db_path=home / "state.db")
    for sid, stamp in rows.items():
        db.create_session(sid, source="telegram", user_id=stamp, chat_id="c-" + sid,
                          session_key="k-" + sid)
        db.append_message(sid, role="user", content=f"needle {sid}")
    db._conn.commit()
    epoch = begin_runtime_epoch(db, instance_id="probe")
    authority = SessionAuthority(_Runner(), profile_id=str(home), instance_id="probe",
                                 db=db, epoch=epoch)
    for sid, stamp in rows.items():
        authority.sessions[sid] = LiveSession(
            SessionSource(platform=Platform.TELEGRAM, chat_id="c-" + sid,
                          user_id=stamp or "", chat_type="dm"), "route-" + sid)
    return db, authority, rows


def _connect(authority, home, login, label, journal):
    """Сокет с тем же грантом, что выдаёт настоящий ``/api/ws``."""
    from gateway.session_controls import AuthorityConnection
    from hermes_cli.web_routers.chat_ws import socket_operator

    identity = {"user_id": login, "provider": "basic", "profile_id": str(home),
                "instance_id": "probe"}
    return AuthorityConnection(authority, RecordingTransport(label, journal), identity,
                               operator=socket_operator(identity))


async def _phase_grant(home, alice, failures):
    """Фаза 1: кто получает ``session:operator``."""
    from hermes_cli.web_routers.chat_ws import socket_operator

    def _identity(login):
        return {"user_id": login, "provider": "basic", "profile_id": str(home),
                "instance_id": "probe"}

    alice_op = socket_operator(_identity("alice"))
    owner_op = socket_operator(_identity(OWNER_LOGIN))
    print(f"-- грант сокета (socket_operator): alice={alice_op} owner={owner_op}")
    if alice_op is not False:
        failures.append("socket_operator: второй человек получил грант оператора")
    if owner_op is not True:
        failures.append("socket_operator: владелец НЕ получил грант оператора")
    print(f"-- actor сокета A: subject={alice.actor.subject} caps={sorted(alice.actor.capabilities)}")
    print()


async def _phase_direct(alice, failures):
    """Фаза 2: открытие по прямому id — своя / чужая / несуществующая."""
    print("-- A(alice) по прямому id: своя / чужая / несуществующая --")
    print(f"  {'ручка':26} {'своя':12} {'чужая (bob)':14} {'нет такой':12} кадры совпали")
    for method, extra in VERBS:
        replies = {}
        for label, sid in (("own", OWN_CHAT), ("foreign", FOREIGN_CHAT),
                           ("missing", MISSING_CHAT)):
            replies[label] = await alice.dispatch(
                {"id": 1, "method": method, "params": dict(extra, session_id=sid)})
        same = replies["foreign"] == replies["missing"]
        print(f"  {method:26} {_reason(replies['own']):12} "
              f"{_reason(replies['foreign']):14} {_reason(replies['missing']):12} "
              f"{'да' if same else 'НЕТ'}")
        if _reason(replies["own"]) != "OK":
            failures.append(f"{method}: своя сессия недоступна ({_reason(replies['own'])})")
        if _reason(replies["foreign"]) == "OK":
            failures.append(f"{method}: ЧУЖАЯ сессия открылась")
        if not same:
            failures.append(f"{method}: отказ по чужому id отличим от отсутствия (оракул id)")
        if f"needle {FOREIGN_CHAT}" in str(replies["foreign"]):
            failures.append(f"{method}: в ответе чужой транскрипт")
    print()


async def _phase_lists(alice, owner, rows, failures):
    """Фаза 3: списки и владельческие операции."""
    print("-- списки: что видит каждый сокет --")
    for label, conn in (("A(alice)", alice), ("owner", owner)):
        live = await conn.dispatch({"id": 2, "method": "session.list", "params": {}})
        active = await conn.dispatch({"id": 3, "method": "session.active_list", "params": {}})
        ids, aids = _ids(live), _ids(active)
        print(f"  {label:10} session.list={ids}")
        print(f"  {'':10} session.active_list={aids}")
        if label != "A(alice)":
            if set(ids) != set(rows):
                failures.append(f"session.list владельца: {ids} вместо всех {sorted(rows)}")
            if len(ids) != len(rows):
                failures.append(f"владелец видит {len(ids)} строк вместо {len(rows)}")
            continue
        if ids != [OWN_CHAT]:
            failures.append(f"session.list A: {ids} вместо ['{OWN_CHAT}']")
        if aids != [OWN_CHAT]:
            failures.append(f"session.active_list A: {aids} вместо ['{OWN_CHAT}']")
        if FOREIGN_CHAT in str(live) + str(active):
            failures.append("в списке A есть чужая сессия")
    st = await alice.dispatch({"id": 4, "method": "session.prune",
                               "params": {"never_active_days": 1}})
    print(f"  A(alice) session.prune={_reason(st)} (ожидается permission_denied)")
    if _reason(st) != "permission_denied":
        failures.append("session.prune не отказал не-владельцу")
    print()


async def _phase_events(authority, alice, owner, rows, journal, failures):
    """Фаза 4 (критерий 4): что реально дошло до сокета A."""
    print("-- поток событий: что реально дошло до сокета A --")
    # Владелец подписывается на все свои строки: тогда «0 событий у A про B» не означает
    # «события не доставляются никому» — владельцу они доходят.
    for sid in rows:
        await owner.dispatch({"id": 7, "method": "session.resume",
                              "params": {"session_id": sid}})
    journal.clear()
    for sid in rows:
        authority.sessions[sid].event_stream.publish(
            sid, {"text": f"live {sid}"}, event_type="message.complete")
    got_own = _wait_for(journal, lambda r: r[0] == "A(alice)" and r[3] == OWN_CHAT)
    # Каждый транспорт пишет своим потоком: дожидаемся последнего кадра, иначе «нет утечки»
    # значило бы «проба не успела посмотреть».
    _wait_for(journal, lambda r: r[1] == "event" and r[3] == "unowned-chat")
    leaked = [r for r in journal if r[0] == "A(alice)" and r[3] != OWN_CHAT]
    got_owner = sorted({r[3] for r in journal if r[0] == "owner" and r[1] == "event"})
    print(f"  своё событие A(alice) дошло: {'да' if got_own else 'НЕТ'}")
    print(f"  чужие события в сокете A: {leaked or 'нет'}")
    print(f"  события в сокете владельца: {got_owner or 'нет'}")
    if not got_own:
        failures.append("сокет A не получил событие СВОЕЙ сессии (проба ничего не доказала бы)")
    if leaked:
        failures.append(f"УТЕЧКА: сокет A получил чужие события {leaked}")
    if sorted(got_owner) != sorted(rows):
        failures.append(f"владелец получил события {got_owner} вместо всех {sorted(rows)}")
    print()


async def _phase_owner(owner, rows, failures):
    """Фаза 5 (критерий 5): владелец открывает каждую строку, как раньше."""
    print("-- владелец: открытие по прямому id --")
    for sid in rows:
        reply = await owner.dispatch({"id": 5, "method": "session.resume",
                                      "params": {"session_id": sid}})
        print(f"  owner→{sid}: {_reason(reply)}")
        if _reason(reply) != "OK":
            failures.append(f"владелец не открыл {sid}: {_reason(reply)}")
    print()


async def _phase_oracle(alice, failures):
    """Фаза 6: отказ не подтверждает существование id."""
    print("-- отказ не оракул: чужой id против несуществующего --")
    foreign = await alice.dispatch({"id": 6, "method": "session.resume",
                                    "params": {"session_id": FOREIGN_CHAT}})
    missing = await alice.dispatch({"id": 6, "method": "session.resume",
                                    "params": {"session_id": MISSING_CHAT}})
    print(f"  идентичны: {'да' if foreign == missing else 'НЕТ'}  ({_reason(foreign)})")
    if foreign != missing:
        failures.append("ответ на чужой id отличается от ответа на несуществующий")
    print()


async def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="mpd4-ws-")) / ".hermes"
    home.mkdir(parents=True)
    owner_subject = _setup(home)
    db, authority, rows = _seed(home, owner_subject)
    journal: list = []
    failures: list[str] = []
    try:
        alice = _connect(authority, home, "alice", "A(alice)", journal)
        owner = _connect(authority, home, OWNER_LOGIN, "owner", journal)
        print("== ЖИВАЯ ПРОБА СОКЕТА ШАГ 4 (МПД Т5) ==")
        print(f"HERMES_HOME пробы: {home}")
        print(f"владелец (subject): {owner_subject}")
        print()
        await _phase_grant(home, alice, failures)
        await _phase_direct(alice, failures)
        await _phase_lists(alice, owner, rows, failures)
        await _phase_events(authority, alice, owner, rows, journal, failures)
        await _phase_owner(owner, rows, failures)
        await _phase_oracle(alice, failures)
        await alice.close()
        print(f"ИТОГ: {'OK' if not failures else 'FAIL'}")
        for line in failures:
            print(f"  • {line}")
        return 0 if not failures else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
