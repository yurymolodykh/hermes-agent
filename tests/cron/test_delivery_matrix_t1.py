"""
T1 (РГЗ/tghyg): test-matrix of the TG delivery policy + negative isolation tests.

R1 regime (REFACTOR-DESIGN.md §(в)): the content CLASSIFIER is gone — the
cron ``_tg_gate_target`` is a RECIPIENT-VERIFY-ONLY gate (ИНВ1), and content
typing moved to the delivery engine.  These cells exercise the LIVE cron gate
via ``cron/scheduler._tg_delivery_policy_context`` + ``_tg_gate_target`` the
way ``_deliver_result`` does.  No real Telegram sendMessage is performed —
"delivery" surfaces are stubbed; every cell is deterministic.

Matrix axes (kept cells):

  * origin        : telegram (owner 125805788) | api_server OWUI (foreign ids)
                    | pwa | none (V3 hard-coded admin target)
  * deliver       : origin | telegram | telegram:<chat_id> | all | local
  * target        : initiator | foreign chat_id | home TG | second OWUI user

Removed by R1 (were testing deleted content-heuristics / split-1 notifier):
  * content-type dimension (classify: artifacts/final/action-required/routine)
  * kanban-notifier cells (``_tg_notify_policy_decision`` — now the engine)
  * policy-flag cells (``policy_enabled`` — single engine, no off flag)
"""
from __future__ import annotations

import os

import pytest

# Home of the real policy env (testing live code paths, not a reimplementation).
from hermes_cli.delivery_policy import (
    InitiatorRef,
    TargetRef,
    verify_recipient,
)

OWNER_TG = "125805788"
ALLOWED = [OWNER_TG]
OWNER_TG_ORIGIN = {"platform": "telegram", "chat_id": OWNER_TG, "user_id": OWNER_TG}
FOREIGN_TG = "999999999"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ctx_owner_home(monkeypatch, allowed=None):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(allowed or ALLOWED))
    # Home channel must resolve so deliver=all / deliver=telegram target cells
    # actually produce a telegram target.  OWNER_TG is the live owner home.
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    return OWNER_TG


def _run_gate(job, content, *, artifacts=(), no_agent=False):
    """Apply the LIVE cron gate (_tg_gate_target) to every telegram target.

    Returns a list of (chat_id, allowed_bool) for each telegram target the job
    would resolve to — exactly what _deliver_result does per target before any
    send. The send itself is NOT performed here.
    """
    from cron.scheduler import (
        _get_home_target_chat_id,
        _resolve_delivery_targets,
        _tg_delivery_policy_context,
        _tg_gate_target,
    )

    job = dict(job)
    job["no_agent"] = bool(job.get("no_agent")) or no_agent
    home = _get_home_target_chat_id("telegram")
    ctx = _tg_delivery_policy_context(
        job, content, list(artifacts), home_channel=home,
        allowed_users=ALLOWED,
    )
    out = []
    for target in _resolve_delivery_targets(job):
        if str(target.get("platform") or "").lower() != "telegram":
            continue
        out.append((str(target["chat_id"]), _tg_gate_target(target, content, ctx)))
    return out


def _assert_delivers(job, content, *, chat=OWNER_TG, **kw):
    results = _run_gate(job, content, **kw)
    assert results, f"no telegram target resolved for job {job!r}"
    for chat_id, allowed in results:
        assert allowed is True, (
            f"expected DELIVER to {chat_id}, gate suppressed: {results}"
        )


def _assert_suppressed(job, content, *, chat_any=False, **kw):
    results = _run_gate(job, content, **kw)
    if chat_any:
        assert results, f"no telegram target resolved for job {job!r}"
    for chat_id, allowed in results:
        assert allowed is False, (
            f"expected SUPPRESS to {chat_id}, gate allowed: {results}"
        )


# ---------------------------------------------------------------------------
# NEGATIVE RECIPIENT GATE: НИЧЕГО не уходит чужим / не-инициатору
# ---------------------------------------------------------------------------

class TestNegativeOthersNeverReceive:
    """Fail-closed: foreign chat_id / second OWUI user / cross-platform strangers."""

    def test_foreign_chat_never_receives(self, monkeypatch):
        # deliver=telegram:<foreign> with any content — must NOT land.
        _ctx_owner_home(monkeypatch)
        job = {"id": "j1", "deliver": f"telegram:{FOREIGN_TG}", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, "полный отчёт приложен", chat_any=True)

    def test_foreign_chat_never_receives_content(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "j2", "deliver": f"telegram:{FOREIGN_TG}", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, "Итоговый отчёт периода: закрыто 12 задач", chat_any=True)

    def test_owner_fanout_all_does_not_leave_home_stack(self, monkeypatch):
        # deliver=all with a telegram-origin owner job: only home = owner chat
        # resolves; the fanout must not reach any foreign chat.
        _ctx_owner_home(monkeypatch)
        job = {"id": "j4", "deliver": "all", "origin": OWNER_TG_ORIGIN}
        results = _run_gate(job, "Итоговый отчёт периода")
        assert results, "deliver=all resolved no telegram target"
        for chat_id, allowed in results:
            assert chat_id in ALLOWED, f"deliver=all reached foreign chat {chat_id}"
            assert allowed is True, f"owner home not delivered: {chat_id}"

    def test_cross_platform_foreign_owui_never_reaches_home(self, monkeypatch):
        # V1: foreign OWUI user (3daba661…) schedules via api_server; job must
        # NOT fan out to the owner's home TG.
        _ctx_owner_home(monkeypatch)
        job = {
            "id": "j5",
            "deliver": "telegram",  # deliver=telegram -> home TG
            "origin": {
                "platform": "api_server",
                "chat_id": "owui-chat-1",
                "user_id": "3daba661-feb8-4c1d-a823-4c5223f98d30",
            },
        }
        _assert_suppressed(job, "полный отчёт приложен", chat_any=True)

    def test_cross_platform_second_foreign_owui_never_reaches_home(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {
            "id": "j6",
            "deliver": "telegram",
            "origin": {
                "platform": "api_server",
                "chat_id": "owui-chat-2",
                "user_id": "9ae3eca0-d6e9-45d5-b4af-414afe4616b9",
            },
        }
        _assert_suppressed(job, "полный отчёт приложен", chat_any=True)

    def test_cross_platform_foreign_all_fanout_suppressed(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {
            "id": "j7",
            "deliver": "all",
            "origin": {
                "platform": "api_server",
                "chat_id": "owui-chat-3",
                "user_id": "3daba661-feb8-4c1d-a823-4c5223f98d30",
            },
        }
        _assert_suppressed(job, "Итоговый отчёт периода", chat_any=True)

    def test_cross_platform_pwa_never_reaches_home(self, monkeypatch):
        # yadisk_bridge/PWA channel (gateway/origin_outbox.py:27 lists pwa).
        _ctx_owner_home(monkeypatch)
        job = {
            "id": "j8",
            "deliver": "telegram",
            "origin": {"platform": "pwa", "chat_id": "yadisk-dm-1", "user_id": "srv"},
        }
        _assert_suppressed(job, "Итоговый отчёт периода", chat_any=True)

    def test_unknown_origin_admin_not_foreign(self, monkeypatch):
        # V3: no origin recorded but target is the owner's hard-coded chat:
        # admin delivery allowed ONLY to that chat; a foreign target without
        # any known initiator must still be suppressed.
        _ctx_owner_home(monkeypatch)
        job = {"id": "j9", "deliver": f"telegram:{FOREIGN_TG}"}
        _assert_suppressed(job, "Итоговый отчёт периода", chat_any=True)


# ---------------------------------------------------------------------------
# POSITIVE INITIATOR: доходит ИНИЦИАТОРУ (в TG) — R1 verify-only gate
# ---------------------------------------------------------------------------

class TestPositiveInitiatorDelivers:

    def test_owner_origin_delivers(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "p1", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, "Итоговый отчёт периода: закрыто 12 задач")

    def test_owner_origin_action_required_delivers(self, monkeypatch):
        # R1: content classification is gone; recipient verification decides.
        _ctx_owner_home(monkeypatch)
        job = {"id": "p3", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, "[ACTION-REQUIRED] Владелец, выберите провайдера.")

    def test_owner_home_target_allowlist_admin(self, monkeypatch):
        # V3 allowlist: no-origin admin job pointed at owner chat.
        _ctx_owner_home(monkeypatch)
        job = {"id": "p4", "deliver": f"telegram:{OWNER_TG}"}
        _assert_delivers(job, "Итоговый отчёт периода", chat=OWNER_TG)

    def test_deliver_local_no_outbound(self, monkeypatch):
        # local jobs are the empty-set of delivered targets — by construction.
        _ctx_owner_home(monkeypatch)
        job = {"id": "p5", "deliver": "local", "origin": OWNER_TG_ORIGIN}
        assert _run_gate(job, "Итоговый отчёт периода") == []


# ---------------------------------------------------------------------------
# RECIPIENT GATE STILL APPLIES TO ALL CONTENT (verify-only, NOT content-based)
# ---------------------------------------------------------------------------

class TestRecipientGateAppliesToAnyContent:
    """R1: the gate decides by RECIPIENT, not content.  Owner gets it all
    (they initiated); a foreign chat gets nothing even for "valuable" content.

    Content-level suppression (routine no-delta etc.) moved OUT of the cron
    gate into the delivery engine's structural event typing — see
    engine.classify_event (gateway/delivery_engine.py)."""

    def test_owner_gets_routine_like_content(self, monkeypatch):
        # Routine-shaped stdout is NOT content-suppressed any more; it reaches
        # the owner because the recipient verifies (R1: no content classifier).
        _ctx_owner_home(monkeypatch)
        job = {"id": "s3", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, '{"copied": 0, "missing_in_kb": 0, "errors": 0}')

    def test_foreign_never_gets_routine_like_content(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "s5", "deliver": f"telegram:{FOREIGN_TG}", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, '{"copied": 0, "missing_in_kb": 0, "errors": 0}',
                           chat_any=True)

    def test_empty_content_no_target_emitted(self, monkeypatch):
        # Empty content still yields no deliverable telegram target at the
        # scheduler level (should_deliver=False before the gate), so the gate
        # produces an empty result set for telegram targets.
        _ctx_owner_home(monkeypatch)
        job = {"id": "s4", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        assert _run_gate(job, "") == []


# ---------------------------------------------------------------------------
# ALLOWLIST / ADMIN EXCEPTIONS (V3) — owner home without origin
# ---------------------------------------------------------------------------

class TestAllowlistExceptions:

    def test_news_digest_deliver_all_owner_home(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "a1", "deliver": "all", "origin": OWNER_TG_ORIGIN}
        news = "Дайджест технологических конкурсов: выпуск 7, 12 материалов."
        _assert_delivers(job, news)

    def test_limits_monitor_anomaly_delivers_owner(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "a3", "deliver": f"telegram:{OWNER_TG}", "no_agent": True}
        anomaly = "⚠️ РГЗ worker-limits: аномалии:\n• effective_max<base: 5 < 8"
        _assert_delivers(job, anomaly, chat=OWNER_TG)
