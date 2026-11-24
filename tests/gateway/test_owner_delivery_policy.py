"""Owner delivery policy: only decisions and final deliverables reach the owner.

Two layers under test:

* the pure classifier/journal in ``gateway.owner_delivery_policy``;
* the notifier wiring — Telegram subscriptions filtered (event kinds, wake, artifacts,
  summary text) while every other platform keeps its pre-policy behaviour, and
  ``enabled: false`` restoring the old path.
"""

import asyncio
import json
from typing import Optional

import pytest

from gateway.config import Platform
from gateway.owner_delivery_policy import (
    DEFAULT_ARTIFACT_EXTENSIONS,
    OwnerDeliveryConfig,
    classify_kanban_event,
    filter_artifacts,
    record_suppressed,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


# --- pure classifier -----------------------------------------------------------------


def test_intermediate_card_completion_is_not_delivered():
    category, reason = classify_kanban_event(
        kind="completed",
        payload={"artifacts": ["/tmp/report.md"], "summary": "step done"},
        has_open_ancestor=True,
    )
    assert category is None
    assert "intermediate" in reason


def test_root_completion_with_deliverable_and_source_files():
    payload = {
        "artifacts": ["/tmp/report.pdf", "/tmp/notes.md", "/tmp/data.json", "/tmp/gen.py"],
        "summary": "Готов отчёт по конкурсу",
    }
    category, reason = classify_kanban_event(
        kind="completed", payload=payload, has_open_ancestor=False,
    )
    assert category == "final"
    assert "artifact" in reason
    assert filter_artifacts(payload["artifacts"]) == ["/tmp/report.pdf"]


def test_root_completion_without_artifacts_or_summary_is_silent():
    category, reason = classify_kanban_event(kind="completed", payload={}, has_open_ancestor=False)
    assert category is None
    assert reason == "service:empty_completed"


def test_root_completion_with_summary_only_is_final():
    category, _reason = classify_kanban_event(
        kind="completed", payload={"summary": "Сдано, см. доску"}, has_open_ancestor=False,
    )
    assert category == "final"


def test_source_only_artifacts_do_not_make_a_completion_final():
    category, reason = classify_kanban_event(
        kind="completed",
        payload={"artifacts": ["/tmp/a.md", "/tmp/b.py", "/tmp/c.log"]},
        has_open_ancestor=False,
    )
    assert category is None
    assert reason == "service:empty_completed"


@pytest.mark.parametrize("block_kind", ["needs_input", "capability"])
def test_blocked_decision_kinds_reach_the_owner(block_kind):
    category, reason = classify_kanban_event(
        kind="blocked", payload={"kind": block_kind, "reason": "нужен доступ"},
        has_open_ancestor=False,
    )
    assert category == "decision"
    assert block_kind in reason


def test_blocked_dependency_is_service():
    category, _reason = classify_kanban_event(
        kind="blocked", payload={"kind": "dependency"}, has_open_ancestor=False,
    )
    assert category is None


def test_blocked_without_kind_is_a_decision():
    """An untyped block waits for a human: nothing auto-continues it (only ``dependency``)."""
    category, reason = classify_kanban_event(kind="blocked", payload={"reason": "нет доступа"})
    assert category == "decision"
    assert reason == "blocked:untyped"


def test_gave_up_parked_sticky_is_a_decision():
    category, reason = classify_kanban_event(
        kind="gave_up", payload={"error": "budget", "sticky": True},
    )
    assert category == "decision"
    assert reason == "gave_up:sticky_park"


def test_gave_up_without_sticky_stays_service():
    for payload in ({"error": "budget"}, {"error": "budget", "sticky": False}):
        category, reason = classify_kanban_event(kind="gave_up", payload=payload)
        assert category is None
        assert reason == "service:gave_up"


def test_block_loop_detected_needs_input_is_decision():
    category, _ = classify_kanban_event(
        kind="block_loop_detected",
        payload={"kind": "needs_input", "reason": "нет кредов", "recurrences": 3},
    )
    assert category == "decision"


def test_block_loop_detected_other_kind_is_service():
    category, _ = classify_kanban_event(kind="block_loop_detected", payload={"kind": "transient"})
    assert category is None


def test_status_blocked_needs_input_is_decision_but_running_is_not():
    decision, _ = classify_kanban_event(
        kind="status", payload={"status": "blocked", "kind": "needs_input"},
    )
    assert decision == "decision"
    for payload in ({"status": "running"}, {"status": "blocked", "kind": "dependency"}):
        category, _ = classify_kanban_event(kind="status", payload=payload)
        assert category is None


@pytest.mark.parametrize("kind", [
    "crashed", "timed_out", "gave_up", "commented", "review_requested",
    "changes_requested", "archived", "unblocked",
])
def test_service_kinds_never_reach_the_owner(kind):
    category, reason = classify_kanban_event(kind=kind, payload={"summary": "x"}, artifacts=["/tmp/a.pdf"])
    assert category is None
    assert reason.startswith("service:")


def test_filter_artifacts_default_whitelist():
    paths = ["/x/report.pdf", "/x/a.docx", "/x/b.html", "/x/c.png", "/x/d.zip",
             "/x/s.md", "/x/s.json", "/x/s.py", "/x/s.log", "/x/s.diff", "/x/s.jsonl", "/x/s.out"]
    assert filter_artifacts(paths) == ["/x/report.pdf", "/x/a.docx", "/x/b.html", "/x/c.png", "/x/d.zip"]
    assert ".md" not in DEFAULT_ARTIFACT_EXTENSIONS


def test_owner_delivery_config_defaults_and_rollback():
    default = OwnerDeliveryConfig.from_config({})
    assert default.enabled is True
    assert default.covers("telegram") is True
    assert default.covers("local") is False

    off = OwnerDeliveryConfig.from_config({"owner_delivery_policy": {"enabled": False}})
    assert off.covers("telegram") is False

    custom = OwnerDeliveryConfig.from_config({"owner_delivery_policy": {"platforms": ["telegram", "local"]}})
    assert custom.covers("local") is True


def test_record_suppressed_writes_jsonl_with_reason(tmp_path):
    log = tmp_path / "logs" / "delivery-policy-suppressed.log"
    ok = record_suppressed(
        task_id="t_1", event_kind="crashed", platform="telegram", chat_id="chat-1",
        reason="service:crashed", path=log, now=1_700_000_000,
    )
    assert ok is True
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows == [{
        "ts": "2023-11-14T22:13:20Z", "task_id": "t_1", "event_kind": "crashed",
        "platform": "telegram", "chat_id": "chat-1", "reason": "service:crashed",
    }]


# --- notifier wiring -----------------------------------------------------------------


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []
        self.documents = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True

    def extract_local_files(self, text):
        return [], text

    def supports_async_delivery(self):
        return True

    async def send_document(self, chat_id, file_path, metadata=None):
        self.documents.append(file_path)


RecordingAdapter.supports_async_delivery = True


async def _run_one_notifier_tick(monkeypatch, runner, policy):
    """One tick with the owner policy injected (config is otherwise read from disk)."""
    from gateway import kanban_watchers_notifier as notifier

    monkeypatch.setattr(notifier, "_owner_delivery_config", lambda: policy)
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)
    # Collection runs in a worker thread; give its thread nothing to finish but mark done.
    await real_sleep(0)


def _make_runner(adapter, platform=Platform.TELEGRAM):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _policy(enabled=True, platforms=("telegram",)):
    return OwnerDeliveryConfig(enabled=enabled, platforms=tuple(platforms))


def _sub(task_id, platform, chat_id="chat-1", mode="notify+wake"):
    conn = kbc.connect()
    try:
        kbn.add_notify_sub(
            conn, task_id=task_id, platform=platform, chat_id=chat_id, delivery_mode=mode,
        )
    finally:
        conn.close()


def _completed_task(summary="Готово", artifacts=None, *, parent_open=False, platform="telegram"):
    """A card whose ``completed`` event is on the journal.

    The event is appended directly: dependency gating refuses to complete a card that
    still has an open parent, but the notifier's input is the journal, and a handoff can
    land on a card whose parent was reopened afterwards — exactly the intermediate case
    the policy must stay quiet about.
    """
    conn = kbc.connect()
    try:
        if parent_open:
            parent = kb.create_task(conn, title="parent card", assignee="worker")
        tid = kb.create_task(conn, title="child card", assignee="worker")
        if parent_open:
            kb.link_tasks(conn, parent_id=parent, child_id=tid)
        _sub(tid, platform)
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        payload = {"summary": summary} if summary else {}
        if artifacts:
            payload["artifacts"] = list(artifacts)
        kb._append_event(conn, tid, "completed", payload)
        return tid
    finally:
        conn.close()


def _blocked_task(kind: Optional[str] = "needs_input", *, platform="telegram", reason="нужен пароль"):
    """A blocked card + its journalled event; ``kind=None`` is the legacy untyped block."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="blocked card", assignee="worker")
        _sub(tid, platform)
        conn.execute("UPDATE tasks SET status = 'blocked', block_kind = ? WHERE id = ?", (kind, tid))
        payload = {"reason": reason}
        if kind:
            payload["kind"] = kind
        kb._append_event(conn, tid, "blocked", payload)
        return tid
    finally:
        conn.close()


def _suppression_rows(tmp_path):
    log = tmp_path / "home" / "logs" / "delivery-policy-suppressed.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def run_tick(tmp_path, monkeypatch):
    """Run one notifier tick against a temp board + temp log dir, returning the suppression rows."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: home, raising=False,
    )
    kb.init_db()

    def runner_tick(adapter, policy, platform=Platform.TELEGRAM):
        runner = _make_runner(adapter, platform)
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner, policy))
        return runner

    runner_tick.rows = lambda: _suppression_rows(tmp_path)  # type: ignore[attr-defined]
    return runner_tick


def test_intermediate_completed_under_open_parent_is_suppressed_and_journalled(run_tick, tmp_path):
    tid = _completed_task(artifacts=["/tmp/report.md"], parent_open=True)
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())

    assert adapter.sent == [], "an intermediate card's completion must not message the owner"
    assert adapter.handled == [], "and must not wake the creator's session"
    rows = run_tick.rows()
    assert [r["event_kind"] for r in rows] == ["completed"]
    assert rows[0]["task_id"] == tid
    assert rows[0]["platform"] == "telegram"
    assert rows[0]["chat_id"] == "chat-1"
    assert rows[0]["reason"] == "service:intermediate_card"


def test_final_completion_sends_summary_passively_without_wake(run_tick, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    tid = _completed_task(summary="Отчёт по конкурсу готов", artifacts=[str(pdf), "/tmp/notes.md"])
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "Отчёт по конкурсу готов" in text, "a final ping must carry the task summary, not just ✅"
    assert adapter.handled == [], "a final deliverable is passive: no wake turn"
    assert adapter.documents == [str(pdf)], "only the whitelisted .pdf is uploaded"
    assert run_tick.rows() == []


def test_completed_without_summary_or_artifacts_sends_nothing(run_tick):
    _completed_task(summary="", artifacts=None)
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())
    assert adapter.sent == []
    assert [r["reason"] for r in run_tick.rows()] == ["service:empty_completed"]


def test_blocked_needs_input_is_delivered_with_reason_and_wakes_once(run_tick, monkeypatch):
    tid = _blocked_task("needs_input", reason="нужен доступ к хранилищу")
    adapter = RecordingAdapter()
    wakes = []

    from gateway import kanban_watchers_notifier as notifier
    real_wake = notifier._KanbanNotification.wake

    async def counting_wake(self):
        wakes.append(self.task_id)
        return await real_wake(self)

    monkeypatch.setattr(notifier._KanbanNotification, "wake", counting_wake)
    run_tick(adapter, _policy())

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert "нужен доступ к хранилищу" in adapter.sent[0]["text"], "the owner must see the question"
    assert wakes == [tid], "a decision wakes the creator exactly once"
    assert run_tick.rows() == []


def test_blocked_dependency_is_suppressed(run_tick):
    _blocked_task("dependency")
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())
    assert adapter.sent == []
    assert adapter.handled == []
    assert [r["reason"] for r in run_tick.rows()] == ["service:blocked:dependency"]


def test_blocked_untyped_is_delivered_and_wakes(run_tick):
    """A legacy untyped block cannot move without the owner, so it must reach him."""
    tid = _blocked_task(None, reason="ждёт решения владельца")
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())
    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert "ждёт решения владельца" in adapter.sent[0]["text"]
    assert len(adapter.handled) == 1, "a decision hands the turn back to the creator"
    assert run_tick.rows() == [], "a decision is delivered, not journalled as suppressed"


def test_sticky_gave_up_is_delivered_and_wakes(run_tick):
    """A sticky park is held for an operator: nobody resumes it automatically."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="parked card", assignee="worker")
        _sub(tid, "telegram")
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        kb._append_event(
            conn, tid, "gave_up",
            {"error": "бюджет итераций исчерпан", "failures": 3, "sticky": True},
        )
    finally:
        conn.close()
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())
    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert len(adapter.handled) == 1, "the owner has to unstick it: wake the creator"
    assert run_tick.rows() == []


def test_non_sticky_gave_up_is_still_only_a_wake(run_tick):
    """Without ``sticky`` the card is auto-continuable: keep it off the owner's chat."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="flaky card", assignee="worker")
        _sub(tid, "telegram")
        kb._append_event(conn, tid, "gave_up", {"error": "boom", "failures": 3})
    finally:
        conn.close()
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())
    assert adapter.sent == []
    assert [r["event_kind"] for r in run_tick.rows()] == ["gave_up"]


@pytest.mark.parametrize("kind", ["crashed", "timed_out", "gave_up"])
def test_service_failure_events_are_suppressed(run_tick, kind):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="flaky card", assignee="worker")
        _sub(tid, "telegram")
        kb._append_event(conn, tid, kind, {"error": "boom", "failures": 2, "limit_seconds": 60})
    finally:
        conn.close()
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())
    assert adapter.sent == []
    assert adapter.handled == []
    assert [r["event_kind"] for r in run_tick.rows()] == [kind]


def test_commented_and_review_requested_are_suppressed(run_tick):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="review card", assignee="worker")
        _sub(tid, "telegram")
        kb.add_comment(conn, tid, author="worker", body="готово, смотрите")
        kb._append_event(conn, tid, "review_requested", {"summary": "PR ready"})
    finally:
        conn.close()
    adapter = RecordingAdapter()
    run_tick(adapter, _policy())
    assert adapter.sent == []
    assert adapter.handled == []
    assert sorted(r["event_kind"] for r in run_tick.rows()) == ["commented", "review_requested"]


# --- desktop / non-covered platforms keep the old behaviour ---------------------------


@pytest.mark.parametrize("platform,enum", [("local", Platform.LOCAL), ("api_server", Platform.API_SERVER)])
def test_local_platform_completion_delivers_as_before(run_tick, platform, enum):
    md = "/tmp/notes.md"
    _completed_task(artifacts=[md], parent_open=True, platform=platform)
    adapter = RecordingAdapter()

    delivered = {}
    real = GatewayRunner._deliver_kanban_artifacts

    async def spy(self, *, adapter, chat_id, metadata, event_payload, task, relation_task=None, event_kind=""):
        delivered["payload"] = event_payload
        delivered["task"] = task
        return await real(self, adapter=adapter, chat_id=chat_id, metadata=metadata,
                          event_payload=event_payload, task=task,
                          relation_task=relation_task, event_kind=event_kind)

    GatewayRunner._deliver_kanban_artifacts = spy
    try:
        run_tick(adapter, _policy(), platform=enum)
    finally:
        GatewayRunner._deliver_kanban_artifacts = real

    assert len(adapter.sent) == 1, "desktop/local must still get the ping"
    assert len(adapter.handled) == 1, "and still be woken"
    assert delivered["payload"]["artifacts"] == [md], "no extension filter off Telegram"
    assert delivered["task"] is not None
    assert run_tick.rows() == [], "nothing suppressed for a non-covered platform"


@pytest.mark.parametrize("kind", ["crashed", "commented", "review_requested"])
@pytest.mark.parametrize("platform,enum", [("local", Platform.LOCAL), ("api_server", Platform.API_SERVER)])
def test_local_platform_service_events_still_deliver(run_tick, kind, platform, enum):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="desktop card", assignee="worker")
        _sub(tid, platform)
        kb._append_event(conn, tid, kind, {"summary": "x", "author": "w", "preview": "p"})
    finally:
        conn.close()
    adapter = RecordingAdapter()
    run_tick(adapter, _policy(), platform=enum)
    assert len(adapter.sent) == 1
    assert run_tick.rows() == []


@pytest.mark.parametrize("platform,enum", [("local", Platform.LOCAL), ("api_server", Platform.API_SERVER)])
def test_local_platform_review_requested_still_wakes(run_tick, platform, enum):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="desktop review", assignee="worker")
        _sub(tid, platform)
        kb._append_event(conn, tid, "review_requested", {"summary": "PR ready"})
    finally:
        conn.close()
    adapter = RecordingAdapter()
    run_tick(adapter, _policy(), platform=enum)
    assert len(adapter.handled) == 1, "wake is not narrowed off Telegram"


def test_enabled_false_restores_previous_behaviour(run_tick):
    """One-line rollback: ``enabled: false`` delivers every TERMINAL kind again."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="rolled back", assignee="worker")
        _sub(tid, "telegram")
        kb._append_event(conn, tid, "crashed", {"error": "boom"})
        kb._append_event(conn, tid, "commented", {"author": "w", "preview": "p"})
    finally:
        conn.close()
    adapter = RecordingAdapter()
    run_tick(adapter, _policy(enabled=False))

    assert len(adapter.sent) == 2, "without the policy both events are pinged as before"
    assert run_tick.rows() == []


def test_enabled_false_still_wakes_for_completed(run_tick):
    _completed_task(artifacts=["/tmp/report.md"])
    adapter = RecordingAdapter()
    run_tick(adapter, _policy(enabled=False))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1, "the old wake path is untouched when the policy is off"


def test_platforms_list_does_not_affect_other_platforms(run_tick):
    """``platforms: [telegram]`` alone decides the scope — local stays untouched."""
    _completed_task(artifacts=["/tmp/report.md"], parent_open=True, platform="local")
    _completed_task(artifacts=["/tmp/report.md"], parent_open=True, platform="telegram")

    local_adapter = RecordingAdapter()
    run_tick(local_adapter, _policy(), platform=Platform.LOCAL)
    assert len(local_adapter.sent) == 1, "local delivered even though the policy is on for telegram"
    assert run_tick.rows() == [], "a local tick journals nothing"

    telegram_adapter = RecordingAdapter()
    run_tick(telegram_adapter, _policy())
    assert telegram_adapter.sent == [], "the telegram card is held back by the policy"
    assert {r["platform"] for r in run_tick.rows()} == {"telegram"}


def test_both_adapters_in_one_runner_are_scoped_by_subscription_platform(run_tick, monkeypatch):
    """A runner serving every channel must still gate per subscription, not per process."""
    _completed_task(artifacts=["/tmp/notes.md"], parent_open=True, platform="local")
    _completed_task(artifacts=["/tmp/notes.md"], parent_open=True, platform="telegram")

    local_adapter, telegram_adapter = RecordingAdapter(), RecordingAdapter()
    runner = _make_runner(local_adapter, Platform.LOCAL)
    runner.adapters = {Platform.LOCAL: local_adapter, Platform.TELEGRAM: telegram_adapter}  # type: ignore[assignment]
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner, _policy()))

    assert len(local_adapter.sent) == 1, "the desktop subscription keeps its ping"
    assert telegram_adapter.sent == [], "the telegram subscription stays silent"
    assert {r["platform"] for r in run_tick.rows()} == {"telegram"}
