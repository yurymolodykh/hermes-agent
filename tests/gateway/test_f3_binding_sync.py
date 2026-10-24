"""F3 (t_0aedc91c, tg-delivery-contract) unit tests: binding sync + file merge + PWA marker.

Покрывают три части фикса F3 (v2):
  1. ``tools.owui_tg_binding_sync`` — консервативный синк привязок из
     webui.db + channel_directory.json (однозначность, короткие имена,
     ALLOWED-фильтр, dry-run/-write).
  2. ``hermes_cli.delivery_policy.owui_telegram_users`` — чтение синк-файла
     как низкоприоритетного источника (merge, не-override, corrupt → {}).
  3. ``gateway.kanban_watchers`` PWA-маркер ``owui_user_id_attr`` —
     api_server-подписка с маркером атрибутируется OWUI-пользователю.

Все тесты изолированы: временные файлы/БД, никакой записи в прод-конфиг.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from hermes_cli import delivery_policy as dp


# ---------------------------------------------------------------------------
# 1. tools.owui_tg_binding_sync
# ---------------------------------------------------------------------------

def _make_webui_db(path, users):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT)")
    for u in users:
        con.execute(
            "INSERT INTO user (id, name, email, role) VALUES (?,?,?,?)",
            (u["id"], u["name"], u["email"], u["role"]),
        )
    con.commit()
    con.close()


def _make_channel(path, contacts):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"updated_at": "t", "platforms": {"telegram": contacts}},
            fh, ensure_ascii=False,
        )


def _default_users():
    return [
        {"id": "9ae3eca0-d6e9-45d5-b4af-414afe4616b9", "name": "Юрий Молодых",
         "email": "yury.molodykh@hermes.upgreat.one", "role": "admin"},
        {"id": "3daba661-feb8-4c1d-a823-4c5223f98d30", "name": "Мария Сафронова",
         "email": "maria.safronova@hermes.upgreat.one", "role": "user"},
        {"id": "0795993b-4cc5-431f-a7fb-45b18967a9ba", "name": "Ксения Козловская",
         "email": "ksenia.kozlovskaya@hermes.upgreat.one", "role": "user"},
        {"id": "54ba7089-81ee-41fa-a458-f8fa09474a70", "name": "Владимир Смирнов",
         "email": "vladimir.smirnov@hermes.upgreat.one", "role": "user"},
        {"id": "ddeea376-29f0-4ba3-ba93-c93f3e94e635", "name": "Сергей Лобанов",
         "email": "sergei.lobanov@hermes.upgreat.one", "role": "user"},
        {"id": "b5b7fc5b-4afd-4dcd-a4bf-7abd999a16ee", "name": "Данил Сербин",
         "email": "danil.serbin@hermes.upgreat.one", "role": "user"},
    ]


def _contacts_default():
    return [
        {"id": "125805788", "name": "Yury Molodykh", "type": "dm"},
        {"id": "543447681", "name": "Мария", "type": "dm"},
        {"id": "320318064", "name": "Ксения Козловская", "type": "dm"},
        {"id": "1215917988", "name": "Vladimir Smirnov", "type": "dm"},
        {"id": "520461899", "name": "Danil", "type": "dm"},
        {"id": "291434967", "name": "Сергей Лобанов \"Авача\"", "type": "dm"},
        # неоднозначные / короткие — не матчатся
        {"id": "449439313", "name": "M M", "type": "dm"},
        {"id": "90541313", "name": "Abramova Anastasia", "type": "dm"},
        {"id": "230083475", "name": "Tatiana Maximova", "type": "dm"},
    ]


from tools import owui_tg_binding_sync as sync  # noqa: E402


def test_sync_discovers_only_unambiguous_bindings(tmp_path, monkeypatch):
    db = tmp_path / "webui.db"
    ch = tmp_path / "channel_directory.json"
    _make_webui_db(str(db), _default_users())
    _make_channel(str(ch), _contacts_default())
    # No ALLOWED filter: everything discovered is safe.
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)

    res = sync.discover_bindings(str(db), str(ch))
    bindings = res["bindings"]
    # D0 C4-подтверждённые однозначные пары.
    assert bindings.get("3daba661-feb8-4c1d-a823-4c5223f98d30") == "543447681", bindings
    assert bindings.get("0795993b-4cc5-431f-a7fb-45b18967a9ba") == "320318064"
    assert bindings.get("54ba7089-81ee-41fa-a458-f8fa09474a70") == "1215917988"
    assert bindings.get("b5b7fc5b-4afd-4dcd-a4bf-7abd999a16ee") == "520461899"
    assert bindings.get("ddeea376-29f0-4ba3-ba93-c93f3e94e635") == "291434967"
    # Владелец: точное латинское совпадение.
    assert bindings.get("9ae3eca0-d6e9-45d5-b4af-414afe4616b9") == "125805788"
    # Fail-closed: короткие/неоднозначные не попали.
    for uid in bindings:
        assert uid not in ("449439313", "90541313", "230083475")
    # Неоднозначные в actually-skipped.
    skipped_chats = {s["chat_id"] for s in res["skipped"]}
    assert "449439313" in skipped_chats
    assert "90541313" in skipped_chats
    assert "230083475" in skipped_chats


def test_sync_ambiguous_name_never_matches(tmp_path, monkeypatch):
    db = tmp_path / "webui.db"
    ch = tmp_path / "channel_directory.json"
    # Два пользователя с одинаковым нормализованным именем → неоднозначность.
    _make_webui_db(str(db), [
        {"id": "u1", "name": "Александр Шашков", "email": "a1@x.invalid", "role": "user"},
        {"id": "u2", "name": "Александр Шашков", "email": "a2@x.invalid", "role": "user"},
    ])
    _make_channel(str(ch), [
        {"id": "111", "name": "Александр Шашков", "type": "dm"},
    ])
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    res = sync.discover_bindings(str(db), str(ch))
    assert res["bindings"] == {}, res["bindings"]
    assert "111" in {s["chat_id"] for s in res["skipped"]}


def test_sync_short_name_never_matches(tmp_path, monkeypatch):
    db = tmp_path / "webui.db"
    ch = tmp_path / "channel_directory.json"
    _make_webui_db(str(db), [
        {"id": "u1", "name": "Максим Матросов", "email": "mm@x.invalid", "role": "user"},
    ])
    _make_channel(str(ch), [{"id": "449439313", "name": "M M", "type": "dm"}])
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    res = sync.discover_bindings(str(db), str(ch))
    assert res["bindings"] == {}, res["bindings"]
    assert "449439313" in {s["chat_id"] for s in res["skipped"]}


def test_sync_allowlist_filter_drops_non_allowed(tmp_path, monkeypatch):
    db = tmp_path / "webui.db"
    ch = tmp_path / "channel_directory.json"
    _make_webui_db(str(db), _default_users())
    _make_channel(str(ch), _contacts_default())
    # Только владелец в ALLOWED → все остальные отфильтрованы.
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "125805788")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "")
    res = sync.discover_bindings(str(db), str(ch))
    assert res["bindings"] == {
        "9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "125805788",
    }, res["bindings"]


def test_sync_write_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "webui.db"
    ch = tmp_path / "channel_directory.json"
    _make_webui_db(str(db), _default_users())
    _make_channel(str(ch), _contacts_default())
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    out = tmp_path / "tg_bindings.json"
    res = sync.discover_bindings(str(db), str(ch))
    sync.write_bindings_file(res["bindings"], str(out), sources=res["sources"])
    data = json.load(open(out, encoding="utf-8"))
    assert set(data["bindings"]) == set(res["bindings"])
    assert data["schema"] == "tg-delivery-contract/F3-bindings-v1"


# ---------------------------------------------------------------------------
# 2. delivery_policy.owui_telegram_users + binding file
# ---------------------------------------------------------------------------

def test_binding_file_merge_adds_missing(tmp_path, monkeypatch):
    # Explicit cfg binding for the owner; file adds a foreign user.
    binding_cfg = {"9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "125805788"}
    f = tmp_path / "tb.json"
    with open(f, "w", encoding="utf-8") as fh:
        json.dump({"bindings": {"3daba661-feb8-4c1d-a823-4c5223f98d30": "543447681"}}, fh)
    out = dp.owui_telegram_users({"openwebui_telegram_users": binding_cfg,
                                  "openwebui_telegram_bindings_file": str(f)})
    assert out["9ae3eca0-d6e9-45d5-b4af-414afe4616b9"] == "125805788"
    assert out["3daba661-feb8-4c1d-a823-4c5223f98d30"] == "543447681"


def test_binding_file_does_not_override_cfg(tmp_path):
    binding_cfg = {"9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "125805788"}
    f = tmp_path / "tb.json"
    with open(f, "w", encoding="utf-8") as fh:
        json.dump({"bindings": {"9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "999"}}, fh)
    out = dp.owui_telegram_users({"openwebui_telegram_users": binding_cfg,
                                  "openwebui_telegram_bindings_file": str(f)})
    assert out["9ae3eca0-d6e9-45d5-b4af-414afe4616b9"] == "125805788"


def test_binding_file_corrupt_fail_closed(tmp_path):
    binding_cfg = {"9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "125805788"}
    f = tmp_path / "tb.json"
    f.write_text("{corrupt", encoding="utf-8")
    out = dp.owui_telegram_users({"openwebui_telegram_users": binding_cfg,
                                  "openwebui_telegram_bindings_file": str(f)})
    assert out == binding_cfg


def test_binding_file_missing_is_empty(tmp_path):
    missing = tmp_path / "nope.json"
    out = dp.owui_telegram_users({"openwebui_telegram_bindings_file": str(missing)})
    assert out == {}


def test_binding_file_env_path(monkeypatch, tmp_path):
    f = tmp_path / "tb.json"
    with open(f, "w", encoding="utf-8") as fh:
        json.dump({"bindings": {"3daba661-feb8-4c1d-a823-4c5223f98d30": "543447681"}}, fh)
    monkeypatch.setenv("HERMES_OPENWEBUI_TELEGRAM_BINDINGS_FILE", str(f))
    out = dp.owui_telegram_users({})
    assert out["3daba661-feb8-4c1d-a823-4c5223f98d30"] == "543447681"


# ---------------------------------------------------------------------------
# 3. gateway.delivery_engine — PWA-маркер owui_user_id_attr (unit-level)
# ---------------------------------------------------------------------------
#
# R1 (d3cfc258d5): the owner-resolution logic that used to live in the
# removed ``_sub_owui_owner_id`` now lives in
# ``DeliveryEngine._tg_bound_chat_for`` — api_server sub -> owner's bound TG
# chat, with ``user_id`` taking priority over the server-side
# ``delivery_metadata[owui_user_id_attr]`` marker (F3), fail-closed when both
# are absent.

def _engine_tg_chat_for(sub, *, owui_to_tg=None):
    from gateway.delivery_engine import DeliveryEngine
    eng = DeliveryEngine(owui_to_tg=owui_to_tg or {})
    return eng._tg_bound_chat_for(sub, "api_server")


def test_sub_owui_owner_id_user_id_priority():
    # ``user_id`` wins over the F3 marker even when both are present.
    assert _engine_tg_chat_for(
        {"user_id": "A", "delivery_metadata": {"owui_user_id_attr": "B"}},
        owui_to_tg={"A": "tg-A", "B": "tg-B"},
    ) == "tg-A"


def test_sub_owui_owner_id_falls_back_to_attr():
    # Marker-only PWA sub (no user_id) falls back to the F3 marker owner.
    assert _engine_tg_chat_for(
        {
            "user_id": "",
            "delivery_metadata": {
                "owui_user_id_attr": "9ae3eca0-d6e9-45d5-b4af-414afe4616b9",
            },
        },
        owui_to_tg={"9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "tg-owner"},
    ) == "tg-owner"


def test_sub_owui_owner_id_empty_fail_closed():
    # No owner (neither user_id nor marker) -> no TG lane (OWUI-only).
    assert _engine_tg_chat_for(
        {"user_id": "", "delivery_metadata": {}},
        owui_to_tg={"whoever": "tg-x"},
    ) is None
    assert _engine_tg_chat_for({"user_id": None}) is None
