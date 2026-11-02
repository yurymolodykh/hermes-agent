"""t_b154af7d — автодоставка финального отчёта терминальной карточки в TG.

Acceptance карточки t_b154af7d:

  «для sno-economics-r3 повторное завершение/эмуляция завершения приводит к
   автоматической отправке финального отчёта владельцу; в логах видно событие
   отправки без ручного действия».

Дочерняя карточка t_bd966ce0 расширяет приёмку на ВСЕ миссии с воркерами и
требует обработки краевых случаев: отчёты не-HTML, миссии без финального
отчёта, повторные завершения, сбои отправки.

Что именно проверяется (реальный watcher-стек, не мок решения):

  1. Терминальное закрытие OWUI-миссии уходит владельцу в TG автоматически —
     без ручного запроса, с отчётом как вложением (send_document) и текстом
     итога, содержащим summary.
  2. Отчёт, который произвёл ДОЧЕРНИЙ узел (контроллер закрыт административно,
     без artifacts) — всё равно доставляется: subtree-фолбэк.
  3. Отчёт, объявленный в «точке истины» миссии (durable/<group>/<mission>),
     доставляется, когда ни корень, ни поддерево артефактов не несут.
  4. Отчёт НЕ-HTML (.md/.txt) доставляется тем же путём (краевой случай из
     t_bd966ce0), не-HTML не блокирует отправку.
  5. Миссия БЕЗ отчёта вообще: текст итога доставляется, ничего не падает
     (нет ложного «отчёт отправлен»).
  6. СБОЙ отправки (``success=False`` / ``send_path_degraded``): исключительно
     важный инвариант — once-only dedup-маркер НЕ пишется, канал остаётся
     pending, следующий тик ПОВТОРЯЕТ отправку и доставляет отчёт. Именно
     потеря этого повтора была причиной инцидента sno-economics-r3.
  7. Повторное завершение (re-close после reopen) доставляет ровно одно новое
     сообщение; повторные тики без изменения состояния — ни одного (нет спама).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

OWNER_OWUI = "sess-owui-owner"
OWNER_TG = "tg-owner-125805788"


class FlakyAdapter:
    """RecordingAdapter с управляемыми сбоями ``send`` (кейс 6)."""

    supports_async_delivery = True

    def __init__(self):
        self.sent: list[dict] = []
        self.docs: list[dict] = []
        self.fail_sends = 0          # сколько следующих send провалить
        self.fail_error = "send_path_degraded"

    async def send(self, chat_id, text, metadata=None):
        if self.fail_sends > 0:
            self.fail_sends -= 1
            return SendResult(success=False, error=self.fail_error, retryable=True)
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
        return SendResult(success=True, message_id=len(self.sent))

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append(
            {"chat_id": chat_id, "file_path": str(file_path),
             "file_name": file_name or os.path.basename(str(file_path))}
        )
        return SendResult(success=True)

    async def handle_message(self, event):
        return None


@pytest.fixture
def mission_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "tgauto.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", OWNER_TG)
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.API_SERVER: adapter}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner


async def _run_one_branch_tick(monkeypatch, runner, interval=1):
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


def _staged_sink(monkeypatch, staged, state="acknowledged"):
    from gateway import kanban_origin_delivery as _kod

    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return (state, 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)


def _mk_mission(conn, *, report_factory=None):
    """Root (OWUI mission, controller) + 2 children; report made by a child."""
    root = kb.create_task(conn, title="mission", assignee="worker")
    kids = []
    for i in range(2):
        tid = kb.create_task(conn, title=f"child-{i}", assignee="worker")
        kb.link_tasks(conn, root, tid)
        kids.append(tid)
    kb.add_notify_sub(
        conn, task_id=root, platform="api_server", chat_id="owui-request",
        user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    return root, kids


def _report(tmp_path: Path, name: str, body: str = None) -> Path:
    p = tmp_path / name
    body = body or (
        "<!doctype html><html><body><h1>Итоговый отчёт миссии</h1>"
        + "".join(f"<p>Раздел {i}: результаты и выводы.</p>" for i in range(60))
        + "</body></html>"
    )
    p.write_text(body, encoding="utf-8")
    return p


def _complete(conn, tid, summary="done", artifacts=None):
    meta = {"artifacts": [str(a) for a in artifacts]} if artifacts else None
    return kb.complete_task(conn, tid, summary=summary, metadata=meta)


# ---------------------------------------------------------------------------
# 1. Терминальное закрытие: отчёт дочернего узла автоматически уходит в TG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminal_close_autodelivers_child_report_to_owner_tg(
    mission_env, tmp_path, monkeypatch,
):
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="контроллер миссии закрыт")
    _complete(mission_env, kids[0], summary="S6: отчёт готов", artifacts=[report])
    _complete(mission_env, kids[1], summary="S7: приёмка")

    staged: list = []
    _staged_sink(monkeypatch, staged)
    adapter = FlakyAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)

    assert adapter.sent, "терминальный итог обязан уйти в TG автоматически"
    msg = adapter.sent[0]["text"]
    assert adapter.sent[0]["chat_id"] == OWNER_TG
    assert "контроллер миссии закрыт" in msg
    assert [d["file_name"] for d in adapter.docs] == [report.name], (
        f"финальный отчёт должен уйти вложением: {adapter.docs}"
    )

    # Второй тик: ни повторной отправки, ни повторного вложения.
    adapter.sent.clear()
    adapter.docs.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [] and adapter.docs == [], (
        "повторный тик не должен дублировать доставленный итог"
    )


# ---------------------------------------------------------------------------
# 2. Контроллер закрыт без artifacts: отчёт берётся из поддерева
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_controller_without_artifacts_uses_subtree_report(
    mission_env, tmp_path, monkeypatch,
):
    report = _report(tmp_path, "mission-report-final.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="контроллер закрыт административно")
    _complete(mission_env, kids[0], summary="подготовлен отчёт", artifacts=[report])
    _complete(mission_env, kids[1], summary="готово")

    from gateway.delivery_engine import owui_root_artifact_paths
    paths = owui_root_artifact_paths(
        {"root_id": root, "board": "default", "marker_id": 1}
    )
    assert [Path(p).name for p in paths] == [report.name], paths

    staged: list = []
    _staged_sink(monkeypatch, staged)
    adapter = FlakyAdapter()
    await _run_one_branch_tick(monkeypatch, _make_runner(adapter))
    assert [d["file_name"] for d in adapter.docs] == [report.name]


# ---------------------------------------------------------------------------
# 3. Отчёт объявлен в «точке истины» миссии (durable/<group>/<mission>)
# ---------------------------------------------------------------------------

def test_mission_declared_report_is_resolved(mission_env, tmp_path):
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    mission_dir = tmp_path / "durable" / "sno" / "sno-economics-r3"
    mission_dir.mkdir(parents=True)
    (mission_dir / "NIGHT-PLAN.md").write_text(
        "# План\n\nФинальный отчёт: "
        f"{report.as_posix()}\n\nПрочие файлы: "
        f"{(tmp_path / 'build_script.py').as_posix()}\n",
        encoding="utf-8",
    )
    (tmp_path / "build_script.py").write_text("print(1)\n", encoding="utf-8")

    root = kb.create_task(mission_env, title="контроллер миссии", assignee="worker")
    with kb.write_txn(mission_env):
        mission_env.execute(
            "UPDATE tasks SET body=? WHERE id=?",
            (f"Точка истины: {mission_dir.as_posix()}", root),
        )
    kb.add_notify_sub(
        mission_env, task_id=root, platform="api_server", chat_id="owui-request",
        user_id=OWNER_OWUI, delivery_mode="notify+wake",
    )
    # Закрываем административно — без completed-события и без artifacts.
    with kb.write_txn(mission_env):
        mission_env.execute("UPDATE tasks SET status='archived' WHERE id=?", (root,))

    from gateway.delivery_engine import owui_root_artifact_paths
    paths = owui_root_artifact_paths({"root_id": root, "board": "default"})
    assert [Path(p).name for p in paths] == [report.name], paths


# ---------------------------------------------------------------------------
# 4. Не-HTML отчёт доставляется тем же путём
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_html_report_is_delivered(mission_env, tmp_path, monkeypatch):
    report = _report(
        tmp_path, "mission-report.md",
        "# Отчёт\n\n" + "\n".join(f"- вывод {i}" for i in range(300)) + "\n",
    )
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="закрыто")
    _complete(mission_env, kids[0], summary="отчёт", artifacts=[report])
    _complete(mission_env, kids[1], summary="готово")

    staged: list = []
    _staged_sink(monkeypatch, staged)
    adapter = FlakyAdapter()
    await _run_one_branch_tick(monkeypatch, _make_runner(adapter))
    assert [d["file_name"] for d in adapter.docs] == [report.name]


# ---------------------------------------------------------------------------
# 5. Миссия без отчёта: текст уходит, ничего не падает
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mission_without_report_still_delivers_text(
    mission_env, monkeypatch,
):
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="миссия завершена без отчётного файла")
    for i, c in enumerate(kids):
        _complete(mission_env, c, summary=f"шаг {i}")

    staged: list = []
    _staged_sink(monkeypatch, staged)
    adapter = FlakyAdapter()
    await _run_one_branch_tick(monkeypatch, _make_runner(adapter))
    assert adapter.sent, "текст итога обязан дойти даже без отчёта"
    assert "миссия завершена без отчётного файла" in adapter.sent[0]["text"]
    assert adapter.docs == []


# ---------------------------------------------------------------------------
# 6. Сбой отправки: маркер НЕ пишется, канал pending, следующий тик повторяет
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_send_is_retried_and_report_not_lost(
    mission_env, tmp_path, monkeypatch,
):
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="миссия закрыта")
    _complete(mission_env, kids[0], summary="отчёт", artifacts=[report])
    _complete(mission_env, kids[1], summary="готово")

    staged: list = []
    _staged_sink(monkeypatch, staged)
    adapter = FlakyAdapter()
    adapter.fail_sends = 1  # первый send падает (send_path_degraded)
    runner = _make_runner(adapter)

    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [], "сбойный send не должен считаться доставкой"
    assert adapter.docs == [], "отчёт не отправляется, пока текст не принят"
    # Нет необработанного исключения в цикле — ошибка зафиксирована явно.
    assert "send_path_degraded" in (getattr(runner, "_kanban_last_branch_error", "") or "")

    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent, "следующий тик обязан ПОВТОРИТЬ доставку итога"
    assert [d["file_name"] for d in adapter.docs] == [report.name], (
        "после успешного повтора отчёт обязан уйти владельцу"
    )

    # Третий тик — уже без дублей.
    adapter.sent.clear()
    adapter.docs.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [] and adapter.docs == []


# ---------------------------------------------------------------------------
# 7. Повторное завершение (reopen → close) = ровно одно новое сообщение
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reclose_after_reopen_delivers_exactly_once_more(
    mission_env, tmp_path, monkeypatch,
):
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="первое закрытие")
    _complete(mission_env, kids[0], summary="отчёт", artifacts=[report])
    _complete(mission_env, kids[1], summary="готово")

    staged: list = []
    _staged_sink(monkeypatch, staged)
    adapter = FlakyAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(adapter.sent) == 1

    # Reopen: карточку вернули в работу, затем закрыли снова.
    with kb.write_txn(mission_env):
        mission_env.execute("UPDATE tasks SET status='running' WHERE id=?", (kids[1],))
    await _run_one_branch_tick(monkeypatch, runner)  # ветка открыта → сброс маркера
    _complete(mission_env, kids[1], summary="готово со второй попытки")

    adapter.sent.clear()
    adapter.docs.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(adapter.sent) == 1, (
        f"повторное завершение должно дать ровно одно новое сообщение: {adapter.sent}"
    )
    assert "вторая попытка" not in adapter.sent[0]["text"]  # summary корня
    assert [d["file_name"] for d in adapter.docs] == [report.name]


# ---------------------------------------------------------------------------
# Unit: селекторы отчётов (без ложных срабатываний на stage-артефакты)
# ---------------------------------------------------------------------------

def test_report_name_selection_rejects_stage_artifacts():
    from gateway.delivery_engine import _is_final_report_name

    assert _is_final_report_name("sno-economics-report-for-manager-v8")
    assert _is_final_report_name("digests-день-1")
    assert not _is_final_report_name("s1_report")
    assert not _is_final_report_name("s2b-report")
    assert not _is_final_report_name("build_script")
    assert not _is_final_report_name("data")


def test_report_family_dedupe_keeps_newest_revision(tmp_path):
    from gateway.delivery_engine import _dedupe_report_families

    old = tmp_path / "sno-economics-report-v7.html"
    new = tmp_path / "sno-economics-report-v8.html"
    other = tmp_path / "digest-day-1.html"
    for p in (old, new, other):
        p.write_text("x", encoding="utf-8")
    scored = [(3.0, 10, str(old)), (2.0, 10, str(new)), (1.0, 10, str(other))]
    keep = _dedupe_report_families(scored)
    assert [Path(p).name for p in keep] == [old.name, other.name]


# ---------------------------------------------------------------------------
# 8. Логирование события отправки (t_e04a8f44)
#    «в логах есть запись об отправке с результатом» — durable JSON-журнал
#    плюс строка на стандартном логгере, БЕЗ ручного запроса.
# ---------------------------------------------------------------------------

def _journal_events(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_mask_chat_id_keeps_owner_pii_out_of_logs():
    from gateway.delivery_engine import mask_chat_id

    assert mask_chat_id("125805788") == "******788"
    assert "125805788" not in mask_chat_id("125805788")
    assert mask_chat_id("") == "?"
    assert mask_chat_id("tg-owner-125805788") == "tg\u2026"
    assert "125805788" not in mask_chat_id("tg-owner-125805788")


def test_report_type_of_buckets_extensions(tmp_path):
    from gateway.delivery_engine import report_type_of

    assert report_type_of([]) == "none"
    assert report_type_of([str(tmp_path / "a.html")]) == "html"
    assert (
        report_type_of([str(tmp_path / "a.html"), str(tmp_path / "b.md")])
        == "html+markdown"
    )


@pytest.mark.asyncio
async def test_terminal_close_writes_delivery_journal_entry(
    mission_env, tmp_path, monkeypatch, caplog,
):
    journal = tmp_path / "tg-report-delivery.log"
    monkeypatch.setenv("HERMES_REPORT_DELIVERY_LOG", str(journal))
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="контроллер миссии закрыт")
    _complete(mission_env, kids[0], summary="S6: отчёт готов", artifacts=[report])
    _complete(mission_env, kids[1], summary="S7: приёмка")

    _staged_sink(monkeypatch, [])
    adapter = FlakyAdapter()
    with caplog.at_level("INFO"):
        await _run_one_branch_tick(monkeypatch, _make_runner(adapter))

    events = _journal_events(journal)
    assert len(events) == 1, events
    ev = events[0]
    assert ev["event"] == "report_delivery"
    assert ev["task_id"] == root
    assert ev["result"] == "delivered"
    assert ev["report_type"] == "html"
    assert ev["trigger"] == "branch_terminal"
    assert ev["manual"] is False
    assert [Path(p).name for p in ev["artifacts"]] == [report.name]
    assert ev["artifacts_failed"] == []
    assert "125805788" not in ev["chat_id_masked"]
    assert ev["ts"] and ev["text_len"] > 0

    # Тот же факт виден и в обычном логгере, а не только в файле.
    assert any(
        "telegram report delivery" in rec.getMessage()
        and root in rec.getMessage()
        and "result=delivered" in rec.getMessage()
        for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_repeat_completion_does_not_spam_the_journal(
    mission_env, tmp_path, monkeypatch,
):
    journal = tmp_path / "tg-report-delivery.log"
    monkeypatch.setenv("HERMES_REPORT_DELIVERY_LOG", str(journal))
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="закрыто")
    _complete(mission_env, kids[0], summary="отчёт", artifacts=[report])
    _complete(mission_env, kids[1], summary="готово")

    _staged_sink(monkeypatch, [])
    adapter = FlakyAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    await _run_one_branch_tick(monkeypatch, runner)
    await _run_one_branch_tick(monkeypatch, runner)

    events = _journal_events(journal)
    assert len(events) == 1, f"идемпотентность: {events}"


@pytest.mark.asyncio
async def test_failed_send_is_journaled_then_retry_journals_delivery(
    mission_env, tmp_path, monkeypatch,
):
    journal = tmp_path / "tg-report-delivery.log"
    monkeypatch.setenv("HERMES_REPORT_DELIVERY_LOG", str(journal))
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="миссия закрыта")
    _complete(mission_env, kids[0], summary="отчёт", artifacts=[report])
    _complete(mission_env, kids[1], summary="готово")

    _staged_sink(monkeypatch, [])
    adapter = FlakyAdapter()
    adapter.fail_sends = 1
    runner = _make_runner(adapter)

    await _run_one_branch_tick(monkeypatch, runner)
    events = _journal_events(journal)
    assert [e["result"] for e in events] == ["failed"], events
    assert "send_path_degraded" in events[0]["reason"]
    # Недоставленный отчёт не выдаётся за отправленный: artifacts пуст,
    # найденный файл честно помечен как «не загружен».
    assert events[0]["artifacts"] == []
    assert [Path(p).name for p in events[0]["artifacts_not_uploaded"]] == [report.name]

    await _run_one_branch_tick(monkeypatch, runner)
    events = _journal_events(journal)
    assert [e["result"] for e in events] == ["failed", "delivered"], events
    assert [Path(p).name for p in events[1]["artifacts"]] == [report.name]


@pytest.mark.asyncio
async def test_text_only_close_is_journaled_without_artifacts(
    mission_env, tmp_path, monkeypatch,
):
    journal = tmp_path / "tg-report-delivery.log"
    monkeypatch.setenv("HERMES_REPORT_DELIVERY_LOG", str(journal))
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="миссия завершена без отчётного файла")
    for i, c in enumerate(kids):
        _complete(mission_env, c, summary=f"шаг {i}")

    _staged_sink(monkeypatch, [])
    await _run_one_branch_tick(monkeypatch, _make_runner(FlakyAdapter()))

    events = _journal_events(journal)
    assert len(events) == 1, events
    assert events[0]["result"] == "delivered"
    assert events[0]["artifacts"] == []
    assert events[0]["report_type"] == "none"


@pytest.mark.asyncio
async def test_artifact_upload_failure_is_recorded_as_failed_artifact(
    mission_env, tmp_path, monkeypatch,
):
    journal = tmp_path / "tg-report-delivery.log"
    monkeypatch.setenv("HERMES_REPORT_DELIVERY_LOG", str(journal))
    report = _report(tmp_path, "sno-economics-report-for-manager-v8.html")
    root, kids = _mk_mission(mission_env)
    _complete(mission_env, root, summary="закрыто")
    _complete(mission_env, kids[0], summary="отчёт", artifacts=[report])
    _complete(mission_env, kids[1], summary="готово")

    _staged_sink(monkeypatch, [])

    async def _boom(*args, **kwargs):
        raise RuntimeError("document_too_big")

    adapter = FlakyAdapter()
    adapter.send_document = _boom  # type: ignore[assignment]
    await _run_one_branch_tick(monkeypatch, _make_runner(adapter))

    events = _journal_events(journal)
    assert len(events) == 1, events
    # Текст итога ушёл -> delivered, но файл честно помечен как несостоявшийся.
    assert events[0]["result"] == "delivered"
    assert [Path(p).name for p in events[0]["artifacts_failed"]] == [report.name]
    assert events[0]["artifacts"] == []
