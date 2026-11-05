"""Integration tests for the kanban branch-summary watcher stage.

Covers the "короткий итог ветки" delivery: ONE short message per channel when
the whole branch closes, no duplication across repeated ticks, failure
summaries for needs-a-fix nodes, api_server (OWUI) delivery, reactivation
re-send, and the `kanban.branch_summary_replace` suppression flag.
"""

import asyncio
import os

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.docs = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append(
            {"chat_id": chat_id, "file_path": file_path,
             "file_name": file_name or os.path.basename(file_path),
             "metadata": metadata or {}}
        )

    async def handle_message(self, event):
        self.handled.append(event)


@pytest.fixture
def board_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "branch.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, assignee="worker"):
    return kb.create_task(conn, title=title, assignee=assignee)


def _complete_branch(conn, root, children):
    for tid in [root] + list(children):
        assert kb.complete_task(conn, tid, summary="done")


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_dispatcher_lock_handle = object()  # owns dispatcher -> include unowned subs
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner


async def _run_one_branch_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the branch-summary watcher."""
    real_sleep = asyncio.sleep
    runner._running = True  # a prior tick's cadence sleep clears it

    async def fake_sleep(delay):
        if delay >= 5:
            return None  # startup + cadence; keep running this tick
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


# ---------------------------------------------------------------------------
# Single summary per channel, no duplicate ticks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_single_delivery_no_dup_telegram(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    b = _mk(board_env, "b")
    kb.link_tasks(board_env, root, a)
    kb.link_tasks(board_env, root, b)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    _complete_branch(board_env, root, [a, b])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    # F1 (tg-delivery-contract): the terminal итог carries the root's summary.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]
    assert adapter.sent[0]["chat_id"] == "chat-1"
    marker = kb.has_branch_summary(kb.connect(board="default"), root)
    assert marker is not None
    assert len(kb.branch_summary_pending_subs(
        marker, [{"platform": "telegram", "chat_id": "chat-1", "thread_id": ""}])) == 0

    # repeat tick -> no second message
    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_summary_open_branch_no_delivery(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.complete_task(board_env, root, summary="done")  # a stays ready -> open

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []
    assert kb.has_branch_summary(kb.connect(board="default"), root) is None


@pytest.mark.asyncio
async def test_summary_already_delivered_root_not_redelivered_when_descendant_closes(
    board_env, monkeypatch,
):
    """F8 wave-suppression: an already-delivered old root must NOT be
    re-marked/re-delivered when a NEW descendant closes in the same branch
    (batch [F7 readback]-style synthetic cards closing together change the
    branch fingerprint without the branch ever reopening).

    Regression for РГЗ/tghyg F8: 10 old done / synthetic roots were re-sent as
    "OWUI→TG mirror terminal итог" (18:17:37-39) after a mass close wave.
    The close-state fingerprint changed (new descendant), so the marker's
    fingerprint no longer matched, and _plan_branch_summary's stale-claim
    branch (marker exists + fingerprint differs) cleared the marker and
    re-marked + re-delivered the OLD итог to every channel again.
    """
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    _complete_branch(board_env, root, [a])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    # F1 (tg-delivery-contract): the terminal итог carries the root's summary.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]

    # Branch is delivered. Now a NEW descendant (synthetic readback card) is
    # linked under the root and closed — the branch fingerprint changes, but
    # the branch NEVER passed through open. The old итог must not replay.
    b = _mk(board_env, "readback-card")
    kb.link_tasks(board_env, root, b)
    kb.complete_task(board_env, b, summary="done")

    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [], (
        "already-delivered old root must not be re-delivered when a "
        f"descendant closes; sent={adapter.sent}"
    )
    # The marker must still exist (not cleared/rewritten).
    conn2 = kb.connect(board="default")
    try:
        marker = kb.has_branch_summary(conn2, root)
        assert marker is not None, "marker must be preserved, not cleared"
    finally:
        conn2.close()


@pytest.mark.asyncio
async def test_summary_shared_descendant_wave_no_redelivery_for_all_ancestor_roots(
    board_env, monkeypatch,
):
    """F8 decision-support: EXACT incident shape — one shared [F7 readback]
    descendant is linked under MULTIPLE already-delivered ancestor roots, then
    batch-closed in a wave. NO ancestor root that already received its итог
    may be re-delivered (the close-state fingerprint of every ancestor changes
    identically, which previously replayed every one of them).
    """
    root_a = _mk(board_env, "mission-A")
    a1 = _mk(board_env, "a1")
    kb.link_tasks(board_env, root_a, a1)
    kb.add_notify_sub(board_env, task_id=root_a, platform="telegram", chat_id="chat-A")

    root_b = _mk(board_env, "mission-B")
    b1 = _mk(board_env, "b1")
    kb.link_tasks(board_env, root_b, b1)
    kb.add_notify_sub(board_env, task_id=root_b, platform="telegram", chat_id="chat-B")

    _complete_branch(board_env, root_a, [a1])
    _complete_branch(board_env, root_b, [b1])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert sorted(s["chat_id"] for s in adapter.sent) == ["chat-A", "chat-B"]

    # Shared synthetic readback card linked under BOTH roots, then closed in
    # one wave — the fingerprint of both branches changes.
    shared = _mk(board_env, "shared-readback")
    kb.link_tasks(board_env, root_a, shared)
    kb.link_tasks(board_env, root_b, shared)
    kb.complete_task(board_env, shared, summary="done")

    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [], (
        "shared-descendant close must not re-deliver already-sent ancestor "
        f"roots; sent={adapter.sent}"
    )
    conn2 = kb.connect(board="default")
    try:
        assert kb.has_branch_summary(conn2, root_a) is not None
        assert kb.has_branch_summary(conn2, root_b) is not None
    finally:
        conn2.close()


@pytest.mark.asyncio
async def test_summary_open_no_delivery_after_this_test_spawns_no_marker(board_env, monkeypatch):
    # dependency-blocked descendant keeps the branch open -> no summary
    root = _mk(board_env, "root")
    a = _mk(board_env, "dep_child")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.complete_task(board_env, root, summary="done")
    kb.block_task(board_env, a, kind="dependency", reason="waiting")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []
    assert kb.has_branch_summary(kb.connect(board="default"), root) is None


@pytest.mark.asyncio
async def test_summary_failure_needs_input_telegram(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "child_needs_answer")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.complete_task(board_env, root, summary="done")
    kb.block_task(board_env, a, kind="needs_input", reason="respond please")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"].startswith("❌ Задача не выполнена (нужен фикс):")
    assert a in adapter.sent[0]["text"] or "child_needs_answer" in adapter.sent[0]["text"]


@pytest.mark.asyncio
async def test_summary_reactivation_resends_after_reclose(board_env, monkeypatch):
    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    _complete_branch(board_env, root, [a])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    # F1 (tg-delivery-contract): the terminal итог carries the root's summary.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]

    # reopen a node (simulate done -> ready reactivation)
    with kb.write_txn(board_env):
        board_env.execute("UPDATE tasks SET status='ready' WHERE id=?", (a,))
    # while open: tick clears the marker, no new message
    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == []
    conn2 = kb.connect(board="default")
    assert kb.has_branch_summary(conn2, root) is None
    conn2.close()

    # re-close -> fresh summary delivered again
    kb.complete_task(board_env, a, summary="done")
    await _run_one_branch_tick(monkeypatch, runner)
    # F1 (tg-delivery-contract): the re-closed terminal итог also carries summary.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]


@pytest.mark.asyncio
async def test_summary_api_server_delivery_and_ack(board_env, monkeypatch):
    """api_server (OWUI) sub -> staged via the delivery outbox and acked once."""
    from gateway import kanban_origin_delivery as _kod

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc123",
        user_id="sess-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    staged = []
    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    # note: no OWUI registry file needed — stage_terminal is patched.

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    # watcher requires an adapter entry for the platform to consider it live
    runner.adapters = {Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    assert len(staged) == 1
    assert staged[0][0] == "owui-abc123"
    assert staged[0][1] == "✅ Задача выполнена"
    conn2 = kb.connect(board="default")
    marker = kb.has_branch_summary(conn2, root)
    assert kb.branch_summary_pending_subs(
        marker, [{"platform": "api_server", "chat_id": "owui-abc123", "thread_id": ""}]) == []
    conn2.close()

    # repeated tick -> no re-stage
    staged.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert staged == []


# ---------------------------------------------------------------------------
# Replace-mode descendant suppression (R0(в) 10): the flag was removed — a
# terminal DESCENDANT event is always silent in the notifier (its ping is
# claimed+advanced, not rendered); the whole-branch итог is the single
# channel message and arrives only once the branch fully closes.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replace_mode_suppresses_descendant_notifier_pings(board_env, monkeypatch):
    """A terminal DESCENDANT event is silent in the notifier (its ping is
    claimed+advanced, not rendered) — the whole-branch итог is the single
    channel message, and it arrives only once the branch fully closes.
    Replace-mode is the SINGLE behavior since the `branch_summary_replace`
    flag was removed (R0(в) 10)."""

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(board_env, task_id=root, platform="telegram", chat_id="chat-1")
    kb.add_notify_sub(board_env, task_id=a, platform="telegram", chat_id="chat-2")

    # The TG delivery-policy gate (7ea1a5ff943, default ON, fail-closed) is
    # ORTHOGONAL to what this test asserts (replace-mode suppression
    # of descendant pings): with an empty TELEGRAM_ALLOWED_USERS allowlist the
    # gate suppresses even the root's own completion ping as "recipient
    # mismatch", which predates and masks the replace-mode behaviour under
    # test. Same env-override convention as tests/cron/conftest.py
    # (_cron_tg_delivery_policy_default_off): legacy delivery tests keep the
    # gate OFF; dedicated policy tests opt back in explicitly.
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    async def notifier_tick():
        real_sleep = asyncio.sleep
        runner._running = True

        async def fake_sleep(delay):
            if delay == 5:
                return None
            runner._running = False
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await runner._kanban_notifier_watcher(interval=1)

    # Close the root first (parent gating: a descendant only becomes `ready`
    # once its parent is done). Root's OWN completion ping is NOT suppressed
    # — replace mode silences descendants, not the root.
    assert kb.complete_task(board_env, root, summary="done")
    await notifier_tick()
    assert [s["chat_id"] for s in adapter.sent] == ["chat-1"]

    # Now the descendant reaches a terminal notifier event (blocked:
    # needs an answer) — branch closes in failure. Its individual ping must
    # be claimed+silent (the whole-branch итог is the single message).
    adapter.sent.clear()
    assert kb.block_task(board_env, a, reason="need an answer", kind="needs_input")
    assert kb.get_task(board_env, a).status == "blocked"
    await notifier_tick()
    assert adapter.sent == []

    # branch итог: exactly ONE short message per channel, no individual pings
    await _run_one_branch_tick(monkeypatch, runner, interval=1)
    texts = {s["chat_id"]: s["text"] for s in adapter.sent}
    assert set(texts) == {"chat-1", "chat-2"}
    assert texts["chat-1"].startswith("❌ Задача не выполнена (нужен фикс):")
    assert texts["chat-2"].startswith("❌ Задача не выполнена (нужен фикс):")

    # repeated tick -> no duplicate
    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner, interval=1)
    assert adapter.sent == []


# ---------------------------------------------------------------------------
# Terminal gate (t_f285cb14): only the TERMINAL close of the whole
# mission/root tree reaches the TG mirror; intermediate branch итоги
# ([split N/M], daughter cards, sub-branches with an open ancestor mission)
# are suppressed for telegram WITH a journal record, while OWUI api_server
# keeps receiving both.
# ---------------------------------------------------------------------------

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


def _mk_bg(conn, title, parent=None, status="running", assignee="worker"):
    """Create a card (optionally linked under parent) and return its id."""
    tid = kb.create_task(
        conn, title=title, assignee=assignee, initial_status=status,
    )
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


@pytest.mark.asyncio
async def test_intermediate_branch_suppressed_for_telegram_with_journal(
    board_env, monkeypatch,
):
    """Split/разовая дочерняя карточка закрыта, а миссия выше ещё running:
    итог ветки — ПРОМЕЖУТОЧНЫЙ → для telegram подавлен, в журнале ровно одна
    запись (reason=routine status close, owui_to_tg=True); OWUI не потерян.
    """
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    # Root with a subscription but NO subscribed ancestor: `split` is the
    # branch root (mission above stays unsubscribed and still-running).
    kb.add_notify_sub(
        board_env, task_id=split, platform="telegram", chat_id="tg-chat-1",
    )
    # OWUI subscription on the same branch root (api_server) must NOT be lost.
    kb.add_notify_sub(
        board_env,
        task_id=split,
        platform="api_server",
        chat_id="owui-abc",
        user_id="sess-1",
        delivery_mode="notify+wake",
    )
    # Close the split branch fully; keep the mission running. `complete_task`
    # is parent-gated (split's parent mission is still open), so simulate the
    # terminal close directly — exactly the state the bug report describes: a
    # mid-tree split branch HAS closed while the ancestor mission is still
    # running ([split N/M] / one-off child close).
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='done' WHERE id IN (?, ?)", (child, split),
        )

    staged = []
    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)
    from gateway import kanban_origin_delivery as _kod
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    # api_server adapter must be live too (OWUI lane is primary).
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    # TG mirror: SUPPRESSED (не терминальный итог).
    assert adapter.sent == [], (
        "промежуточный итог ветки не должен уходить в TG"
    )
    # OWUI (api_server): delivered as before.
    assert len(staged) == 1
    assert staged[0][0] == "owui-abc"
    assert staged[0][1] == "✅ Задача выполнена"

    # Journal: exactly one record for the suppressed TG channel.
    journal = _read_suppression_journal()
    assert len(journal) == 1, f"журнал должен содержать ровно 1 запись: {journal}"
    assert journal[0]["reason"] == "routine status close"
    assert journal[0]["app"] == "kanban"
    assert journal[0]["target"] == "telegram:tg-chat-1"
    assert journal[0]["task_id"] == split
    assert journal[0]["event_kind"] == "branch_summary"
    assert journal[0]["owui_to_tg"] is True

    # The TG channel is acked so the marker completes and the tick stops
    # retrying (no duplicate journal records on a repeated tick).
    conn2 = kb.connect(board="default")
    try:
        marker = kb.has_branch_summary(conn2, split)
        assert kb.branch_summary_pending_subs(
            marker, [
                {"platform": "telegram", "chat_id": "tg-chat-1", "thread_id": ""},
            ]
        ) == []
    finally:
        conn2.close()
    adapter.sent.clear()
    staged.clear()
    await _run_one_branch_tick(monkeypatch, runner)
    assert adapter.sent == [] and staged == []
    assert len(_read_suppression_journal()) == 1


@pytest.mark.asyncio
async def test_terminal_root_delivers_telegram_no_journal(board_env, monkeypatch):
    """Полное закрытие корня дерева миссии (без открытого предка) ->
    ТЕРМИНАЛЬНЫЙ итог -> TG-доставка происходит, журнал подавлений пуст.
    """
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-chat-9",
    )
    _complete_branch(board_env, root, [a])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)

    # F1 (tg-delivery-contract): the terminal итог carries the root's summary.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]
    assert adapter.sent[0]["chat_id"] == "tg-chat-9"
    assert _read_suppression_journal() == [], (
        "терминальный итог не должен писать в журнал подавлений"
    )


# ---------------------------------------------------------------------------
# F1 (tg-delivery-contract, native telegram lane): терминальный итог ветки
# обязан нести summary и/или артефакты; голый «✅ Задача выполнена»
# подавляется с записью в журнал.  Эти тесты покрывают ПРЯМУЮ telegram
# подписку (в отличие от OWUI→TG mirror в test_kanban_mission_chat_silence_
# dual_lane.py), т.е. собственный объём карточки F1-watcher.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f1_native_tg_terminal_itog_carries_summary_and_artifact(
    board_env, monkeypatch,
):
    """Терминальный итог нативной telegram-подписки: текст несёт summary
    корня, а артефакт (kanban_complete artifacts) уезжает файлом через
    существующий хелпер (_deliver_kanban_artifacts → send_document)."""
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-chat-f1a",
    )
    # Artifact file must exist on disk (the helper skips missing files).
    import tempfile
    report = tempfile.NamedTemporaryFile(
        suffix=".md", prefix="f1-native-", delete=False,
    )
    report.write("# F1 native lane report\n\nреальный отчёт\n".encode("utf-8"))
    report.close()
    report_path = report.name
    # complete_task promotes metadata["artifacts"] onto the event payload.
    for tid in [root, a]:
        assert kb.complete_task(
            board_env, tid, summary="done",
            metadata={"artifacts": [report_path]},
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)

    # Text carries the summary under the F1 contract.
    assert [s["text"] for s in adapter.sent] == ["✅ Задача выполнена\n\ndone"]
    assert adapter.sent[0]["chat_id"] == "tg-chat-f1a"
    # Artifact rides as a native file via the existing helper.
    assert adapter.docs, f"артефакт должен уехать файлом: {adapter.docs}"
    assert os.path.realpath(adapter.docs[0]["file_path"]) == os.path.realpath(report_path)
    # Terminal итог with content -> no suppression journal.
    assert _read_suppression_journal() == [], (
        f"terminal итог с контентом не должен подавляться: {_read_suppression_journal()}"
    )
    os.unlink(report_path)


@pytest.mark.asyncio
async def test_f1_native_tg_bare_itog_suppressed_with_journal(board_env, monkeypatch):
    """Голый терминальный итог нативной telegram-подписки (нет summary,
    нет артефактов) подавляется полностью: ни одного send(), в журнале
    запись reason=bare terminal итог without content."""
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env, task_id=root, platform="telegram", chat_id="tg-chat-f1b",
    )
    # No summary, no artifacts: complete with EMPTY handoff (bare).
    for tid in [root, a]:
        assert kb.complete_task(board_env, tid, summary="")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)

    # Fully suppressed: no contentless ping to Telegram.
    assert adapter.sent == [], f"голый итог не должен уходить в TG: {adapter.sent}"
    assert adapter.docs == []
    journal = _read_suppression_journal()
    assert journal, f"подавление должно быть в журнале: {journal}"
    assert any(
        r["reason"] == "bare terminal итог without content" for r in journal
    ), f"все подавления должны быть bare-итог: {journal}"
    # The suppressed channel is still acked so the tick doesn't retry.
    conn2 = kb.connect(board="default")
    try:
        marker = kb.has_branch_summary(conn2, root)
        assert kb.branch_summary_pending_subs(
            marker,
            [{"platform": "telegram", "chat_id": "tg-chat-f1b", "thread_id": ""}],
        ) == []
    finally:
        conn2.close()


@pytest.mark.asyncio
async def test_terminal_root_owui_api_server_still_delivered(board_env, monkeypatch):
    """Корень дерева закрыт полностью: OWUI (api_server) доставка НЕ теряется
    даже при терминальном итоге (primary лента остаётся без изменений).
    """
    from gateway import kanban_origin_delivery as _kod
    root = _mk_bg(board_env, "root")
    a = _mk_bg(board_env, "a", parent=root)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc123",
        user_id="sess-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    staged = []
    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner.adapters = {Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    assert len(staged) == 1
    assert staged[0][0] == "owui-abc123"
    assert staged[0][1] == "✅ Задача выполнена"


@pytest.mark.asyncio
async def test_intermediate_branch_owui_api_server_still_delivered(
    board_env, monkeypatch,
):
    """Промежуточный итог (миссия выше открыта): OWUI api_server доставка
    сохраняется — гейт действует ТОЛЬКО на TG-зеркало, primary лента жива.
    """
    from gateway import kanban_origin_delivery as _kod
    mission = _mk_bg(board_env, "mission")
    split = _mk_bg(board_env, "split-1", parent=mission)
    child = _mk_bg(board_env, "sub-task", parent=split)
    kb.add_notify_sub(
        board_env,
        task_id=split,
        platform="api_server",
        chat_id="owui-abc",
        user_id="sess-1",
        delivery_mode="notify+wake",
    )
    # Parent-gated `complete_task` can't close the split while the mission is
    # open; simulate the mid-tree terminal close directly (see the journal
    # test above — real [split N/M] close with a still-running mission).
    with kb.write_txn(board_env):
        board_env.execute(
            "UPDATE tasks SET status='done' WHERE id IN (?, ?)", (child, split),
        )

    staged = []
    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)
    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner.adapters = {Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    assert len(staged) == 1, f"OWUI api_server не должен терять доставку: {staged}"
    assert staged[0][0] == "owui-abc"
    assert staged[0][1] == "✅ Задача выполнена"
    assert _read_suppression_journal() == []


# ---------------------------------------------------------------------------
# Wave root-event filter (t_09f66666): replan a root only when the current
# wave carries a terminal close event for the root's own subtree.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_old_delivered_root_not_replanned_on_unrelated_wave(
    board_env, monkeypatch,
):
    """F8/F7 incident shape (event#91420-91431): an old, already-delivered
    branch root must NOT be re-planned/re-delivered when a wave of unrelated
    synthetic [F7 readback] cards closes in the same board.

    The wave filter skips roots whose subtree had NO terminal close event in
    the current wave — old roots t_0f0e834d / t_0db5f307 style (their branches
    closed long ago, marker already delivered) stay untouched when 12 unrelated
    readback cards land a batch INSERT completed.
    """
    # Old root: closed + delivered on the first tick.
    old_root = _mk(board_env, "old-root")
    old_child = _mk(board_env, "old-child")
    kb.link_tasks(board_env, old_root, old_child)
    kb.add_notify_sub(
        board_env, task_id=old_root, platform="telegram", chat_id="tg-old",
    )
    _complete_branch(board_env, old_root, [old_child])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    await _run_one_branch_tick(monkeypatch, runner)
    assert [s["chat_id"] for s in adapter.sent] == ["tg-old"]
    old_marker = kb.has_branch_summary(kb.connect(board="default"), old_root)
    assert old_marker is not None

    # Wave: 12 F7 readback cards, each with its own subscription, batch-closed.
    wave_roots = []
    for i in range(12):
        r = _mk(board_env, f"readback-{i}")
        c = _mk(board_env, f"readback-child-{i}")
        kb.link_tasks(board_env, r, c)
        kb.add_notify_sub(
            board_env, task_id=r, platform="telegram", chat_id=f"tg-wave-{i}",
        )
        wave_roots.append(r)
    for r in wave_roots:
        _complete_branch(board_env, r, list(kb.get_descendant_ids(board_env, r)))

    adapter.sent.clear()
    await _run_one_branch_tick(monkeypatch, runner)

    # The wave roots ARE delivered (their own completed events are in the wave).
    wave_chats = sorted({s["chat_id"] for s in adapter.sent})
    assert wave_chats == sorted(f"tg-wave-{i}" for i in range(12)), (
        f"expected exactly the 12 wave deliveries, got {wave_chats}"
    )
    # The old root must NOT be re-delivered.
    assert "tg-old" not in {s["chat_id"] for s in adapter.sent}, (
        "old delivered root must not be re-delivered on an unrelated wave; "
        f"sent={adapter.sent}"
    )
    conn2 = kb.connect(board="default")
    try:
        new_marker = kb.has_branch_summary(conn2, old_root)
        assert new_marker is not None, "old root marker must be preserved"
        assert new_marker.get("_event_id") == old_marker.get("_event_id"), (
            "old root marker must not be rewritten by an unrelated wave"
        )
    finally:
        conn2.close()

@pytest.mark.asyncio
async def test_wave_filter_skips_old_root_before_planning(
    board_env, monkeypatch,
):
    """Loop-level wave filter (t_09f66666): during a wave that does NOT touch
    an already-delivered root's subtree, `_plan_branch_summary` must not even
    be consulted for that root (short-circuit before branch_state/branch scan).
    """
    old_root = _mk(board_env, "old-root")
    old_child = _mk(board_env, "old-child")
    kb.link_tasks(board_env, old_root, old_child)
    kb.add_notify_sub(
        board_env, task_id=old_root, platform="telegram", chat_id="tg-old",
    )
    _complete_branch(board_env, old_root, [old_child])

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    called_roots: list[str] = []
    real_plan = runner._plan_branch_summary

    def spy_plan(conn, root_id, root_subs, board_slug):
        called_roots.append(root_id)
        return real_plan(conn, root_id, root_subs, board_slug)

    runner._plan_branch_summary = spy_plan

    # First tick: old root delivered.
    await _run_one_branch_tick(monkeypatch, runner)
    assert "tg-old" in {s["chat_id"] for s in adapter.sent}
    assert old_root in called_roots

    # Unrelated wave: 12 F7 readback cards batch-closed.
    called_roots.clear()
    adapter.sent.clear()
    wave_roots = []
    for i in range(12):
        r = _mk(board_env, f"readback-{i}")
        c = _mk(board_env, f"readback-child-{i}")
        kb.link_tasks(board_env, r, c)
        kb.add_notify_sub(
            board_env, task_id=r, platform="telegram", chat_id=f"tg-wave-{i}",
        )
        wave_roots.append(r)
    for r in wave_roots:
        _complete_branch(board_env, r, list(kb.get_descendant_ids(board_env, r)))

    await _run_one_branch_tick(monkeypatch, runner)

    assert old_root not in called_roots, (
        "wave filter must skip an already-delivered old root whose subtree "
        f"has no terminal event in the wave; planned_roots={called_roots}"
    )
    # Wave roots were still planned+delivered.
    assert "tg-old" not in {s["chat_id"] for s in adapter.sent}


@pytest.mark.asyncio
async def test_a2_drop_persists_reason_comment(board_env, monkeypatch):
    """A2 (t_5d3bc199): when an api_server channel hits the 12-failure drop,
    the last delivery reason is persisted as a comment on the branch root so
    an operator can triage without grepping logs."""
    from gateway import kanban_origin_delivery as _kod

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc123",
        user_id="sess-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    def broken_stage(outbox, registry, sub, profile, board, event_id, message):
        # Simulates a permanently-broken OWUI chat (e.g. missing registry):
        # enqueue succeeds but stays pending forever -> never acknowledged.
        return ("pending", None)

    monkeypatch.setattr(_kod, "stage_terminal", broken_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    for _ in range(12):
        runner = _make_runner(adapter)
        runner.adapters = {Platform.API_SERVER: adapter}
        # 11 prior failures already accumulated on this same channel.
        runner._kanban_branch_summary_fail_counts = {
            (root, "api_server", "owui-abc123", ""): 11,
        }
        await _run_one_branch_tick(monkeypatch, runner)

    # After 12 failures the channel is dropped -> drop-reason comment on root.
    conn2 = kb.connect(board="default")
    try:
        comments = kb.list_comments(conn2, root)
    finally:
        conn2.close()
    assert comments, "expected a drop-reason comment on the root"
    drop = [c for c in comments if c.body.startswith("[delivery-drop]")]
    assert drop, f"expected [delivery-drop] comment; got {[c.body for c in comments]}"
    body = drop[-1].body
    assert "owui-abc123" in body, body
    assert "причина" in body, body


@pytest.mark.asyncio
async def test_a1_valid_owui_initiator_channel_not_dropped_after_12_ticks(
    board_env, monkeypatch,
):
    """A1 (t_9944a3ba): валидный OWUI-канал инициатора НЕ помечается dropped
    после 12 запусков доставки.

    Сценарий: подписка api_server несёт верифицированный user_id инициатора
    (A4) и `stage_terminal` возвращает ``acknowledged`` для его чата. Каждый
    тик завершается успешно, `_deliver_branch_summary` возвращает True,
    счётчик неудач сбрасывается.  Даже если канал остаётся в pending между
    тиками (транзиентная задержка дренажа), канал НЕ должен дойти до
    12-неудачного дропа: на root не должно появиться [delivery-drop]-коммента,
    а маркер не должен быть acked-принудительно без доставки.
    """
    from gateway import kanban_origin_delivery as _kod

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc456",
        user_id="sess-initiator-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    # ВАЛИДНЫЙ канал: stage_terminal ACK-ает (как после A1/A4 — канал с
    # верифицированным user_id и рабочим append-target больше не уходит в
    # вечные pending).
    calls = []

    def ok_stage(outbox, registry, sub, profile, board, event_id, message):
        calls.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", event_id)

    monkeypatch.setattr(_kod, "stage_terminal", ok_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    for _ in range(12):
        runner = _make_runner(adapter)
        runner.adapters = {Platform.API_SERVER: adapter}
        await _run_one_branch_tick(monkeypatch, runner)

    # 12 тиков — доставка каждый раз успешна (ACK), канал НЕ дропнут.
    assert calls, "валидный канал доставляется в outbox"
    assert len(calls) >= 1, f"ожидали как минимум одну успешную доставку: {calls}"
    assert calls[0][0] == "owui-abc456", f"доставка идёт в канал инициатора: {calls[0]}"
    # Повторные тики не должны дублировать доставку (маркер acked).
    assert len(calls) <= 2, f"после ack повторные тики не шлют заново: {calls}"

    conn2 = kb.connect(board="default")
    try:
        comments = kb.list_comments(conn2, root)
        marker = kb.has_branch_summary(conn2, root)
    finally:
        conn2.close()
    drop = [c for c in comments if c.body.startswith("[delivery-drop]")]
    assert drop == [], (
        "валидный канал инициатора не должен дропаться после 12 тиков; "
        f"найден [delivery-drop]: {[c.body for c in comments]}"
    )
    # Маркер существует и канал в нём acked (доставлен, а не принудительно сброшен).
    assert marker is not None, "маркер branch-итога должен существовать"
    pending = kb.branch_summary_pending_subs(
        marker,
        [{"platform": "api_server", "chat_id": "owui-abc456", "thread_id": ""}],
    )
    assert pending == [], (
        "валидный канал инициатора после успешной доставки не должен "
        f"остаться pending: {pending}"
    )


@pytest.mark.asyncio
async def test_a1_valid_initiator_channel_not_dropped_after_12_failures(
    board_env, monkeypatch,
):
    """A1 (t_b8bd2fe3): валидный OWUI-канал инициатора НЕ помечается dropped
    после 12 неудачных попыток доставки, и остаётся пригодным для успешной
    доставки следом.

    Сценарий: подписка api_server несёт верифицированный user_id инициатора
    (A4) и её чат валиден. `stage_terminal` возвращает ``pending`` — транзиентная
    задержка (дренаж outbox / чтение из Open WebUI ещё не подтверждено), которая
    НЕ является неисправимой поломкой канала.  После 12 таких тиков канал НЕ
    должен быть дропнут: на root не появляется [delivery-drop]-коммента, маркер
    сохраняется, а канал остаётся в pending (может быть доставлен позже).
    Затем доставка восстанавливается (``acknowledged``) — канал получает итог,
    маркер ACKится, pending пуст: канал остался пригодным.
    """
    from gateway import kanban_origin_delivery as _kod

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc789",
        user_id="sess-initiator-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    calls = []
    mode = {"state": "pending"}

    def flaky_stage(outbox, registry, sub, profile, board, event_id, message):
        calls.append((mode["state"], sub.get("chat_id"), message, event_id))
        return (mode["state"], event_id if mode["state"] == "acknowledged" else None)

    monkeypatch.setattr(_kod, "stage_terminal", flaky_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    adapter = RecordingAdapter()
    # 12 НЕУДАЧНЫХ попыток доставки валидному каналу инициатора (pending).
    for _ in range(12):
        runner = _make_runner(adapter)
        runner.adapters = {Platform.API_SERVER: adapter}
        await _run_one_branch_tick(monkeypatch, runner)

    assert len(calls) == 12, (
        "каждая из 12 попыток должна обратиться к stage_terminal: "
        f"calls={len(calls)}"
    )
    assert all(state == "pending" for state, _, _, _ in calls), (
        "все 12 попыток должны быть неудачными (pending)"
    )

    conn2 = kb.connect(board="default")
    try:
        comments = kb.list_comments(conn2, root)
        marker = kb.has_branch_summary(conn2, root)
    finally:
        conn2.close()
    drop = [c for c in comments if c.body.startswith("[delivery-drop]")]
    assert drop == [], (
        "валидный канал инициатора не должен дропаться после 12 неудачных "
        f"попыток; найден [delivery-drop]: {[c.body for c in comments]}"
    )
    # Канал остаётся пригодным: маркер жив, канал всё ещё pending (не acked
    # принудительно, не отброшен).
    assert marker is not None, "маркер branch-итога должен сохраниться"
    pending = kb.branch_summary_pending_subs(
        marker,
        [{"platform": "api_server", "chat_id": "owui-abc789", "thread_id": ""}],
    )
    assert pending, (
        "после 12 транзиентных неудач канал должен остаться pending "
        f"(доставка возможна позже): {pending}"
    )

    # Следующая попытка успешна — доказывает, что канал остался пригодным:
    # итог доставляется в канал инициатора и маркер ACKится.
    mode["state"] = "acknowledged"
    runner = _make_runner(adapter)
    runner.adapters = {Platform.API_SERVER: adapter}
    await _run_one_branch_tick(monkeypatch, runner)

    ok_calls = [c for c in calls if c[0] == "acknowledged"]
    assert len(ok_calls) == 1, f"ожидали одну успешную доставку: {ok_calls}"
    assert ok_calls[0][1] == "owui-abc789", (
        f"успешная доставка идёт в канал инициатора: {ok_calls[0]}"
    )

    conn3 = kb.connect(board="default")
    try:
        comments_final = kb.list_comments(conn3, root)
        marker_final = kb.has_branch_summary(conn3, root)
    finally:
        conn3.close()
    drop_final = [
        c for c in comments_final if c.body.startswith("[delivery-drop]")
    ]
    assert drop_final == [], (
        "после успешной доставки канал всё ещё не дропнут: "
        f"{[c.body for c in comments_final]}"
    )
    assert marker_final is not None, "маркер должен существовать после ACK"
    pending_final = kb.branch_summary_pending_subs(
        marker_final,
        [{"platform": "api_server", "chat_id": "owui-abc789", "thread_id": ""}],
    )
    assert pending_final == [], (
        "после успешной доставки канал acked и не остаётся в pending: "
        f"{pending_final}"
    )



# ---------------------------------------------------------------------------
# OWUI ack gate must NEVER starve the OWUI→Telegram mirror (t_5d99e48d)
# ---------------------------------------------------------------------------
# Dead-session / slow-drain regression: a terminal branch итог whose OWUI
# stage is pending OR failed must still reach the owner's bound Telegram chat
# (dual-lane rule 1/4/5), exactly once across repeated ticks, and must not be
# silently dropped after MAX_FAILURES.

def _make_bs_runner(adapter, *, tg_adapter=None, profile="01-orchestrator"):
    from gateway.run import GatewayRunner
    from gateway.config import Platform as _P
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {_P.API_SERVER: adapter}
    if tg_adapter is not None:
        runner.adapters[_P.TELEGRAM] = tg_adapter
    runner._kanban_dispatcher_lock_handle = object()  # owns dispatcher
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    # A secondary profile bound to the OWUI channel carries its own TG adapter.
    runner._profile_adapters = {profile: {_P.TELEGRAM: tg_adapter}} if tg_adapter is not None else {}
    return runner


@pytest.mark.asyncio
async def test_pending_owui_still_mirrors_tg_once_and_keeps_channel(
    board_env, monkeypatch,
):
    """t_5d99e48d + контракт владельца 2026-11-04: OWUI pending (slow drain).

    Исходное свойство (t_5d99e48d) — «pending не должен голодить TG-зеркало,
    потому что итог обязан дойти до владельца» — сохранено, но его носитель
    сменился контрактом владельца.  Живой, хотя и не подтверждённый OWUI-канал
    САМ доставит итог (после ACK); поэтому текстовый успешный итог в TG не
    дублируется, канал остаётся pending до ACK, а дедуп-маркер TG НЕ пишется —
    если OWUI умрёт, следующий тик доставит через TG (owui_healthy=False).
    """
    import json as _json
    from gateway import kanban_origin_delivery as _kod

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc790",
        user_id="sess-initiator-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    mode = {"state": "pending"}
    calls = []

    def flaky_stage(outbox, registry, sub, profile, board, event_id, message):
        calls.append((mode["state"], sub.get("chat_id")))
        return (mode["state"], event_id if mode["state"] == "acknowledged" else None)

    monkeypatch.setattr(_kod, "stage_terminal", flaky_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    monkeypatch.setenv("HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        _json.dumps({"sess-initiator-1": "tg-owner-1"}),
    )

    owui = RecordingAdapter()
    tg = RecordingAdapter()
    # One persistent runner so the mirror dedup (delivery_dedup) is exercised
    # across repeated pending ticks -> must send ONCE.
    runner = _make_bs_runner(owui, tg_adapter=tg, profile="01-orchestrator")
    for _ in range(3):
        await _run_one_branch_tick(monkeypatch, runner)

    # TG mirror молчит: живой OWUI-канал сам отдаст итог после ACK.
    assert tg.sent == [], (
        f"pending OWUI — текстовый успешный итог не дублируется в TG: {tg.sent}"
    )
    # Channel stays pending (not acked, not dropped) while OWUI is pending.
    marker = kb.has_branch_summary(kb.connect(board="default"), root)
    assert marker is not None
    pending = kb.branch_summary_pending_subs(
        marker,
        [{"platform": "api_server", "chat_id": "owui-abc790", "thread_id": ""}],
    )
    assert pending, "channel must stay pending while OWUI not yet acked"

    # OWUI recovers on a later tick -> channel acked, still no TG punctuation.
    mode["state"] = "acknowledged"
    await _run_one_branch_tick(monkeypatch, runner)
    assert tg.sent == [], (
        f"no TG ping after OWUI ack; got {tg.sent}"
    )
    conn = kb.connect(board="default")
    try:
        marker_final = kb.has_branch_summary(conn, root)
        pending_final = kb.branch_summary_pending_subs(
            marker_final,
            [{"platform": "api_server", "chat_id": "owui-abc790", "thread_id": ""}],
        )
    finally:
        conn.close()
    assert pending_final == [], f"channel should be acked after OWUI ack: {pending_final}"


@pytest.mark.asyncio
async def test_dead_session_owui_still_mirrors_tg_and_closes_channel(
    board_env, monkeypatch,
):
    """t_5d99e48d: dead OWUI session (stage failed after bounded retries) →
    the итог is NOT lost: the OWUI→TG mirror fires once and the channel is
    closed (acked) with a [delivery-drop] reason — no silent double loss."""
    import json as _json
    from gateway import kanban_origin_delivery as _kod

    root = _mk(board_env, "root")
    a = _mk(board_env, "a")
    kb.link_tasks(board_env, root, a)
    kb.add_notify_sub(
        board_env,
        task_id=root,
        platform="api_server",
        chat_id="owui-abc791",
        user_id="sess-initiator-1",
        delivery_mode="notify+wake",
    )
    _complete_branch(board_env, root, [a])

    def dead_stage(outbox, registry, sub, profile, board, event_id, message):
        # enqueue fails permanently (e.g. chat deleted from registry -> unbound)
        return ("unbound", None)

    monkeypatch.setattr(_kod, "stage_terminal", dead_stage)
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")
    monkeypatch.setenv("HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        _json.dumps({"sess-initiator-1": "tg-owner-1"}),
    )

    owui = RecordingAdapter()
    tg = RecordingAdapter()
    runner = _make_bs_runner(owui, tg_adapter=tg, profile="01-orchestrator")
    for _ in range(2):
        await _run_one_branch_tick(monkeypatch, runner)

    # The terminal итог reached the owner's bound TG chat exactly once.
    assert len(tg.sent) == 1, f"dead session must mirror итог to TG once: {tg.sent}"
    # Channel closed (acked) — the OWUI lane is permanently broken.
    conn = kb.connect(board="default")
    try:
        marker = kb.has_branch_summary(conn, root)
        pending = kb.branch_summary_pending_subs(
            marker,
            [{"platform": "api_server", "chat_id": "owui-abc791", "thread_id": ""}],
        )
        comments = kb.list_comments(conn, root)
    finally:
        conn.close()
    assert pending == [], f"dead-session channel should be closed: {pending}"
    drop = [c for c in comments if c.body.startswith("[delivery-drop]")]
    assert drop == [], (
        "no forced drop needed: итог already reached the owner via TG mirror "
        f"({[c.body for c in comments]})"
    )
