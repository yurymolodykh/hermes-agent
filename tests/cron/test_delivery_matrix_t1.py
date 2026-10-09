"""
T1 (РГЗ/tghyg): test-matrix of the TG delivery policy + negative isolation tests.

Runs against the LIVE code (F1 gate already injected into cron/scheduler.py
``_deliver_result`` and gateway/kanban_watchers.py ``_tg_notify_policy_decision``).
No real Telegram sendMessage is performed anywhere in this module — the
"delivery" surfaces are stubbed (recording adapter / recording standalone
sender), so the owner is never bothered and every cell is deterministic.

Matrix axes (per the T1 card):

  * origin        : telegram (owner 125805788) | api_server OWUI (owner id,
                    foreign 3daba661…, foreign 9ae3eca0…) | pwa/yadisk_bridge
                    | none (V3 hard-coded admin target)
  * deliver       : origin | telegram | telegram:<chat_id> | all | local
  * content class : artifact (MEDIA:) | final report | [ACTION-REQUIRED]/
                    blocked/needs_input | completed-ping | status-flip |
                    routine no-delta stdout | empty
  * target        : initiator | foreign chat_id | home TG | second OWUI user

Cell convention: every cell encodes the EXPECTED outcome from the owner's policy
spec (research/delivery-policy.md, §0–§7) and asserts the live gate produces it.
FAIL = live code violates the expected result. All rows are self-contained so a
FAIL names the exact cell in ``pytest -rA`` output.

These tests are supplementary to the F1 unit suite (tests/cron/
test_delivery_policy.py, 27 green) — they exercise the full matrix on the live
injected gate rather than re-testing the classifier internals again.
"""
from __future__ import annotations

import os

import pytest

# Home of the real policy env (testing live code paths, not a reimplementation).
from hermes_cli.delivery_policy import (
    InitiatorRef,
    TargetRef,
    classify,
    default_context,
    policy_enabled,
    verify_recipient,
    verify_subscription_recipient,
)

OWNER_TG = "125805788"
ALLOWED = [OWNER_TG]
OWNER_TG_ORIGIN = {"platform": "telegram", "chat_id": OWNER_TG, "user_id": OWNER_TG}
FOREIGN_TG = "999999999"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ctx_owner_home(monkeypatch, allowed=None):
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(allowed or ALLOWED))
    # Home channel must resolve so deliver=all / deliver=telegram target cells
    # actually produce a telegram target.  OWNER_TG is the live owner home
    # (probe env at D0 used TELEGRAM_HOME_CHANNEL=125805788).
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    return OWNER_TG


def _run_gate(job, content, *, artifacts=(), no_agent=False):
    """Apply the LIVE cron gate (_tg_gate_target) to every telegram target.

    Returns a list of (chat_id, allowed_bool) for each telegram target the job
    would resolve to — this is exactly what _deliver_result does per target
    before any send. The send itself is NOT performed here.

    ``no_agent`` is injected into the job dict the way the live scheduler
    stores it (job.get("no_agent")), so the gate ctx sees the real flag.
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
# CONTENT-TYPE DIMENSION (classifier reachable through the live gate)
# ---------------------------------------------------------------------------

ARTIFACT = "Итог:\nMEDIA: C:/tmp/report.pdf\nполный отчёт приложен"
FINAL_REPORT = (
    "Итоговый отчёт периода: закрыто 12 задач, 3 артефакта, все цели достигнуты."
)
REQUIRES_ACTION = "[ACTION-REQUIRED] Владелец, выберите провайдера для развёртывания."
COMPLETED_PING = "✔ Kanban t_abc done — задача закрыта"
STATUS_FLIP = "Kanban t_abc → status done"
ROUTINE = '{"copied": 0, "missing_in_kb": 0, "errors": 0}'
EMPTY = ""


# ---------------------------------------------------------------------------
# NEGATIVE RECIPIENT GATE: НИЧЕГО не уходит чужим / нев-инициатору
# ---------------------------------------------------------------------------

class TestNegativeOthersNeverReceive:
    """Fail-closed: foreign chat_id / second OWUI user / cross-platform strangers.

    These are the highest-priority cells of the T1 card.
    """

    def test_foreign_chat_never_receives_artifact(self, monkeypatch):
        # deliver=telegram:<foreign> with valuable content — must NOT land.
        _ctx_owner_home(monkeypatch)
        job = {"id": "j1", "deliver": f"telegram:{FOREIGN_TG}", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, ARTIFACT, chat_any=True)

    def test_foreign_chat_never_receives_final_report(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "j2", "deliver": f"telegram:{FOREIGN_TG}", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, FINAL_REPORT, chat_any=True)

    def test_foreign_chat_never_receives_action_required(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "j3", "deliver": f"telegram:{FOREIGN_TG}", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, REQUIRES_ACTION, chat_any=True)

    def test_owner_fanout_all_does_not_leave_home_stack(self, monkeypatch):
        # deliver=all with a telegram-origin owner job: only home = owner chat
        # resolves; the fanout must not reach any foreign chat.
        _ctx_owner_home(monkeypatch)
        job = {"id": "j4", "deliver": "all", "origin": OWNER_TG_ORIGIN}
        results = _run_gate(job, FINAL_REPORT)
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
        _assert_suppressed(job, FINAL_REPORT, chat_any=True)

    def test_cross_platform_second_foreign_owui_never_reaches_home(self, monkeypatch):
        # 9ae3eca0… (213 subs) is a DIFFERENT user — never the owner.
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
        _assert_suppressed(job, ARTIFACT, chat_any=True)

    def test_cross_platform_foreign_all_fanout_suppressed(self, monkeypatch):
        # deliver=all from a foreign api_server origin — the TG home leg must
        # be suppressed, not leak the owner's channel.
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
        _assert_suppressed(job, FINAL_REPORT, chat_any=True)

    def test_cross_platform_pwa_never_reaches_home(self, monkeypatch):
        # yadisk_bridge/PWA channel (gateway/origin_outbox.py:27 lists pwa) —
        # a pwa-origin job must not land in owner's TG home either.
        _ctx_owner_home(monkeypatch)
        job = {
            "id": "j8",
            "deliver": "telegram",
            "origin": {"platform": "pwa", "chat_id": "yadisk-dm-1", "user_id": "srv"},
        }
        _assert_suppressed(job, FINAL_REPORT, chat_any=True)

    def test_unknown_origin_admin_not_foreign(self, monkeypatch):
        # V3: no origin recorded but target is the owner's hard-coded chat:
        # admin delivery allowed ONLY to that chat; a foreign target without
        # any known initiator must still be suppressed.
        _ctx_owner_home(monkeypatch)
        job = {"id": "j9", "deliver": f"telegram:{FOREIGN_TG}"}
        _assert_suppressed(job, FINAL_REPORT, chat_any=True)


# ---------------------------------------------------------------------------
# POSITIVE INITIATOR: ценные классы доходят ИНИЦИАТОРУ (в TG)
# ---------------------------------------------------------------------------

class TestPositiveInitiatorGetsValuable:

    def test_owner_origin_artifact(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "p1", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, ARTIFACT)

    def test_owner_origin_final_report(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "p2", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, FINAL_REPORT)

    def test_owner_origin_action_required(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "p3", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, REQUIRES_ACTION)

    def test_owner_home_target_allowlist_admin(self, monkeypatch):
        # V3 allowlist: no-origin admin job pointed at owner chat (limits-monitor)
        _ctx_owner_home(monkeypatch)
        job = {"id": "p4", "deliver": f"telegram:{OWNER_TG}"}
        _assert_delivers(job, FINAL_REPORT, chat=OWNER_TG)

    def test_deliver_local_no_outbound(self, monkeypatch):
        # local jobs are the empty-set of delivered targets — by construction.
        _ctx_owner_home(monkeypatch)
        job = {"id": "p5", "deliver": "local", "origin": OWNER_TG_ORIGIN}
        assert _run_gate(job, FINAL_REPORT) == []


# ---------------------------------------------------------------------------
# SERVICE / ROUTINE: НЕ доходит даже самому инициатору (ИНВ2)
# ---------------------------------------------------------------------------

class TestServiceDeliveriesSuppressed:
    """ИНВ2: cron-рутина не доходит даже самому инициатору.

    NOTE (surface split, T1): «completed-ping»/«status-flip» подавление живёт в
    НОТИФИКАТОРЕ kanban (event_kind известен), а не в cron-гейте — см.
    TestKanbanNotifierMatrix.test_notifier_owner_completed_ping_suppressed и
    test_notifier_owner_status_flip_suppressed.  Cron-гейт (completed_terminal
    hard-coded=True) правильно доставляет финальный stdout джоба и не может
    знать kanban event_kind — это зафиксировано в
    test_cron_ping_like_line_is_job_final_output ниже.
    """

    def test_cron_ping_like_line_is_job_final_output(self, monkeypatch):
        # Cron-выход, даже если читается как «завершение», для cron-гейта —
        # это финальный отчёт джоба (completed_terminal=True).  Рутинное
        # закрытие/статус-флип подавляет НОТИФИКАТОР (эвентный kind), а не
        # cron-гейт; здесь фиксируем фактическое поведение живого гейта.
        _ctx_owner_home(monkeypatch)
        job = {"id": "s1", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, COMPLETED_PING)
        job2 = {"id": "s2", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job2, STATUS_FLIP)

    def test_routine_no_delta_stdout_suppressed(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "s3", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, ROUTINE, chat_any=True, no_agent=True)

    def test_empty_content_suppressed(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        job = {"id": "s4", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_suppressed(job, EMPTY, chat_any=True)


# ---------------------------------------------------------------------------
# ALLOWLIST EXCEPTIONS (NIGHT-PLAN §1.3, §1.4, §1.1)
# ---------------------------------------------------------------------------

class TestAllowlistExceptions:

    def test_news_digest_deliver_all_owner_home(self, monkeypatch):
        # global-technology-prize-news-every-4th-Sunday deliver=all — allowlist:
        # owner initiates from TG, home == owner chat -> delivered.
        _ctx_owner_home(monkeypatch)
        job = {"id": "a1", "deliver": "all", "origin": OWNER_TG_ORIGIN}
        news = "Дайджест технологических конкурсов: выпуск 7, 12 материалов."
        _assert_delivers(job, news)

    def test_session_restore_message_not_suppressed_prefix(self, monkeypatch):
        # §1.4: “Сессия восстановлена после перезапуска шлюза” — must never be
        # classified routine. It is not covered by the gate (gateway-level
        # send), but the classifier must not silently swallow it either.
        _ctx_owner_home(monkeypatch)
        job = {"id": "a2", "deliver": "origin", "origin": OWNER_TG_ORIGIN}
        _assert_delivers(job, "Сессия восстановлена после перезапуска шлюза")

    def test_limits_monitor_anomaly_not_suppressed(self, monkeypatch):
        # §1.1: rgz-worker-limits-monitor alert-only — ANOMALY must pass
        # (non-routine no_agent output); the healthy leg emits EMPTY stdout
        # (script header: «В НОРМЕ: пустой stdout»), which is suppressed.
        _ctx_owner_home(monkeypatch)
        job = {"id": "a3", "deliver": f"telegram:{OWNER_TG}", "no_agent": True}
        # Real F2 message shape (profiles/02/scripts/rgz-worker-limits-monitor.py):
        anomaly = "⚠️ РГЗ worker-limits: аномалии:\n• effective_max<base: 5 < 8\nвремя: 2026-10-08 12:00:00 MSK"
        _assert_delivers(job, anomaly, chat=OWNER_TG)
        # healthy leg — silent (empty stdout) => suppressed, not an alert
        _assert_suppressed(job, EMPTY, chat_any=True)


# ---------------------------------------------------------------------------
# KANBAN NOTIFIER DIMENSION (_tg_notify_policy_decision — live code)
# ---------------------------------------------------------------------------

class _Ev:
    def __init__(self, kind, payload=None, eid="ev-1"):
        self.kind = kind
        self.payload = payload or {}
        self.id = eid


def _notify_decision(sub, ev, msg, task=None, allowed=None,
                     *, task_is_root=None, task_tg_owner=""):
    from gateway.kanban_watchers import _tg_notify_policy_decision

    return _tg_notify_policy_decision(
        sub, ev, ev.kind, msg, task, list(allowed or ALLOWED),
        task_is_root=task_is_root, task_tg_owner=task_tg_owner,
    )


class TestKanbanNotifierMatrix:

    def test_notifier_owner_completed_final_delivers(self, monkeypatch):
        # K1 RF2: a completed is terminal ONLY via the explicit final marker —
        # here the branch ROOT closing (task_is_root=True) — NOT because a
        # summary is present.  Root closing + owner sub → final delivers.
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t1"}
        ev = _Ev("completed", {"summary": "Окончательный отчёт готов."})
        dec = _notify_decision(sub, ev, ev.payload["summary"], task_is_root=True)
        assert dec is not None and not dec.suppressed

    def test_notifier_owner_completed_archived_task_delivers(self, monkeypatch):
        # K1 RF2: task.status=="archived" is the second explicit final marker.
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t1a"}
        ev = _Ev("completed", {"summary": "Итог"})
        fake_task = type("FakeTask", (), {"status": "archived"})()
        dec = _notify_decision(sub, ev, ev.payload["summary"], task=fake_task)
        assert dec is not None and not dec.suppressed

    def test_notifier_owner_intermediate_completed_with_summary_suppressed(self, monkeypatch):
        # K1 RF2 (ИНВ2): an INTERMEDIATE sub-task completed WITH a summary but
        # with NO explicit final marker (not root closing, not archived) is a
        # routine status close → suppressed (before RF2 this leaked through as
        # deliver(default) and woke the creator).
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t1b"}
        ev = _Ev("completed", {"summary": "Промежуточный отчёт: подзадача готова."})
        dec = _notify_decision(sub, ev, ev.payload["summary"])
        assert dec is not None and dec.suppressed

    def test_notifier_owner_completed_ping_suppressed(self, monkeypatch):
        # completed intermediate ping WITHOUT handoff -> routine status close.
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t2"}
        dec = _notify_decision(sub, _Ev("completed", {}), "✔ done")
        assert dec is not None and dec.suppressed

    def test_notifier_owner_status_flip_suppressed(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t3"}
        dec = _notify_decision(sub, _Ev("status", {}), "task moved to done")
        assert dec is not None and dec.suppressed

    def test_notifier_owner_blocked_delivers(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t4"}
        dec = _notify_decision(sub, _Ev("blocked", {"reason": "нужен выбор"}), "⏸ blocked")
        assert dec is not None and not dec.suppressed

    def test_notifier_owner_artifacts_deliver(self, monkeypatch):
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t5"}
        ev = _Ev("completed", {"summary": "Итог", "artifacts": ["C:/tmp/report.md"]})
        dec = _notify_decision(sub, ev, ev.payload["summary"])
        assert dec is not None and not dec.suppressed

    def test_notifier_foreign_user_never(self, monkeypatch):
        # A foreign telegram subscriber must receive NOTHING — even blocked.
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "telegram", "user_id": FOREIGN_TG, "chat_id": FOREIGN_TG, "task_id": "t6"}
        for kind, msg in (("blocked", "⏸ blocked"), ("completed", "✔ done")):
            dec = _notify_decision(sub, _Ev(kind, {}), msg)
            assert dec is not None and dec.suppressed, f"foreign got {kind}"

    def test_notifier_api_server_subscription_untouched(self, monkeypatch):
        # api_server subs are verified by the server registry — the TG gate
        # must return None (out of scope), not suppress them.
        _ctx_owner_home(monkeypatch)
        sub = {"platform": "api_server", "user_id": "owui-u", "chat_id": "c", "task_id": "t7"}
        dec = _notify_decision(sub, _Ev("completed", {}), "✔ done")
        assert dec is None

    def test_wake_kinds_exclude_suppressed_event(self, monkeypatch):
        # Wake-suppression interplay (kanban_watchers.py:1113-1124): an event
        # whose id is in _tg_suppressed_event_ids must NOT belong to the
        # wake set even when its kind is a _WAKE_KIND (e.g. completed ping).
        _ctx_owner_home(monkeypatch)
        from gateway.kanban_watchers import _tg_notify_policy_decision

        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t8"}
        # 1) routine completed ping (no summary -> not terminal) is suppressed
        #    by the gate, and its event id is what the notifier would record.
        ev = _Ev("completed", {}, eid="ev-routine-1")
        dec = _tg_notify_policy_decision(sub, ev, "completed", "✔ done", None, ALLOWED)
        assert dec is not None and dec.suppressed
        suppressed_ids = {ev.id}
        # 2) the wake set built exactly like the notifier's loop
        events = [ev]
        wake_kinds = {
            e.kind
            for e in events
            if e.kind in _WAKE_KINDS
            and getattr(e, "id", None) not in suppressed_ids
        }
        # a suppressed routine completed ping must not wake the creator
        assert "completed" not in wake_kinds
        # 3) sanity: an unsuppressed blocked event (human action) WOULD wake
        ev2 = _Ev("blocked", {"reason": "нужен выбор"}, eid="ev-blocked-2")
        dec2 = _tg_notify_policy_decision(sub, ev2, "blocked", "🛑 blocked", None, ALLOWED)
        assert dec2 is not None and not dec2.suppressed
        wake2 = {
            e.kind
            for e in [ev, ev2]
            if e.kind in _WAKE_KINDS
            and getattr(e, "id", None) not in suppressed_ids
        }
        assert "blocked" in wake2

    def test_notifier_policy_module_broken_fail_closed(self, monkeypatch):
        # K1 RF1: when the delivery-policy MODULE cannot be imported the
        # notifier must FAIL CLOSED — suppress (journal + ERROR log), never
        # fall back to "deliver". Pre-RF1 this returned None (deliver).
        _ctx_owner_home(monkeypatch)
        import sys
        from gateway.kanban_watchers import _tg_notify_policy_decision

        class _BrokenPolicy:
            def __getattr__(self, _name):
                raise ImportError("simulated broken policy module")

        monkeypatch.setitem(sys.modules, "hermes_cli.delivery_policy", _BrokenPolicy())
        sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t9"}
        ev = _Ev("blocked", {"reason": "важно"})
        dec = _tg_notify_policy_decision(sub, ev, "blocked", "🛑 blocked", None, ALLOWED)
        assert dec is not None and dec.suppressed

    def test_notifier_policy_enabled_raises_fail_closed(self, monkeypatch):
        # K1 RF1: module imports fine but policy_enabled() raises -> suppress.
        _ctx_owner_home(monkeypatch)
        import sys
        from gateway.kanban_watchers import _tg_notify_policy_decision
        from hermes_cli import delivery_policy as _dp

        orig = sys.modules.get("hermes_cli.delivery_policy")

        class _RaisingEnabled:
            def __getattr__(self, name):
                if name == "policy_enabled":
                    def _boom(*_a, **_k):
                        raise RuntimeError("policy_enabled boom")
                    return _boom
                return getattr(_dp, name)

        monkeypatch.setitem(sys.modules, "hermes_cli.delivery_policy", _RaisingEnabled())
        try:
            sub = {"platform": "telegram", "user_id": OWNER_TG, "chat_id": OWNER_TG, "task_id": "t9b"}
            dec = _tg_notify_policy_decision(
                sub, _Ev("blocked", {}), "blocked", "🛑 blocked", None, ALLOWED,
            )
            assert dec is not None and dec.suppressed
        finally:
            if orig is not None:
                monkeypatch.setitem(sys.modules, "hermes_cli.delivery_policy", orig)


# ---------------------------------------------------------------------------
# POLICY FLAG: gate off == byte-identical historical behaviour (F1 §8)
# ---------------------------------------------------------------------------

class TestPolicyFlag:

    def test_gate_off_passthrough(self, monkeypatch):
        monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
        # The _deliver_result gate returns None => _tg_gate_target returns True
        # (historical behaviour). Verify at the classifier level: policy off.
        assert not policy_enabled()
        job = {"id": "f1", "deliver": f"telegram:{FOREIGN_TG}", "origin": OWNER_TG_ORIGIN}
        results = _run_gate(job, FINAL_REPORT)
        assert len(results) == 1 and results[0][1] is True

    def test_gate_on_active(self, monkeypatch):
        monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
        assert policy_enabled()
