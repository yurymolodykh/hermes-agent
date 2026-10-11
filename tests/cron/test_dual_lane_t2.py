"""
T2 (РГЗ/tghyg): dual-lane OWUI→TG доставка по серверной привязке.

Механика (спека research/delivery-policy.md §8):
  §8.1  Привязка ``openwebui_telegram_users`` (config.yaml / env
        HERMES_OPENWEBUI_TELEGRAM_USERS, env wins) — ЕДИНСТВЕННЫЙ источник
        права «TG-чат принадлежит данному OWUI-пользователю». Отсутствие =
        OWUI-only (fail-closed, поведение V8).
  §8.2  Для OWUI-джоба с api_server-лентой при policy on и известном
        authoritative owner из серверного реестра при наличии привязки
        owner ∈ ALLOWED/home к targets ДОБАВЛЯЕТСЯ telegram:<bound_chat> —
        тот же пер-таргетный гейт (classify + verify).
        verify_recipient кросс-платформы api_server→telegram проходит
        ТОЛЬКО если target.chat_id == owui_to_tg[initiator.user_id] и этот
        чат ∈ ALLOWED/home.
  §8.3  Чужой OWUI-пользователь (не в привязке) → verify=False → TG
        suppressed даже для ценного контента.  Рутина/no-delta → suppress
        независимо от привязки.  deliver=local / не-api инициаторы не
        затрагиваются.
  §8.4  Существующие джобы ``deliver: api_server:…,telegram:…`` перестают
        падать с «mixed push/API targets»: обе ленты работают, TG-лента
        проверяется по привязке.

Никаких реальных Telegram-sendMessage здесь нет: гейт проверяется на
живом коде, отправка не выполняется (паттерн test_delivery_matrix_t1).
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from hermes_cli.delivery_policy import (
    InitiatorRef,
    TargetRef,
    verify_recipient,
)

OWNER_TG = "125805788"
ALLOWED = [OWNER_TG]
# Второй (чужой) пользователь OWUI, НЕ в привязке.
FOREIGN_OWUI = "3daba661-feb8-4c1d-a823-4c5223f98d30"
# Владелец OWUI (authoritative owner_id из серверного реестра).
OWNER_OWUI = "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"

ARTIFACT = "Итог:\nMEDIA: C:/tmp/report.pdf\nполный отчёт приложен"
FINAL_REPORT = (
    "Итоговый отчёт периода: закрыто 12 задач, 3 артефакта, все цели достигнуты."
)
ROUTINE = '{"copied": 0, "missing_in_kb": 0, "errors": 0}'

OWNER_TG_TARGET = {
    "platform": "telegram",
    "chat_id": OWNER_TG,
    "thread_id": None,
}


# ---------------------------------------------------------------------------
# §8.1 — загрузчик привязки owui_telegram_users()
# ---------------------------------------------------------------------------

def test_binding_from_env_json_wins(monkeypatch):
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        '{"' + OWNER_OWUI + '": "' + OWNER_TG + '"}',
    )
    from hermes_cli.delivery_policy import owui_telegram_users

    assert owui_telegram_users() == {OWNER_OWUI: OWNER_TG}


def test_binding_env_overrides_config(monkeypatch):
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        '{"' + OWNER_OWUI + '": "' + OWNER_TG + '"}',
    )
    from hermes_cli.delivery_policy import owui_telegram_users

    # config-значение должно игнорироваться: env wins (§8.1)
    cfg = {"openwebui_telegram_users": {FOREIGN_OWUI: "111111"}}
    assert owui_telegram_users(cfg) == {OWNER_OWUI: OWNER_TG}


def test_binding_from_per_call_cfg_dict():
    from hermes_cli.delivery_policy import owui_telegram_users

    cfg = {"openwebui_telegram_users": {OWNER_OWUI: OWNER_TG}}
    assert owui_telegram_users(cfg) == {OWNER_OWUI: OWNER_TG}


def test_binding_absent_fail_closed(monkeypatch):
    monkeypatch.delenv("HERMES_OPENWEBUI_TELEGRAM_USERS", raising=False)
    from hermes_cli.delivery_policy import owui_telegram_users

    assert owui_telegram_users({}) == {}


def test_binding_malformed_env_fail_closed(monkeypatch):
    monkeypatch.setenv("HERMES_OPENWEBUI_TELEGRAM_USERS", "not-json{{{")
    from hermes_cli.delivery_policy import owui_telegram_users

    assert owui_telegram_users() == {}


def test_binding_non_object_env_fail_closed(monkeypatch):
    monkeypatch.setenv("HERMES_OPENWEBUI_TELEGRAM_USERS", "[1,2,3]")
    from hermes_cli.delivery_policy import owui_telegram_users

    assert owui_telegram_users() == {}


# ---------------------------------------------------------------------------
# §8.2.3 — verify_recipient по привязке (кросс-платформа api_server→telegram)
# ---------------------------------------------------------------------------

def _api_initiator(user_id: str) -> InitiatorRef:
    return InitiatorRef("api_server", "owui-chat-x", user_id)


def _tg_target(chat_id: str) -> TargetRef:
    return TargetRef("telegram", chat_id)


def test_verify_recipient_owui_owner_bound_chat_passes():
    # Владелец в привязке, target = его собственный bound-чат ∈ ALLOWED.
    binding = {OWNER_OWUI: OWNER_TG}
    assert verify_recipient(
        _api_initiator(OWNER_OWUI),
        _tg_target(OWNER_TG),
        home_channel=OWNER_TG,
        allowed_users=ALLOWED,
        owui_to_tg=binding,
    ) is True


def test_verify_recipient_owui_owner_bound_home_passes():
    binding = {OWNER_OWUI: OWNER_TG}
    # home_channel совпадает с bound — тоже ALLOWED/home (§8.2.3).
    assert verify_recipient(
        _api_initiator(OWNER_OWUI),
        _tg_target(OWNER_TG),
        home_channel=OWNER_TG,
        allowed_users=[],
        owui_to_tg=binding,
    ) is True


def test_verify_recipient_foreign_owui_not_in_binding_rejected():
    # Чужой OWUI-пользователь (не в привязке) → verify=False, даже ценный
    # контент и даже если user_id числится в ALLOWED (V1-фолбэк не включается,
    # когда привязка сконфигурирована).
    binding = {OWNER_OWUI: OWNER_TG}
    assert verify_recipient(
        _api_initiator(FOREIGN_OWUI),
        _tg_target(OWNER_TG),
        home_channel=OWNER_TG,
        allowed_users=ALLOWED + [FOREIGN_OWUI],
        owui_to_tg=binding,
    ) is False


def test_verify_recipient_bound_user_other_chat_rejected():
    # Перекрёстная утечка в ЧУЖОЙ чат невозможна: target != bound-чат.
    binding = {OWNER_OWUI: OWNER_TG}
    assert verify_recipient(
        _api_initiator(OWNER_OWUI),
        _tg_target("555555555"),
        home_channel=OWNER_TG,
        allowed_users=ALLOWED,
        owui_to_tg=binding,
    ) is False


def test_verify_recipient_owui_bound_chat_not_allowed_rejected():
    # Привязка вендорит пользователю чат, НЕ входящий в ALLOWED/home —
    # прав на него нет (fail-closed).
    binding = {OWNER_OWUI: "123456"}
    assert verify_recipient(
        _api_initiator(OWNER_OWUI),
        TargetRef("telegram", "123456"),
        home_channel=OWNER_TG,
        allowed_users=ALLOWED,
        owui_to_tg=binding,
    ) is False


def test_verify_recipient_empty_binding_keeps_v1(monkeypatch):
    # Пустая привязка (не сконфигурирована) → прежнее V1-поведение не
    # ломается: кросс-платформа разрешена для владельца в ALLOWED (F1/F3).
    result = verify_recipient(
        _api_initiator(OWNER_TG),
        _tg_target(OWNER_TG),
        home_channel=OWNER_TG,
        allowed_users=ALLOWED,
        owui_to_tg={},
    )
    assert result is True


def test_verify_recipient_foreign_when_no_binding_v1_still_rejected():
    # Без привязки чужой OWUI-user (не в ALLOWED) по-прежнему не проходит.
    result = verify_recipient(
        _api_initiator(FOREIGN_OWUI),
        _tg_target(OWNER_TG),
        home_channel=OWNER_TG,
        allowed_users=ALLOWED,
        owui_to_tg={},
    )
    assert result is False


# ---------------------------------------------------------------------------
# §8.2.2 — расширение api_server-ленты: dual-lane через живой гейт
# ---------------------------------------------------------------------------

def _ctx_owner_home_with_binding(monkeypatch, binding_target=OWNER_TG):
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        '{"' + OWNER_OWUI + '": "' + binding_target + '"}',
    )


def _run_dual_gate(job, content, *, no_agent=False):
    """Применить живой гейт к telegram-таргетам, которые получил бы
    api_server-джоб после §8.2.2 augmentation (через _augment_owui_telegram_lane).

    Возвращает список (chat_id, allowed_bool) для telegram-таргетов —
    без выполнения отправки (паттерн test_delivery_matrix_t1._run_gate).
    """
    from cron.scheduler import (
        _augment_owui_telegram_lane,
        _get_home_target_chat_id,
        _resolve_delivery_targets,
        _tg_delivery_policy_context,
        _tg_gate_target,
    )

    job = dict(job)
    job["no_agent"] = bool(job.get("no_agent")) or no_agent
    targets = _resolve_delivery_targets(job)
    # authoritative owner из серверного реестра (resolve_origin возврат)
    origin = {
        "platform": "api_server",
        "chat_id": str((job.get("origin") or {}).get("chat_id") or "owui-chat-x"),
        "owner_id": (job.get("origin") or {}).get("user_id") or OWNER_OWUI,
        "user_id": (job.get("origin") or {}).get("user_id") or OWNER_OWUI,
    }
    targets = _augment_owui_telegram_lane(job, origin, targets)
    home = _get_home_target_chat_id("telegram")
    ctx = _tg_delivery_policy_context(
        job, content, [], home_channel=home, allowed_users=ALLOWED,
    )
    out = []
    for target in targets:
        if str(target.get("platform") or "").lower() != "telegram":
            continue
        out.append((str(target["chat_id"]), _tg_gate_target(target, content, ctx)))
    return out


def test_dual_lane_owui_owner_gets_artifact_in_tg(monkeypatch):
    # OWUI-origin джоб с привязкой владельца: артефакт доходит и в OWUI
    # (api_server-лента), и в TG инициатору (dual-lane).
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-1",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-1",
                   "user_id": OWNER_OWUI},
    }
    results = _run_dual_gate(job, ARTIFACT)
    assert results, "dual-lane не добавил telegram-таргет владельцу"
    for chat_id, allowed in results:
        assert chat_id == OWNER_TG
        assert allowed is True


def test_dual_lane_owui_owner_gets_final_report_in_tg(monkeypatch):
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-2",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-2",
                   "user_id": OWNER_OWUI},
    }
    results = _run_dual_gate(job, FINAL_REPORT)
    assert results and all(a is True for _, a in results)


def test_dual_lane_routine_suppressed_even_with_binding(monkeypatch):
    # Рутина/no-delta → TG suppressed независимо от привязки (§8.3).
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-3",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-3",
                   "user_id": OWNER_OWUI},
    }
    results = _run_dual_gate(job, ROUTINE, no_agent=True)
    assert results, "dual-lane добавил TG, но рутина должна пройти гейт"
    for chat_id, allowed in results:
        assert chat_id == OWNER_TG
        assert allowed is False, "рутина не должна попасть в TG даже владельцу"


def test_dual_lane_foreign_owui_suppressed_even_artifact(monkeypatch):
    # Чужой OWUI-пользователь (не в привязке): привязка есть, но не для
    # него → TG-лента не добавляется вовсе (fail-closed, §8.3), ценному
    # контенту чужаку нечего подавлять.
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-4",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-4",
                   "user_id": FOREIGN_OWUI},
    }
    results = _run_dual_gate(job, ARTIFACT)
    assert results == [], \
        "чужой OWUI-пользователь не должен получать TG-ленту вообще"


def test_dual_lane_no_binding_keeps_owui_only(monkeypatch):
    # Без привязки — TG-ленты нет вообще (OWUI-only, V8).
    _ctx_owner_home(monkeypatch)
    monkeypatch.delenv("HERMES_OPENWEBUI_TELEGRAM_USERS", raising=False)
    job = {
        "id": "t2-5",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-5",
                   "user_id": OWNER_OWUI},
    }
    results = _run_dual_gate(job, ARTIFACT)
    assert results == [], "без привязки dual-lane не должен добавлять TG-таргет"


def test_dual_lane_policy_off_keeps_owui_only(monkeypatch):
    # Policy off → augmentation не добавляет TG-ленту (§8.2.2 требует policy on).
    from hermes_cli.delivery_policy import owui_telegram_users

    _ctx_owner_home_with_binding(monkeypatch)
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    job = {
        "id": "t2-6",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-6",
                   "user_id": OWNER_OWUI},
    }
    assert owui_telegram_users()  # привязка на месте
    results = _run_dual_gate(job, ARTIFACT)
    assert results == [], "policy off → без TG-ленты (dual-lane выключен)"


# ---------------------------------------------------------------------------
# §8.1 — привязка только при ALLOWED/home: augmentation-условие
# ---------------------------------------------------------------------------

def test_dual_lane_bound_chat_not_allowed_no_lane(monkeypatch):
    # Привязка вендорит чат вне ALLOWED/home → лента не добавляется.
    _ctx_owner_home_with_binding(monkeypatch, binding_target="888888888")
    job = {
        "id": "t2-7",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-7",
                   "user_id": OWNER_OWUI},
    }
    results = _run_dual_gate(job, ARTIFACT)
    assert results == [], "bound-чат вне ALLOWED/home — TG-ленты быть не должно"


def _ctx_owner_home(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)


# ---------------------------------------------------------------------------
# §8.4 — полный dual-lane: обе ленты одновременно + журнал подавлений без
# лишних записей (acceptance t_812d58c2)
# ---------------------------------------------------------------------------

#: Второй OWUI-пользователь, также ПРИВЯЗАННЫЙ (в отличие от FOREIGN_OWUI).
SECOND_OWUI = "aaaaa111-2222-3333-4444-555555555555"
SECOND_TG = "999000111"


def _read_suppression_journal():
    """Jsonl-записи delivery-policy-suppressed.log в изолированном HERMES_HOME."""
    import json as _json

    from hermes_constants import get_hermes_home

    log_path = get_hermes_home() / "logs" / "delivery-policy-suppressed.log"
    if not log_path.exists():
        return []
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(_json.loads(line))
    return records


def _run_all_targets_gate(job, content, *, no_agent=False):
    """Применить гейт ко ВСЕМ таргетам (включая api_server/OWUI).

    Возвращает список (platform, chat_id, allowed) — в отличие от
    _run_dual_gate, который выкидывает не-telegram таргеты, здесь видна
    полная картина dual-lane: OWUI-лента сохраняется, TG-лента добавляется.
    """
    from cron.scheduler import (
        _augment_owui_telegram_lane,
        _get_home_target_chat_id,
        _resolve_delivery_targets,
        _tg_delivery_policy_context,
        _tg_gate_target,
    )

    job = dict(job)
    job["no_agent"] = bool(job.get("no_agent")) or no_agent
    targets = _resolve_delivery_targets(job)
    origin = {
        "platform": "api_server",
        "chat_id": str((job.get("origin") or {}).get("chat_id") or "owui-chat-x"),
        "owner_id": (job.get("origin") or {}).get("user_id") or OWNER_OWUI,
        "user_id": (job.get("origin") or {}).get("user_id") or OWNER_OWUI,
    }
    targets = _augment_owui_telegram_lane(job, origin, targets)
    home = _get_home_target_chat_id("telegram")
    ctx = _tg_delivery_policy_context(
        job, content, [], home_channel=home, allowed_users=ALLOWED,
    )
    return [
        (str(t.get("platform")), str(t.get("chat_id") or t.get("chat") or ""),
         _tg_gate_target(t, content, ctx))
        for t in targets
    ]


def test_dual_lane_artifact_delivers_to_both_owui_and_telegram(monkeypatch):
    # Приёмка 1: OWUI-origin джоб с артефактом доставляется И в OWUI-чат
    # инициатора (api_server-лента), И в его Telegram (dual-lane) — обе
    # ленты присутствуют и обе проходят гейт.
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-full-1",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-full-1",
                   "user_id": OWNER_OWUI},
    }
    results = _run_all_targets_gate(job, ARTIFACT)
    lanes = {p for p, _, _ in results}
    assert "api_server" in lanes, "OWUI-лента (api_server) должна остаться"
    assert "telegram" in lanes, "dual-lane должен добавить TG-ленту"
    by_platform = {}
    for p, c, allowed in results:
        by_platform.setdefault(p, []).append((c, allowed))
    # Telegram-лента ведёт в чат привязки инициатора и разрешена.
    assert by_platform["telegram"] == [(OWNER_TG, True)]
    # OWUI-лента (не-telegram) гейтом не подавляется.
    assert all(allowed is True for _, allowed in by_platform["api_server"])


def test_dual_lane_second_bound_user_receives_nothing(monkeypatch):
    # Приёмка 2 (isolation): привязка есть у ДВУХ пользователей, но джоб
    # принадлежит owner. Augmentation добавляет TG-ленту ТОЛЬКО для чата
    # привязки owner — второй bound-пользователь ничего не получает.
    _ctx_owner_home_with_binding(monkeypatch)
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        '{"' + OWNER_OWUI + '": "' + OWNER_TG + '", "'
        + SECOND_OWUI + '": "' + SECOND_TG + '"}',
    )
    monkeypatch.setenv(
        "TELEGRAM_ALLOWED_USERS", ",".join([OWNER_TG, SECOND_TG]),
    )
    job = {
        "id": "t2-full-2",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-full-2",
                   "user_id": OWNER_OWUI},
    }
    results = _run_all_targets_gate(job, ARTIFACT)
    tg_chats = [c for p, c, _ in results if p == "telegram"]
    assert tg_chats == [OWNER_TG], (
        "второй bound-пользователь не должен получить TG-ленту; "
        f"фактически: {tg_chats}"
    )
    assert SECOND_TG not in tg_chats
    # И на уровне verify_recipient: таргет=чужой bound-чат отвергается.
    assert verify_recipient(
        InitiatorRef("api_server", "owui-chat-full-2", OWNER_OWUI),
        TargetRef("telegram", SECOND_TG),
        home_channel=OWNER_TG,
        allowed_users=[OWNER_TG, SECOND_TG],
        owui_to_tg={OWNER_OWUI: OWNER_TG, SECOND_OWUI: SECOND_TG},
    ) is False


def test_dual_lane_routine_journal_exactly_one_entry(monkeypatch):
    # Приёмка 3+4: рутина/no-delta подавляется в TG даже при привязке —
    # и журнал подавлений получает РОВНО одну запись (без дублей/лишних
    # строк от guest-лент).
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-full-3",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-full-3",
                   "user_id": OWNER_OWUI},
    }
    results = _run_all_targets_gate(job, ROUTINE, no_agent=True)
    tg_entries = [(c, allowed) for p, c, allowed in results if p == "telegram"]
    assert tg_entries == [(OWNER_TG, False)], "рутина должна быть подавлена в TG"
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["target"] == f"telegram:{OWNER_TG}"
    assert journal[0]["app"] == "cron"


def test_dual_lane_artifact_delivered_journal_has_no_extra_entries(monkeypatch):
    # Приёмка 4: ценный артефакт владельцу доставляется в ОБЕ ленты — журнал
    # подавлений НЕ получает никаких записей (ничего не подавлено).
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-full-4",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-full-4",
                   "user_id": OWNER_OWUI},
    }
    results = _run_all_targets_gate(job, ARTIFACT)
    assert all(allowed is True for _, _, allowed in results), results
    assert _read_suppression_journal() == [], (
        "доставленный артефакт не должен оставлять записей в "
        "delivery-policy-suppressed.log"
    )


def test_dual_lane_foreign_owui_no_lane_no_journal(monkeypatch):
    # Приёмка 4 (negative): чужому OWUI-пользователю TG-лента не добавляется
    # вовсе — журнал подавлений тоже пуст (нет лишних записей о «подавлении»
    # контента, который и не целивался ни в какой TG-чат).
    _ctx_owner_home_with_binding(monkeypatch)
    job = {
        "id": "t2-full-5",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "owui-chat-full-5",
                   "user_id": FOREIGN_OWUI},
    }
    results = _run_all_targets_gate(job, ARTIFACT)
    assert [p for p, _, _ in results if p == "telegram"] == []
    assert _read_suppression_journal() == [], (
        "чужому пользователю нечего подавлять — журнал должен остаться пустым"
    )


# ---------------------------------------------------------------------------
# §8.5 (kanban) — ОWUI→TG dual-lane в канбан-нотификаторе
# (gateway.kanban_watchers._tg_owui_dual_lane, сплит t_9abe0eb6, родитель
# t_e7a60f78).  Тот же RGZ-классификатор (classify + verify_recipient +
# log_suppression), что и §8.2.2 для cron, но на КАНБАН-событиях не-РГЗ
# миссии (api_server-подписка инициатора): B/C/D-артефакт инициатору →
# доставка и в OWUI, и в TG-чат привязки; рутина/статус → suppress + журнал;
# чужой OWUI / чужой initiator → TG-ленты нет вовсе.
# ---------------------------------------------------------------------------

def _owui_sub(owner=OWNER_OWUI, chat="owui-chat-k", task_id="t-k"):
    return {
        "platform": "api_server", "user_id": owner,
        "chat_id": chat, "task_id": task_id,
    }


def _owui_ev(kind="completed", artifacts=None, eid=1, summary="Результат"):
    payload = {"summary": summary}
    if artifacts is not None:
        payload["artifacts"] = artifacts
    return SimpleNamespace(id=eid, kind=kind, payload=payload)


def _owui_task(status="done"):
    return SimpleNamespace(status=status, title="root", result="res")


def _run_owui_lane(sub, ev, msg="Kanban T1 done — Результат", *, task=None,
                   task_is_root=None, home_channel=None, allowed=None):
    from gateway.kanban_watchers import _tg_owui_dual_lane

    return _tg_owui_dual_lane(
        sub, ev, ev.kind, msg, task or _owui_task(),
        list(allowed or ALLOWED),
        task_is_root=task_is_root, home_channel=home_channel or "",
    )


def test_owui_lane_intermediate_completed_child_suppressed_with_journal(monkeypatch):
    # F6: промежуточный completed ДОЧЕРНЕЙ карточки (task_is_root не задан /
    # не корень) с артефактами сплита — НЕ терминальный итог миссии (ИНВ2)
    # → TG-лента SUPPRESS + РОВНО одна запись журнала подавлений
    # (reason=routine status close — прецедент classify, owui_to_tg=True).
    # OWUI-лента (primary) этим не затронута: гейт действует только на TG-зеркало.
    _ctx_owner_home_with_binding(monkeypatch)
    decision, bound_chat = _run_owui_lane(
        _owui_sub(), _owui_ev(kind="completed", artifacts=["rpt.md"]),
        task=_owui_task(status="done"),
    )
    assert decision.suppressed is True, (
        f"промежуточный completed ребёнка должен подавляться: {decision}"
    )
    assert bound_chat == OWNER_TG, f"TG-чат = привязка инициатора: {bound_chat}"
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["reason"] == "routine status close"
    assert journal[0]["app"] == "kanban"
    assert journal[0]["target"] == f"telegram:{OWNER_TG}"
    assert journal[0]["owui_to_tg"] is True
    assert journal[0]["event_kind"] == "completed"
    assert journal[0]["task_id"] == "t-k"


def test_owui_lane_human_action_delivers_to_initiator_tg(monkeypatch):
    # human_action (blocked/needs_input) — доставляется в TG-чат привязки.
    _ctx_owner_home_with_binding(monkeypatch)
    for kind in ("blocked", "needs_input"):
        decision, bound_chat = _run_owui_lane(
            _owui_sub(), _owui_ev(kind=kind),
        )
        assert decision.delivered is True, f"{kind} должен доставляться"
        assert bound_chat == OWNER_TG


def test_owui_lane_root_closing_completed_delivers_to_tg(monkeypatch):
    # Закрытие КОРНЯ ветки (task_is_root=True, completed, без артефакта) —
    # терминально (RF2-паритет) → доставка в TG-чат привязки.
    _ctx_owner_home_with_binding(monkeypatch)
    decision, bound_chat = _run_owui_lane(
        _owui_sub(), _owui_ev(kind="completed", artifacts=None),
        task=_owui_task(status="done"), task_is_root=True,
    )
    assert decision.delivered is True, f"root-closing должен доставляться: {decision}"
    assert bound_chat == OWNER_TG


def test_owui_lane_root_completed_with_final_artifact_delivers(monkeypatch):
    # F6: завершение КОРНЯ миссии (task_is_root=True) с финальным итоговым
    # артефактом — терминальный итог миссии → DELIVER (OWUI+TG), журнал пуст.
    _ctx_owner_home_with_binding(monkeypatch)
    decision, bound_chat = _run_owui_lane(
        _owui_sub(), _owui_ev(kind="completed", artifacts=["final-report.pdf"]),
        task=_owui_task(status="done"), task_is_root=True,
    )
    assert decision.delivered is True, (
        f"итог корня с финальным артефактом должен доставляться: {decision}"
    )
    assert bound_chat == OWNER_TG
    assert _read_suppression_journal() == [], (
        "доставленный терминальный итог не должен писать в журнал подавлений"
    )


def test_owui_lane_archived_completed_terminal_delivers(monkeypatch):
    # F6: completed дочерней карточки с task.status==archived (task_terminal) —
    # терминален и в TG проходит (опора completed_terminal), журнал пуст.
    _ctx_owner_home_with_binding(monkeypatch)
    decision, bound_chat = _run_owui_lane(
        _owui_sub(), _owui_ev(kind="completed", artifacts=["arch.yml"]),
        task=_owui_task(status="archived"),
    )
    assert decision.delivered is True, (
        f"archived-completed терминален и должен доставляться: {decision}"
    )
    assert bound_chat == OWNER_TG
    assert _read_suppression_journal() == []


def test_owui_lane_policy_off_passthrough(monkeypatch):
    # F6: policy off → TG-лента не добавляется вовсе (passthrough/OWUI-only),
    # журнал пуст — даже для completed, который при policy on доставлялся бы.
    _ctx_owner_home_with_binding(monkeypatch)
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    for kwargs in (
        {"ev_kind": "completed", "artifacts": ["rpt.md"], "task_is_root": None},
        {"ev_kind": "completed", "artifacts": ["final-report.pdf"],
         "task_is_root": True},
    ):
        out = _run_owui_lane(
            _owui_sub(),
            _owui_ev(kind=kwargs["ev_kind"], artifacts=kwargs["artifacts"]),
            task=_owui_task(status="done"),
            task_is_root=kwargs["task_is_root"],
        )
        assert out is None, f"policy off → OWUI-only (no TG lane): {out}"
    assert _read_suppression_journal() == []


def test_owui_lane_routine_status_suppressed_with_journal(monkeypatch):
    # Рутина/статус-флип: даже при валидной привязке → suppress + РОВНО одна
    # запись в журнале с owui_to_tg=True (как в cron-T2 §8.2.2).
    _ctx_owner_home_with_binding(monkeypatch)
    decision, bound_chat = _run_owui_lane(
        _owui_sub(), _owui_ev(kind="status"),
    )
    assert decision.suppressed is True, f"статус должен подавляться: {decision}"
    assert bound_chat == OWNER_TG
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["target"] == f"telegram:{OWNER_TG}"
    assert journal[0]["app"] == "kanban"
    assert journal[0]["owui_to_tg"] is True


def test_owui_lane_foreign_owui_never_gets_tg_lane(monkeypatch):
    # Чужой OWUI-пользователь (не в привязке): TG-лента не добавляется вовсе,
    # журнал пуст — ценный артефакт чужаку отправлять некуда (fail-closed).
    _ctx_owner_home_with_binding(monkeypatch)
    out = _run_owui_lane(
        _owui_sub(owner=FOREIGN_OWUI),
        _owui_ev(kind="completed", artifacts=["rpt.md"]),
    )
    assert out is None, f"чужой OWUI не должен получить TG-ленту: {out}"
    assert _read_suppression_journal() == []


def test_owui_lane_structural_isolation_and_fail_closed(monkeypatch):
    # (1) Структурная изоляция: миссия владельца A никогда не уходит в чат
    # привязки другого bound-пользователя (owner→chat — эксклюзивно).
    _ctx_owner_home_with_binding(monkeypatch)
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        '{"' + OWNER_OWUI + '": "' + OWNER_TG + '", "'
        + SECOND_OWUI + '": "' + SECOND_TG + '"}',
    )
    monkeypatch.setenv(
        "TELEGRAM_ALLOWED_USERS", ",".join([OWNER_TG, SECOND_TG]),
    )
    decision, bound_chat = _run_owui_lane(
        _owui_sub(owner=OWNER_OWUI),
        _owui_ev(kind="completed", artifacts=["rpt.md"]),
        task=_owui_task(status="done"), task_is_root=True,
    )
    assert decision.delivered is True
    assert bound_chat == OWNER_TG
    assert bound_chat != SECOND_TG
    # (2) Fail-closed: без привязки у владельца — TG-ленты нет вообще (OWUI-only).
    monkeypatch.delenv("HERMES_OPENWEBUI_TELEGRAM_USERS", raising=False)
    assert _run_owui_lane(
        _owui_sub(owner=OWNER_OWUI),
        _owui_ev(kind="completed", artifacts=["rpt.md"]),
    ) is None

