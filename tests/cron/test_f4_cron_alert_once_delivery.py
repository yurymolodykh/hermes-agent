# -*- coding: utf-8 -*-
"""F4: cron -> TG контракт — alert-once аномалий + доставка артефактов.

Покрывает:
1. detect_system_alert распознаёт cron-failure ОБЁРНУТЫЙ заголовком
   «Cronjob Response: ...» и с именем джоба между «Cron» и «failed»
   (root D4: иначе аномалия классифицируется как final_report и гасится
   по recipient mismatch).
2. _tg_gate_target: системный алерт доставляется в home/ALLOWED чат
   владельца БЕЗ verify_recipient (root D5: api_server-cron-джобы без
   user_id в origin гасились как recipient mismatch), и НЕ доставляется
   в чужой чат (fail-closed).
3. alert-once по классу сбоя: cron.jobs mark_failure_alerted /
   clear_failure_alerted — ровно один алерт на (job, error_class),
   новый класс -> новый алерт, после heal -> сброс.
4. Артефакт html в no_agent-джобе проходит gate (MEDIA: маркер).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

# ---------------------------------------------------------------------------
# 1. detect_system_alert — распознавание cron-failure (FIX-1)
# ---------------------------------------------------------------------------


def _detect(text):
    from hermes_cli.delivery_policy import detect_system_alert

    return detect_system_alert(text)


class TestDetectSystemAlertCronFailure:
    def test_wrapped_cronjob_response_failure(self):
        # Реальное тело аномалии cg-report-early-tg-watcher:
        # scheduler ставит «Cronjob Response: <name>…» ПЕРЕД failure-текстом.
        text = (
            "Cronjob Response: cg-report-early-tg-watcher\n"
            "(job_id: c204bde55dc5)\n"
            "-------------\n\n"
            "⚠️ Cron 'cg-report-early-tg-watcher' failed: Script not found: "
            "C:\\Users\\hermes\\AppData\\Local\\hermes\\profiles\\"
            "05-rag-knowledge-engineer\\scripts\\cg_early_report_tg.py"
        )
        assert _detect(text) == "cron_failure"

    def test_cron_name_between_cron_and_failed(self):
        # «⚠️ Cron '<name>' failed: ...» — имя ломает непрерывный штамп.
        # Здесь срабатывает либо префикс «⚠️ Cron» (cron_alert), либо новый
        # _FAIL_STAMP_RE (cron_failure) — ОБА означают системный алерт
        # (classify увидит reason="system_alert").  Ключевое — не None.
        assert _detect("⚠️ Cron 'cg-report-early-tg-watcher' failed: Script not found") in (
            "cron_alert", "cron_failure",
        )
        assert _detect("⚠️ Cron 'disk-cleanup' failed: Script timed out") in (
            "cron_alert", "cron_failure",
        )
        assert _detect("⚠️ Cron 'nightly' skipped: drifted") in (
            "cron_alert", "cron_failure",
        )

    def test_unwrapped_plain_cron_failed_still_detected(self):
        # Существующее поведение не ломается.
        assert _detect("cron job failed after 3 tries") == "cron_failure"
        assert _detect("⚠️ Cron blocked by configuration validation") == "cron_alert"

    def test_ordinary_report_not_detected(self):
        # Не должны срабатывать на обычных отчётах.
        assert _detect("итог: скопировано 5 записей, ошибок 0") is None
        assert _detect("The old server failed twice last year, now fine") is None
        # Слово failed в середине без cron-контекста — не системный алерт.
        assert _detect("Найдено 0 новых файлов, обработка завершена успешно") is None


# ---------------------------------------------------------------------------
# 2. _tg_gate_target: system_alert bypass verify (FIX-2)
# ---------------------------------------------------------------------------


def _build_gate_ctx(allowed_users=("125805788",), home_channel="125805788",
                    origin=None):
    """Политический контекст как в _tg_delivery_policy_context, но с
    контролируемыми home/allowed (без обращения к env/диску)."""
    from hermes_cli.delivery_policy import (
        InitiatorRef,
        classify,
        default_context,
        log_suppression,
        owui_telegram_users,
        verify_recipient,
    )

    return {
        "initiator": InitiatorRef.from_origin(
            origin or {"platform": "api_server", "chat_id": "owui-abc", "user_id": None}
        ),
        "home_channel": home_channel,
        "allowed_users": list(allowed_users),
        "has_media": False,
        "media_files": [],
        "no_agent": True,
        "owui_to_tg": owui_telegram_users() or {},
        "policy_disabled": False,
        "classify": classify,
        "default_context": default_context,
        "log_suppression": log_suppression,
        "verify_recipient": verify_recipient,
    }


def _gate_target(target_chat, content, allowed_users=("125805788",),
                 origin=None):
    from cron.scheduler import _tg_gate_target

    ctx = _build_gate_ctx(allowed_users=allowed_users, origin=origin)
    target = {"platform": "telegram", "chat_id": target_chat, "thread_id": None}
    # log_suppression пишет в файл журнала подавлений — в тесте он получает
    # настоящий delivery_policy.log_suppression; для fail-closed-веток это
    # безопасно (только append-запись в temp, не влияет на вердикт).
    return _tg_gate_target(target, content, ctx)


class TestSystemAlertBypassVerify:
    OBWRAP_FAILURE = (
        "Cronjob Response: cg-report-early-tg-watcher\n"
        "(job_id: c204bde55dc5)\n-------------\n\n"
        "⚠️ Cron 'cg-report-early-tg-watcher' failed: Script not found: "
        "C:\\...\\05-rag-knowledge-engineer\\scripts\\cg_early_report_tg.py"
    )

    def test_wrapped_cron_failure_owner_chat_delivered(self):
        # root D4/D5: обёрнутая аномалия для OWNER-чата доставляется,
        # несмотря на user_id=null в origin.
        assert _gate_target(
            "125805788", self.OBWRAP_FAILURE,
            origin={"platform": "api_server", "chat_id": "owui-abc", "user_id": None},
        ) is True

    def test_plain_cron_failure_owner_home_delivered(self):
        # Даже без обёртки — системный алерт доставляется в home владельца.
        assert _gate_target(
            "125805788", "⚠️ Cron 'cg-x' failed: Script not found: C:\\x.py",
            origin={"platform": "api_server", "chat_id": "owui-abc", "user_id": None},
        ) is True

    def test_system_alert_foreign_chat_fail_closed(self):
        # Fail-closed: системный алерт НЕ уходит в чужой (не home/ALLOWED) чат.
        assert _gate_target(
            "999", self.OBWRAP_FAILURE,
            origin={"platform": "api_server", "chat_id": "owui-abc", "user_id": None},
        ) is False

    def test_system_alert_other_allowed_still_delivered(self):
        # Любой ALLOWED-пользователь (не только home) получает системный алерт.
        assert _gate_target(
            "117", self.OBWRAP_FAILURE, allowed_users=("125805788", "117"),
            origin={"platform": "api_server", "chat_id": "owui-abc", "user_id": None},
        ) is True

    def test_ordinary_final_report_still_requires_verify(self):
        # Некритичное содержимое НЕ получает bypass: verify_recipient
        # отклоняет api_server-origin без user_id даже в home.
        assert _gate_target(
            "125805788", "Сервер Острова можно безопасно перезагружать",
            origin={"platform": "api_server", "chat_id": "owui-abc", "user_id": None},
        ) is False


# ---------------------------------------------------------------------------
# 3. Артефакт html в no_agent-джобе — MEDIA: маркер проходит gate (FIX-3)
# ---------------------------------------------------------------------------

class TestCronArtifactHtmlDelivery:
    def test_html_artifact_media_marker_classified_deliver(self):
        from hermes_cli.delivery_policy import InitiatorRef, classify, default_context

        ctx = default_context(
            kind="cron",
            initiator=InitiatorRef.from_origin(
                {"platform": "api_server", "chat_id": "owui-abc", "user_id": None}
            ),
            target=None,
            home_channel="125805788",
            allowed_users=["125805788"],
            no_agent=True,
            completed_terminal=True,
            has_media=True,
        )
        dec = classify(
            "РГЗ chamber-graves: итоговый отчёт (камерные погребения)\n"
            "MEDIA: C:/archeo/chamber-graves-report.html",
            ctx,
        )
        assert dec.delivered, f"artifact must deliver, got {dec.action}: {dec.reason}"

    def test_html_artifact_gate_allowed_to_owner(self):
        # End-to-end: артефакт в no_agent-джобе (owner origin) доставляется.
        origin = {"platform": "api_server", "chat_id": "owui-own", "user_id": "owner1",
                  "owner_id": "owner1"}
        # owner в привязке owui_to_tg: direct через allowed.
        from hermes_cli.delivery_policy import InitiatorRef

        # Для единообразия: TELEGRAM_ALLOWED_USERS содержит owner-чат -> verify ok.
        import os

        ctx = _build_gate_ctx(
            allowed_users=("125805788",),
            home_channel="125805788",
            origin={"platform": "telegram", "chat_id": "125805788",
                    "user_id": "125805788"},
        )
        from cron.scheduler import _tg_gate_target

        target = {"platform": "telegram", "chat_id": "125805788", "thread_id": None}
        assert _tg_gate_target(
            target,
            "РГЗ chamber-graves: итоговый отчёт\n"
            "MEDIA: C:/archeo/chamber-graves-report.html",
            ctx,
        ) is True


# ---------------------------------------------------------------------------
# 4. Alert-once по классу сбоя — cron.jobs persisted-бит
# ---------------------------------------------------------------------------

class TestFailureAlertOnce:
    def test_mark_and_clear(self, tmp_path):
        import cron.jobs as cron_jobs

        job = {
            "id": "f4-alert-job",
            "name": "f4 alert job",
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
            "deliver": "local",
            "no_agent": True,
            "script": "x.py",
        }
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])

            # Первый тик того же класса -> False (не алертили, алертим сейчас).
            assert cron_jobs.mark_failure_alerted("f4-alert-job", "script_not_found") is False
            # Второй тик того же класса -> True (уже алертили, молчим).
            assert cron_jobs.mark_failure_alerted("f4-alert-job", "script_not_found") is True
            # Другой класс -> False (новая причина, новый алерт).
            assert cron_jobs.mark_failure_alerted("f4-alert-job", "script_timed_out") is False
            # Тот же новый класс повторно -> True.
            assert cron_jobs.mark_failure_alerted("f4-alert-job", "script_timed_out") is True

            # heal -> сброс; будущий сбой снова алертит.
            cron_jobs.clear_failure_alerted("f4-alert-job")
            assert cron_jobs.mark_failure_alerted("f4-alert-job", "script_not_found") is False

            # Поле персистится на джобе (переживает перезагрузку).
            stored = [j for j in cron_jobs.load_jobs() if j["id"] == "f4-alert-job"][0]
            assert stored.get("failure_alerted") == "script_not_found"

    def test_success_clears_marker(self, tmp_path):
        import cron.jobs as cron_jobs

        job = {
            "id": "f4-clear",
            "name": "f4 clear",
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
            "deliver": "local",
            "no_agent": True,
            "script": "x.py",
        }
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            cron_jobs.mark_failure_alerted("f4-clear", "script_not_found")
            # mark_job_run(success=True) чистит failure_alerted.
            cron_jobs.mark_job_run("f4-clear", True, error=None)
            stored = [j for j in cron_jobs.load_jobs() if j["id"] == "f4-clear"][0]
            assert not stored.get("failure_alerted")

    def test_unknown_job_returns_false(self, tmp_path):
        import cron.jobs as cron_jobs

        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([])
            assert cron_jobs.mark_failure_alerted("missing", "script_not_found") is False


# ---------------------------------------------------------------------------
# 5. _cron_failure_alert_class — классификация сбоя (юнит)
# ---------------------------------------------------------------------------

class TestCronFailureAlertClass:
    def test_recognized_classes(self):
        from cron.scheduler import _cron_failure_alert_class

        assert _cron_failure_alert_class("Script not found: C:\\x.py") == "script_not_found"
        assert _cron_failure_alert_class("Script timed out after 900s: C:\\x.py") == "script_timed_out"
        assert _cron_failure_alert_class("Script exited with code 1\nstderr: boom") == "script_exit_code"
        assert _cron_failure_alert_class("Script execution failed: <Error>") == "script_failed"
        assert _cron_failure_alert_class("[blocked_config] config invalid") == "blocked_config"
        assert _cron_failure_alert_class("[drift_skip] provider changed") == "drift_skip"

    def test_unrelated_error_not_system_alert(self):
        from cron.scheduler import _cron_failure_alert_class

        assert _cron_failure_alert_class(None) is None
        assert _cron_failure_alert_class("") is None
        assert _cron_failure_alert_class("RuntimeError: boom unrelated") is None
        # Не системный крон-сбой -> без alert-once (обычный failure-streak).
        assert _cron_failure_alert_class("agent said: не получилось сохранить отчёт") is None


# ---------------------------------------------------------------------------
# 6. Интеграция: run_one_job + alert-once реального сбоя (по образцу
#    test_cron_no_agent / test_cron_drift_alert_once)
# ---------------------------------------------------------------------------

@pytest.fixture
def _hermes_env(tmp_path, monkeypatch):
    """Изолированный HERMES_HOME, как в test_cron_no_agent."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


class TestRunOneJobFailureAlertOnce:
    def test_two_failing_ticks_alert_exactly_once(self, _hermes_env, monkeypatch):
        """Два тика с одним классом сбоя Script-not-found -> ровно один алерт.

        Приёмка F4 (1): упавшая джоба даёт ровно один alert в TG.
        """
        import cron.jobs as cron_jobs
        from cron.jobs import create_job
        import cron.scheduler as scheduler

        # Скрипт отсутствует -> Script not found (класс script_not_found).
        job = create_job(
            prompt=None, schedule="every 5m", script="missing.py",
            no_agent=True, deliver="telegram", name="f4-missing",
        )
        delivered = []

        monkeypatch.setattr(
            scheduler,
            "_deliver_result",
            lambda _job, content, **_kwargs: delivered.append(content),
        )
        monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: {})
        # Отключаем nudge от фейлстрейка, чтобы не влиял на подсчёт.
        monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: _hermes_env)
        monkeypatch.setattr(
            "hermes_cli.env_loader.load_hermes_dotenv", lambda **_kw: None,
        )

        for _ in range(2):
            # reload из стора, чтобы бит пережил тики
            fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            assert scheduler.run_one_job(fresh) is True

        assert len(delivered) == 1, f"expected 1 alert, got {len(delivered)}: {delivered}"
        blob = delivered[0].lower()
        assert "script not found" in blob or "failed" in blob

    def test_heal_clears_bit_realert_no_agent(self, _hermes_env, monkeypatch):
        import cron.jobs as cron_jobs
        from cron.jobs import create_job
        import cron.scheduler as scheduler

        job = create_job(
            prompt=None, schedule="every 5m", script="missing.py",
            no_agent=True, deliver="telegram", name="f4-missing2",
        )
        delivered = []
        monkeypatch.setattr(
            scheduler,
            "_deliver_result",
            lambda _job, content, **_kwargs: delivered.append(content),
        )
        monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: {})
        monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: _hermes_env)
        monkeypatch.setattr(
            "hermes_cli.env_loader.load_hermes_dotenv", lambda **_kw: None,
        )

        # 1-й тик: сбой -> 1 алерт, бит set.
        fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
        assert scheduler.run_one_job(fresh) is True
        assert len(delivered) == 1

        # «Починка»: пишем скрипт, возвращающий пустой stdout (молчащий тик).
        (_hermes_env / "scripts" / "missing.py").write_text(
            'import sys\nsys.stdout.write("")\n'
        )
        fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
        assert scheduler.run_one_job(fresh) is True
        # молчащий успешный тик не даёт доставки; бит сброшен по success.
        stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
        assert not stored.get("failure_alerted")

        # Бит сброшен, но скрипт реально успешен -> новый сбой невозможен без
        # смены класса. Для чистоты проверки: удалим скрипт -> сбой -> новый
        # алерт (класс другой? нет — тот же script_not_found, но бит был
        # сброшен heal'ом, поэтому алерт уходит).
        (_hermes_env / "scripts" / "missing.py").unlink()
        fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
        assert scheduler.run_one_job(fresh) is True

        asserts = [d for d in delivered]
        assert len(asserts) == 2, f"expected re-alert after heal: {delivered}"
