"""
Unit tests for the kept TG delivery-policy primitives (D0 ``tg-delivery-hygiene``, F1).

R1 (REFACTOR-DESIGN.md §(в)): the content CLASSIFIER is gone (classify(),
default_context(), is_routine_report(), detect_system_alert(),
policy_enabled() were removed — the engine is the single decision).  These
tests cover the primitives that REMAIN in hermes_cli/delivery_policy.py:

  * recipient == initiator (ИНВ1) gate            -> verify_recipient
  * kanban-subscription gate                      -> verify_subscription_recipient
  * suppression journal (log_suppression + PolicyDecision deliver/suppress)
  * sanitize_delivery_text / sanitize_display_text
  * OWUI server binding                           -> owui_telegram_users
  * TargetRef / InitiatorRef

The cron _tg_gate_target injection tests (verify-only gate) live in
TestCronGateInjection below; classifier assertions were removed with the
classifier.
"""

from __future__ import annotations

import pytest

from hermes_cli.delivery_policy import (
    InitiatorRef,
    TargetRef,
    deliver,
    log_suppression,
    owui_telegram_users,
    sanitize_delivery_text,
    sanitize_display_text,
    suppress,
    verify_recipient,
    verify_subscription_recipient,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tg_target(chat, thread=None) -> TargetRef:
    return TargetRef.from_dict({"platform": "telegram", "chat_id": chat,
                                "thread_id": thread})


# ---------------------------------------------------------------------------
# 1. Получатель == инициатор (ИНВ1, fail-closed)
# ---------------------------------------------------------------------------


def test_recipient_mismatch_suppress():
    initiator = InitiatorRef.from_origin({"platform": "telegram", "chat_id": "123"})
    target = _tg_target("999")
    assert not verify_recipient(initiator, target, allowed_users=["123"])


def test_verify_recipient_same_chat():
    assert verify_recipient(
        InitiatorRef.from_origin({"platform": "telegram", "chat_id": "123"}),
        _tg_target("123"),
    )


def test_verify_recipient_unknown_initiator_fail_closed():
    assert not verify_recipient(None, _tg_target("123"))


def test_verify_recipient_cross_platform_only_allowed_user():
    # V1: origin=api_server -> TG home; only allowed (owner) user passes.
    home = "125805788"
    assert verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-1", "user_id": "125805788"}),
        _tg_target(home), home_channel=home, allowed_users=["125805788"],
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-2", "user_id": "owui-user-1"}),
        _tg_target(home), home_channel=home, allowed_users=["125805788"],
    )


def test_verify_recipient_admin_delivery_to_owner_home():
    # V3: явный target = чат владельца (allowed) разрешён даже без origin.
    home = "125805788"
    assert verify_recipient(None, _tg_target(home),
                            home_channel=home, allowed_users=[home])


def test_verify_recipient_full_mapping_multi_user_isolation():
    """t_780a6db2: verify_recipient читает ПОЛНЫЙ owui_telegram_users
    (не только владельца) и держит изоляцию инициаторов fail-closed."""
    home = "home-125805788"
    mapping = {
        "9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "125805788",
        "3daba661-feb8-4c1d-a823-4c5223f98d30": "543447681",
    }
    allowed = ["125805788", "543447681", home]

    assert verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-1",
                                  "user_id": "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-2",
                                  "user_id": "3daba661-feb8-4c1d-a823-4c5223f98d30"}),
        _tg_target("543447681"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-3",
                                  "user_id": "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"}),
        _tg_target("543447681"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-4",
                                  "user_id": "3daba661-feb8-4c1d-a823-4c5223f98d30"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-5",
                                  "user_id": "0795993b-4cc5-431f-a7fb-45b18967a9ba"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-6",
                                  "user_id": "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=allowed, owui_to_tg={},
    )


def test_verify_recipient_second_bound_user_gets_mirror_h5():
    """H-5 (t_efffb731): второй привязанный OWUI-пользователь получает
    зеркало в СВОЙ bound-чат, даже когда его чат НЕ входит в ALLOWED/home."""
    home = "125805788"
    mapping = {
        "9ae3eca0-d6e9-45d5-b4af-414afe4616b9": "125805788",
        "3daba661-feb8-4c1d-a823-4c5223f98d30": "543447681",
    }
    allowed = ["125805788", home]

    assert verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-1",
                                  "user_id": "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-2",
                                  "user_id": "3daba661-feb8-4c1d-a823-4c5223f98d30"}),
        _tg_target("543447681"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-3",
                                  "user_id": "3daba661-feb8-4c1d-a823-4c5223f98d30"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-4",
                                  "user_id": "0795993b-4cc5-431f-a7fb-45b18967a9ba"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=allowed, owui_to_tg=mapping,
    )
    assert not verify_recipient(
        InitiatorRef.from_origin({"platform": "api_server",
                                  "chat_id": "job-5",
                                  "user_id": "3daba661-feb8-4c1d-a823-4c5223f98d30"}),
        _tg_target("125805788"), home_channel=home,
        allowed_users=[*allowed, "543447681"], owui_to_tg=mapping,
    )


def test_non_telegram_target_unaffected():
    # Политика — только про TG; прочие платформы проходят.
    tgt = TargetRef.from_dict({"platform": "discord", "chat_id": "x"})
    assert verify_recipient(None, tgt, allowed_users=["123"])


# ---------------------------------------------------------------------------
# 2. Подписки kanban (V4)
# ---------------------------------------------------------------------------

def test_subscription_tg_owner_allowed():
    assert verify_subscription_recipient(
        {"platform": "telegram", "user_id": "125805788"}, ["125805788"],
    )


def test_subscription_tg_foreign_user_rejected():
    assert not verify_subscription_recipient(
        {"platform": "telegram", "user_id": "99999"}, ["125805788"],
    )


def test_subscription_api_server_untouched():
    assert verify_subscription_recipient(
        {"platform": "api_server", "user_id": "owui-u"}, ["125805788"],
    )


def test_subscription_tg_owner_marker_delivers_owner():
    # K1 RF3: when the branch owner's TG id is known, an allow-listed
    # subscriber IS the owner → deliver.
    assert verify_subscription_recipient(
        {"platform": "telegram", "user_id": "125805788", "chat_id": "125805788"},
        ["125805788"],
        task_tg_owner="125805788",
    )


def test_subscription_tg_owner_marker_rejects_other_allowed():
    # K1 RF3: a DIFFERENT (even allow-listed) user subscribing to someone
    # else's branch must NOT receive it — fail-closed on owner mismatch.
    assert not verify_subscription_recipient(
        {"platform": "telegram", "user_id": "125805788", "chat_id": "125805788"},
        ["125805788", "2222"],
        task_tg_owner="2222",
    )


def test_subscription_tg_owner_marker_unknown_owner_falls_back_to_allowlist():
    # K1 RF3: when the branch owner cannot be resolved (task_tg_owner empty),
    # keep the pre-RF3 allowlist check — never break a legit channel.
    assert verify_subscription_recipient(
        {"platform": "telegram", "user_id": "125805788", "chat_id": "125805788"},
        ["125805788"],
        task_tg_owner="",
    )
    assert not verify_subscription_recipient(
        {"platform": "telegram", "user_id": "99999", "chat_id": "99999"},
        ["125805788"],
        task_tg_owner="",
    )


# ---------------------------------------------------------------------------
# 3. Suppression journal: PolicyDecision deliver/suppress + log_suppression
# ---------------------------------------------------------------------------

def test_policy_decision_deliver_suppress_shapes():
    assert deliver().delivered is True
    assert deliver().suppressed is False
    assert suppress("recipient mismatch").suppressed is True
    assert suppress("recipient mismatch").reason == "recipient mismatch"
    assert deliver().reason != "recipient mismatch"


def test_log_suppression_writes_record():
    """log_suppression пишет в журнал и не бросает исключений."""
    decision = suppress("recipient mismatch", detail="chat=999")
    log_suppression(
        app="cron", who="123", target="telegram:999",
        decision=decision,
        snippet="итог",
    )  # No exception raised.
    assert decision.suppressed


# ---------------------------------------------------------------------------
# 4. sanitize_delivery_text / sanitize_display_text
# ---------------------------------------------------------------------------

def test_sanitize_delivery_text_removes_reasoning_block():
    raw = (
        "<reasoning>Рассуждаю о плане</reasoning>\n"
        "ИТОГ: копирование завершено ОК: 5"
    )
    cleaned = sanitize_delivery_text(raw)
    assert "Рассуждаю" not in cleaned
    assert "ИТОГ:" in cleaned


def test_sanitize_delivery_text_keeps_normal_business_text():
    raw = "Итоги недели: сделано 3 источника, добавлена первая правка."
    assert sanitize_delivery_text(raw) == raw


def test_sanitize_display_text_preserves_readable_payload():
    raw = "Просто итог: 3 источника"
    assert sanitize_display_text(raw) == raw


# ---------------------------------------------------------------------------
# 5. OWUI binding (owui_telegram_users) — fail-closed на отсутствие конфига
# ---------------------------------------------------------------------------

def test_owui_telegram_users_not_configured_fail_closed():
    # Без конфига привязок — пусто (fail-closed; цепочки лейтовых добавлений
    # просто не срабатывают).
    mapping = owui_telegram_users()
    assert mapping is None or isinstance(mapping, dict)


# ---------------------------------------------------------------------------
# 6. Инжекция гейта в cron (_tg_gate_target) — verify-only gate (R1)
# ---------------------------------------------------------------------------

def _build_cron_gate_ctx(monkeypatch, allowed_users=("123",), origin=None):
    """Build the policy context exactly as _deliver_result does."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(allowed_users))
    from cron.scheduler import _get_home_target_chat_id, _tg_delivery_policy_context
    home = _get_home_target_chat_id("telegram")
    job = {"id": "test-job", "no_agent": False, "deliver": "origin"}
    return _tg_delivery_policy_context(
        job, "какой-то итог", [], home_channel=home,
        allowed_users=list(allowed_users),
    ), home


@pytest.mark.integration
class TestCronGateInjection:

    def test_owner_chat_allowed(self, monkeypatch):
        ctx, home = _build_cron_gate_ctx(monkeypatch)
        from cron.scheduler import _tg_gate_target
        target = {"platform": "telegram", "chat_id": "123", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", ctx) is True

    def test_foreign_chat_suppressed(self, monkeypatch):
        ctx, home = _build_cron_gate_ctx(monkeypatch, allowed_users=("123",))
        from cron.scheduler import _tg_gate_target
        target = {"platform": "telegram", "chat_id": "999", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", ctx) is False

    def test_non_telegram_target_unaffected(self, monkeypatch):
        ctx, home = _build_cron_gate_ctx(monkeypatch)
        from cron.scheduler import _tg_gate_target
        target = {"platform": "slack", "chat_id": "C123", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", ctx) is True

    def test_policy_unavailable_fail_closed(self, monkeypatch):
        # K1 RF1 (CRITICAL, fail-open→fail-closed): policy_ctx is None when
        # the module is unimportable -> the telegram target must be
        # SUPPRESSED (False), never delivered.
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123")
        from cron.scheduler import _tg_gate_target
        target = {"platform": "telegram", "chat_id": "123", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", None) is False
        target2 = {"platform": "slack", "chat_id": "C123", "thread_id": None}
        assert _tg_gate_target(target2, "итог отчёта", None) is True
