"""
Unit tests for the TG delivery-policy classifier (D0 ``tg-delivery-hygiene``, F1).

Covers the core T1 cases from ``research/delivery-matrix.md`` / the F1
acceptance list:
  * artifacts / MEDIA: / file paths           -> deliver
  * [ACTION-REQUIRED] / blocked / needs_input -> deliver
  * system alerts (blocked_config / drift / cron failure) -> deliver
  * completed-pings / status-flips / routine no-delta -> suppress + journal
  * recipient == initiator (ИНВ1)             -> deliver; mismatch -> suppress
  * config flag off                           -> gate inert
  * empty content                             -> suppress
These tests exercise the pure module (hermes_cli/delivery_policy.py) — no
gateway/cron import needed, so they run fast and deterministically.
"""

from __future__ import annotations

import pytest

from hermes_cli.delivery_policy import (
    InitiatorRef,
    TargetRef,
    classify,
    default_context,
    is_routine_report,
    policy_enabled,
    verify_recipient,
    verify_subscription_recipient,
    deliver,
    suppress,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cron_ctx(no_agent=False, has_media=False, artifacts=None,
              completed_terminal=True, initiator=None, target=None,
              home_channel=None, allowed_users=None):
    return default_context(
        kind="cron",
        initiator=initiator,
        target=target,
        home_channel=home_channel,
        allowed_users=allowed_users,
        no_agent=no_agent,
        completed_terminal=completed_terminal,
        has_media=has_media,
        artifacts=artifacts or [],
    )


def _kanban_ctx(event_kind, completed_terminal=False, artifacts=None, summary="",
                initiator=None, target=None, allowed_users=None):
    return default_context(
        kind="kanban",
        event_kind=event_kind,
        completed_terminal=completed_terminal,
        artifacts=artifacts or [],
        initiator=initiator,
        target=target,
        allowed_users=allowed_users,
        no_agent=False,
    )


def _tg_target(chat, thread=None) -> TargetRef:
    return TargetRef.from_dict({"platform": "telegram", "chat_id": chat,
                                "thread_id": thread})


# ---------------------------------------------------------------------------
# 1. Ценные классы — проходят (ИНВ2)
# ---------------------------------------------------------------------------


def test_artifacts_media_always_deliver():
    ctx = _cron_ctx(has_media=True)
    assert classify("У тебя файлик", ctx).delivered
    # MEDIA: marker in content
    ctx2 = _cron_ctx()
    assert classify("вот: MEDIA: C:/tmp/report.pdf\nитог", ctx2).delivered


def test_action_required_kinds_deliver():
    for kind in ("blocked", "needs_input", "capability", "review_requested",
                 "crashed", "gave_up"):
        ctx = _kanban_ctx(kind)
        assert classify("нужен человек", ctx).delivered, kind


def test_system_alerts_deliver_even_no_agent_routine():
    # blocked_config / drift / cron failure MUST not be swallowed by the
    # routine heuristic (alert-once contract).
    for text in ("[blocked_config] конфиг невалиден", "⛔ Cron blocked...",
                 "⚠️ Cron skipped", "cron job failed after 3 tries"):
        ctx = _cron_ctx(no_agent=True)
        assert classify(text, ctx).delivered, text


def test_final_report_deliver_with_verify():
    initiator = InitiatorRef.from_origin({"platform": "telegram", "chat_id": "123"})
    ctx = _cron_ctx(initiator=initiator, target=_tg_target("123"),
                    allowed_users=["123"])
    assert classify("Итоги недели: сделано Х", ctx).delivered


# ---------------------------------------------------------------------------
# 2. Подавляется — рутина / статус-пинги (ИНВ2)
# ---------------------------------------------------------------------------

def test_empty_content_suppress():
    assert classify("", _cron_ctx()).suppressed
    assert classify("   ", _cron_ctx()).suppressed


def test_completed_ping_without_summary_suppress():
    ctx = _kanban_ctx("completed", completed_terminal=False)
    assert classify("Task t_123 completed", ctx).suppressed


def test_status_flip_suppress():
    ctx = _kanban_ctx("status", completed_terminal=False)
    assert classify("Task moved to in_progress", ctx).suppressed


def test_routine_no_delta_report_suppress():
    ctx = _cron_ctx(no_agent=True)
    assert classify('{"copied": 0, "missing_in_kb": 0, "errors": 0}', ctx).suppressed
    ctx2 = _cron_ctx(no_agent=True)
    assert classify("copied: 0\nskipped: 2", ctx2).suppressed


def test_agent_report_not_suppressed_by_routine_heuristic():
    # no_agent=False → the low-delta heuristic does NOT apply to LLM reports.
    ctx = _cron_ctx(no_agent=False)
    assert classify("here is the copied analysis", ctx).delivered


# ---------------------------------------------------------------------------
# 3. Получатель == инициатор (ИНВ1, fail-closed)
# ---------------------------------------------------------------------------

def _target_gate_allows(content, ctx, initiator=None, target=None,
                        home_channel=None, allowed_users=None) -> bool:
    """Two-stage gate as wired in _tg_gate_target (cron) / notifier (V4):
    classify (content) then verify_recipient (recipient).  Returns True only
    when BOTH allow delivery."""
    decision = classify(content, ctx)
    if decision.suppressed:
        return False
    return verify_recipient(initiator, target, home_channel, allowed_users)


def test_recipient_mismatch_suppress():
    initiator = InitiatorRef.from_origin({"platform": "telegram", "chat_id": "123"})
    target = _tg_target("999")
    # final report passes the content classifier, but the recipient gate
    # rejects the foreign chat -> overall suppression (ИНВ1).
    ctx = _cron_ctx(initiator=initiator, target=target,
                    allowed_users=["123"], home_channel=None)
    assert not _target_gate_allows("секретный отчёт", ctx,
                                   initiator=initiator, target=target,
                                   allowed_users=["123"])


def test_final_report_allowed_to_owner():
    initiator = InitiatorRef.from_origin({"platform": "telegram", "chat_id": "123"})
    target = _tg_target("123")
    ctx = _cron_ctx(initiator=initiator, target=target, allowed_users=["123"])
    assert _target_gate_allows("Итоги недели: сделано X", ctx,
                               initiator=initiator, target=target,
                               allowed_users=["123"])


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


def test_non_telegram_target_unaffected():
    # Политика — только про TG; прочие платформы проходят.
    tgt = TargetRef.from_dict({"platform": "discord", "chat_id": "x"})
    assert verify_recipient(None, tgt, allowed_users=["123"])


# ---------------------------------------------------------------------------
# 4. Подписки kanban (V4)
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
# 5. Конфиг-флаг
# ---------------------------------------------------------------------------

def test_policy_flag_default_on(monkeypatch):
    monkeypatch.delenv("HERMES_TELEGRAM_DELIVERY_POLICY", raising=False)
    assert policy_enabled()


def test_policy_flag_off(monkeypatch):
    monkeypatch.delenv("HERMES_TELEGRAM_DELIVERY_POLICY", raising=False)
    assert not policy_enabled({"telegram_delivery_policy": "off"})
    assert not policy_enabled({"telegram_delivery_policy": "OFF"})


def test_policy_flag_on(monkeypatch):
    monkeypatch.delenv("HERMES_TELEGRAM_DELIVERY_POLICY", raising=False)
    assert policy_enabled({"telegram_delivery_policy": "on"})
    assert policy_enabled({"telegram_delivery_policy": "yes"})


def test_policy_flag_yaml_bool_off(monkeypatch):
    # YAML 1.1 parses an UNQUOTED ``off`` to boolean False (and ``on`` to
    # True) — the gate must honour that so ``telegram_delivery_policy: off``
    # in config.yaml really disables it (regression, found in split 1/2).
    monkeypatch.delenv("HERMES_TELEGRAM_DELIVERY_POLICY", raising=False)
    assert not policy_enabled({"telegram_delivery_policy": False})
    assert policy_enabled({"telegram_delivery_policy": True})


def test_policy_flag_env_override(monkeypatch):
    # Env wins over cfg (tests / operators flip it deterministically).
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    assert not policy_enabled({"telegram_delivery_policy": "on"})


# ---------------------------------------------------------------------------
# 6. is_routine_report
# ---------------------------------------------------------------------------

def test_is_routine_report_cases():
    assert is_routine_report('{"copied": 0, "missing_in_kb": 0, "errors": 0}')
    assert is_routine_report("copied: 0\nnothing to do")
    assert is_routine_report("похоже в норме")
    assert not is_routine_report('{"copied": 5, "missing_in_kb": 0, "errors": 0}')
    assert not is_routine_report("Найдено 3 новых источника, добавлены в KB")


# ---------------------------------------------------------------------------
# 7. classify значение по умолчанию (default → verify, иначе suppress)
# ---------------------------------------------------------------------------

def test_default_deliver_when_verify_ok():
    initiator = InitiatorRef.from_origin({"platform": "telegram", "chat_id": "123"})
    ctx = _cron_ctx(initiator=initiator, target=_tg_target("123"),
                    allowed_users=["123"])
    # generic cron final content — falls through to default+verify
    assert classify("просто итог", ctx).delivered


def test_default_suppress_when_verify_fail():
    initiator = InitiatorRef.from_origin({"platform": "telegram", "chat_id": "1"})
    target = _tg_target("2")
    # content classifier passes it (final report class в cron), but the
    # recipient gate rejects chat 2 -> overall suppression (ИНВ1).
    ctx = _cron_ctx(initiator=initiator, target=target)
    assert not _target_gate_allows("какой-то текст", ctx,
                                   initiator=initiator, target=target)


# ---------------------------------------------------------------------------
# 8. Инжекция гейта в cron (_tg_gate_target) — политика ON
# ---------------------------------------------------------------------------

def _build_cron_gate_ctx(monkeypatch, allowed_users=("123",), origin=None):
    """Build the policy context exactly as _deliver_result does, with the
    policy flag forced ON and a telegram owner-chat env."""
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
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
        # owner chat in allowed_users is 123; target 999 is foreign.
        target = {"platform": "telegram", "chat_id": "999", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", ctx) is False

    def test_non_telegram_target_unaffected(self, monkeypatch):
        ctx, home = _build_cron_gate_ctx(monkeypatch)
        from cron.scheduler import _tg_gate_target
        target = {"platform": "slack", "chat_id": "C123", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", ctx) is True

    def test_policy_unavailable_fail_closed(self, monkeypatch):
        # K1 RF1 (CRITICAL, fail-open→fail-closed): policy_ctx is None when
        # the module is unimportable or policy_enabled() raised -> the
        # telegram target must be SUPPRESSED (False), never delivered.
        monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123")
        from cron.scheduler import _tg_gate_target
        target = {"platform": "telegram", "chat_id": "123", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", None) is False
        # non-telegram target stays untouched even when policy is unavailable
        target2 = {"platform": "slack", "chat_id": "C123", "thread_id": None}
        assert _tg_gate_target(target2, "итог отчёта", None) is True

    def test_policy_disabled_gate_off_historically_allowed(self, monkeypatch):
        # Gate OFF ({"policy_disabled": True}) is the operator-chosen path:
        # delivery proceeds as before — NOT a fail-open.
        monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
        from cron.scheduler import _tg_gate_target
        target = {"platform": "telegram", "chat_id": "123", "thread_id": None}
        assert _tg_gate_target(target, "итог отчёта", {"policy_disabled": True}) is True
