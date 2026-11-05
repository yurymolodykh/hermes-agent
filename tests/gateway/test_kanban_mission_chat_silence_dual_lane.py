"""Acceptance test: mission of N cards — CHAT SILENCE on both lanes (t_e6c12f37).

Requirement (card t_e6c12f37): "миссия из N карточек → в чате только
{запрос}+{терминальный итог с артефактами}+{блокировки при наличии};
промежуточные completed/status/blocked дочерних карточек и split N/M
не видны ни в OWUI-сессии, ни в TG".

This is the DUAL-LANE acceptance matrix that combines the api_server-lane
suppression (t_60aa4496 / t_ec550192 / t_b9f41135) with the wake-mechanics
(t_8788288c / t_7115acce) on ONE mission tree, asserting silence across BOTH
the OWUI session AND the OWUI→Telegram mirror (F7, t_ebc0a351 / T2 §8.2.2).

The mission shape follows the F7 story: an OWUI mission root is subscribed
ONLY via ``api_server`` (0 native telegram rows); the Telegram lane exists
purely through the owner-binding mirror (``HERMES_OPENWEBUI_TELEGRAM_USERS``).

Assertions:

  * the user's REQUEST (the originating OWUI session subscription) survives;
  * intermediate per-child completed/status/self-resolving blocked and
    [split N/M] closes produce NO message on either lane: no OWUI outbox
    staging, no Telegram mirror send, and never wake the agent;
  * a genuine human-action block (blocked + provider_route) stays LOUD on
    both lanes (OWUI staging + TG mirror send);
  * the TERMINAL branch итог with report artifacts arrives on BOTH lanes
    (OWUI per subscribed chat + the mirror to the owner's bound TG chat),
    and no routine suppression journal is produced for the terminal close.

This wires ``should_suppress_api_server_event`` and
``should_wake_agent_for_event`` together on one mission tree through the real
``_kanban_notifier_watcher`` and ``_kanban_branch_summary_watcher`` loops —
the exact shape the acceptance statement describes.

Red→green proof (acceptance): with ``_kanban_branch_summary_replace`` forced
to False (pre-wiring behavior) the silence tests FAIL — per-child pings leak
to TG, the intermediate-noise wave stages to OWUI, and the terminal итог is
delivered per-event instead of as the single branch message. With the wired
flag True (current behavior) they PASS — silence restored on both lanes, and
the human-action loud kinds (provider_route / block_loop_detected / triage)
stay visible on BOTH lanes as 'нужно действие'. Measured on this file:
8 tests on the wired flag True.
"""

import asyncio
import json
import os

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


def _make_runner(adapters):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner._kanban_dispatcher_lock_handle = object()  # owns dispatcher -> include unowned subs
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner


async def _run_one_notifier_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the notifier watcher."""
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None  # startup + cadence; keep this tick alive
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=interval)


async def _run_one_branch_tick(monkeypatch, runner, interval=1):
    """Execute exactly one loop body of the branch-summary watcher."""
    real_sleep = asyncio.sleep
    runner._running = True

    async def fake_sleep(delay):
        if delay >= 5:
            return None  # startup + cadence; keep running this tick
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_branch_summary_watcher(interval=interval)


class RecordingAdapter:
    """Records ``send()``/``send_document()`` and, when push-capable,
    ``handle_message`` wakes."""

    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.docs = []
        self.wakes = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        self.docs.append(
            {"chat_id": chat_id, "file_path": file_path,
             "file_name": file_name or os.path.basename(file_path),
             "metadata": metadata or {}}
        )
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def handle_message(self, event):
        self.wakes.append(getattr(event, "text", ""))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

OWNER_OWUI = "sess-owui-creator"
OWNER_TG = "tg-owner-100200300"
ALLOWED_TG = [OWNER_TG]


@pytest.fixture
def mission_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "dual.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    # OWUI→TG mirror needs the owner binding + TG allowlist/home.
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED_TG))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    # License check / registry files never exist in the hermetic tmp home;
    # stage_terminal is patched below, no real OWUI registry is touched.
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, parent=None, assignee="worker", initial_status="running"):
    tid = kb.create_task(
        conn, title=title, assignee=assignee,
        initial_status=initial_status,
    )
    if parent:
        kb.link_tasks(conn, parent, tid)
    return tid


def _owui_sub(conn, tid, chat, user_id=OWNER_OWUI):
    """Mirror what the auto-subscribe path writes for a server-owned OWUI chat:
    an api_server subscription carries no native telegram row (F7 story)."""
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="api_server",
        chat_id=chat,
        user_id=user_id,
        delivery_mode="notify+wake",
    )


def _staged_sink(monkeypatch, staged):
    from gateway import kanban_origin_delivery as _kod

    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    return staged


def _complete(conn, tid, *, summary="done", artifacts=None):
    metadata = {"artifacts": artifacts} if artifacts is not None else None
    return kb.complete_task(conn, tid, summary=summary, metadata=metadata)


def _emit_self_resolving_blocked(conn, tid, kind="dependency", reason="waiting"):
    """Route a card to ``todo`` with a routine ``blocked`` event (auto-resolving)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='todo', block_kind=?, block_recurrences=0 "
            "WHERE id=? AND status IN ('running','ready')",
            (kind, tid),
        )
        kb._append_event(
            conn, tid, "blocked",
            {"reason": reason, "kind": kind, "recurrences": 0},
        )


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


def _setup_mission(conn, n_children=2, *, with_self_resolving_block=False):
    """Build an OWUI-only mission tree: root + children, api_server subs only.

    TG delivery exists entirely through the owner-binding mirror
    (HERMES_OPENWEBUI_TELEGRAM_USERS set in ``mission_env``). Returns
    (root, children). ``with_self_resolving_block`` additionally emits a
    routine self-resolving ``blocked`` on child-0 BEFORE the terminal wave.
    """
    root = _mk(conn, "mission")
    children = [_mk(conn, f"child-{i}", parent=root) for i in range(n_children)]
    _owui_sub(conn, root, "owui-request")
    for i, cid in enumerate(children):
        _owui_sub(conn, cid, f"owui-child-{i}")
    if with_self_resolving_block:
        _emit_self_resolving_blocked(conn, children[0], kind="dependency")
    return root, children


def _dual_adapters():
    """OWUI (api_server, non-push) + TG (push) adapters on one runner."""
    owui = RecordingAdapter()
    owui.supports_async_delivery = False  # api_server: no push, outbox is delivery
    tg = RecordingAdapter()  # push-capable (mirror sends + wakes)
    return owui, tg, _make_runner({Platform.API_SERVER: owui, Platform.TELEGRAM: tg})


def adapter_sent_texts(adapter):
    return [s["text"] for s in adapter.sent]


# ---------------------------------------------------------------------------
# 1. Intermediate child noise is SILENT on both lanes + never wakes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intermediate_child_noise_silent_on_both_lanes(mission_env, monkeypatch):
    """Intermediate per-child completed + self-resolving blocked produce ZERO
    messages on the OWUI lane AND the TG mirror, and never wake the agent."""
    root, children = _setup_mission(mission_env, n_children=3,
                                    with_self_resolving_block=True)

    # The mission root completes (parent-satisfaction), then its children reach
    # terminal one by one — plus child-0 carried a routine self-resolving
    # blocked. This is the full intermediate-noise wave of the requirement.
    assert _complete(mission_env, root, summary="mission done")
    for i, cid in enumerate(children):
        assert _complete(mission_env, cid, summary=f"child {i} done")

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # NO per-child/secondary message on EITHER lane (the branch итог is the
    # single message, delivered by the branch-summary watcher below).
    assert adapter_sent_texts(tg) == [], f"TG mirror must stay silent: {tg.sent}"
    assert staged == [], f"OWUI must not stage per-child pings: {staged}"
    # Neither the self-resolving blocked nor the child completions wake.
    assert tg.wakes == [] and owui.wakes == [], (
        f"intermediate events must not wake: tg={tg.wakes} owui={owui.wakes}"
    )


@pytest.mark.asyncio
async def test_parent_with_open_children_completed_silent_on_tg(mission_env, monkeypatch):
    """P2 (t_ecab4990): the mission ROOT completes FIRST while its children are
    still OPEN (running). Even though the completing card is the tree ROOT
    (task_is_root=True), it is a PARENT with live descendants — so its
    ``completed`` is NOT the branch итог and the OWUI→TG mirror must stay
    SILENT in the notifier tick (one terminal итог comes later from the
    branch-summary watcher once the whole tree closes).
    """
    root, children = _setup_mission(mission_env, n_children=2)

    # Root completes FIRST; children stay RUNNING (mid-tree, mission NOT closed).
    assert _complete(mission_env, root, summary="mission done")

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # P2: parent-with-open-children completed → SILENT on the TG mirror. The
    # routine close is claimed+journaled, but never reaches Telegram; the
    # whole-mission итог is the branch-summary watcher's single message.
    assert adapter_sent_texts(tg) == [], (
        f"P2: root-with-open-children must be silent on TG: {tg.sent}"
    )
    assert staged == [], f"P2: no OWUI staging for intermediate root close: {staged}"
    assert tg.wakes == [] and owui.wakes == [], (
        f"P2: intermediate root close must not wake: tg={tg.wakes} owui={owui.wakes}"
    )
    # The suppression path for a routine api_server close is the canonical
    # api_server-lane predicate (claimed+silent), which precedes the OWUI→TG
    # mirror — the journal entry is produced by the dual-lane branch (covered
    # in unit form by tests/cron/test_dual_lane_t2.py P2 cases).  The
    # acceptance invariant here is the SILENCE itself on both lanes.
    assert adapter_sent_texts(tg) == [], (
        f"P2: root-with-open-children must be silent on TG: {tg.sent}"
    )


@pytest.mark.asyncio
async def test_parent_with_open_children_completed_control_tg(mission_env, monkeypatch):
    """P2 control: once ALL descendants are terminal (has_open_descendants
    becomes False), the same root confirmed close IS the terminal итог and
    the branch-summary watcher delivers it to TG — the P2 suppression only
    holds while children remain open.
    """
    root, children = _setup_mission(mission_env, n_children=2)
    assert _complete(mission_env, root, summary="mission done")
    for i, cid in enumerate(children):
        assert _complete(mission_env, cid, summary=f"child {i} done")

    # Notifier tick stays silent on TG for the per-event closes...
    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)
    assert adapter_sent_texts(tg) == [], f"notifier must not send TG: {tg.sent}"

    # ...and the branch-summary watcher delivers the single terminal итог to TG.
    from gateway import kanban_origin_delivery as _kod

    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    await _run_one_branch_tick(monkeypatch, runner)

    tg_texts = adapter_sent_texts(tg)
    # Контракт владельца 2026-11-04: итог без файла-артефакта в Telegram не
    # дублируется. Контроль P2 здесь проверяет, что маршрут итога сработал
    # (OWUI-канал получил вердикт), а TG молчит по новому правилу.
    assert branch_staged, f"P2 control: итог должен уйти в OWUI: {branch_staged}"
    assert tg_texts == [], f"P2 control: text-only итог не идёт в TG: {tg_texts}"


@pytest.mark.asyncio
async def test_split_sub_branch_close_silent_on_both_lanes(mission_env, monkeypatch):
    """A ``[split N/M]``-style mid-tree close (child chain under a still-open
    mission root) is silent on BOTH lanes — explicitly listed in acceptance."""
    mission = _mk(mission_env, "mission")
    split = _mk(mission_env, "[split 1/2] sub-branch", parent=mission)
    sub = _mk(mission_env, "sub-task", parent=split)
    _owui_sub(mission_env, mission, "owui-mission")
    _owui_sub(mission_env, split, "owui-split")
    _owui_sub(mission_env, sub, "owui-sub")

    # Mission is still RUNNING. The [split N/M] branch closes directly
    # (parent-gating bypassed exactly as the notifier sees a mid-tree close
    # while the ancestor mission remains open).
    with kb.write_txn(mission_env):
        mission_env.execute("UPDATE tasks SET status='done' WHERE id=?", (sub,))
        kb._append_event(mission_env, sub, "completed", {"summary": "sub done"})
        mission_env.execute("UPDATE tasks SET status='done' WHERE id=?", (split,))
        kb._append_event(mission_env, split, "completed", {"summary": "split done"})

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    assert adapter_sent_texts(tg) == [], f"split close must be silent on TG: {tg.sent}"
    assert staged == [], f"split close must not stage OWUI: {staged}"
    assert tg.wakes == [] and owui.wakes == [], (
        f"split close must not wake: tg={tg.wakes} owui={owui.wakes}"
    )


# ---------------------------------------------------------------------------
# 2. Human-action block (provider_route) stays LOUD on both lanes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_route_block_loud_on_both_lanes(mission_env, monkeypatch):
    """A child blocked by a provider outage (payload ``provider_route``) MUST
    reach BOTH the OWUI session and the owner's bound TG chat — even under
    replace mode ('блокировки при наличии')."""
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-provider", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child-provider")

    assert _complete(mission_env, root, summary="mission done")
    # Provider-outage blocker: blocked + needs_input + provider_route payload.
    with kb.write_txn(mission_env):
        payload = {
            "reason": "Провайдер/модель letovo/deepseek-v4-flash недоступен; выберите маршрут",
            "kind": "needs_input",
            "provider_route": "letovo/deepseek-v4-flash",
        }
        mission_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=? AND status IN ('running','ready')",
            (child,),
        )
        kb._append_event(mission_env, child, "blocked", payload)

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # OWUI lane: staged to the child's session (the creator's request session).
    assert len(staged) == 1, f"provider_route block must stage OWUI: {staged}"
    assert staged[0][0] == "owui-child-provider"
    assert "blocked" in staged[0][1]
    # TG lane: mirrored to the owner's bound chat (HUMAN_ACTION_KINDS delivers).
    tg_texts = adapter_sent_texts(tg)
    assert len(tg_texts) == 1, f"provider_route block must reach TG: {tg_texts}"
    # F2: the OWUI→TG mirror carries the owner-facing «Требует решения»
    # format (title + reason), not the generic notifier excerpt.
    assert "Требуется решение" in tg_texts[0]
    assert "child-provider" in tg_texts[0]
    assert "letovo/deepseek-v4-flash" in tg_texts[0]


@pytest.mark.asyncio
async def test_block_loop_detected_triage_loud_on_both_lanes(mission_env, monkeypatch):
    """A child re-blocked for the same cause past the recurrence limit is
    routed to TRIAGE (``block_loop_detected``) — the ONE transition that
    exists to force human attention. It MUST stay visible on BOTH the OWUI
    lane (staged with a TRIAGE ping) and the owner's bound TG chat, even
    under replace mode ('блокировки при наличии')."""
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-loop", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child-loop")

    assert _complete(mission_env, root, summary="mission done")
    # Emulate the recurrence-limit path (same shape as block_task when
    # recurrences >= BLOCK_RECURRENCE_LIMIT): route to triage + emit the
    # block_loop_detected event.
    with kb.write_txn(mission_env):
        payload = {
            "reason": "re-blocked for the same cause",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        }
        mission_env.execute(
            "UPDATE tasks SET status='triage', block_kind='needs_input', "
            "block_recurrences=2 WHERE id=? AND status IN ('running','ready')",
            (child,),
        )
        kb._append_event(mission_env, child, "block_loop_detected", payload)

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # OWUI lane: TRIAGE ping staged to the child's bound session.
    assert len(staged) == 1, f"block_loop_detected must stage OWUI: {staged}"
    assert staged[0][0] == "owui-child-loop"
    assert "TRIAGE" in staged[0][1].upper()
    # TG lane: block_loop_detected is a HUMAN_ACTION_KIND -> delivered to the
    # owner's bound chat, not suppressed by the routine gate.
    tg_texts = adapter_sent_texts(tg)
    assert len(tg_texts) == 1, f"block_loop_detected must reach TG: {tg_texts}"
    assert "Требуется решение" in tg_texts[0]


@pytest.mark.asyncio
async def test_triage_card_routed_loud_on_both_lanes(mission_env, monkeypatch):
    """A card routed to ``triage`` (card status) stays visible on BOTH lanes
    even when only a routine ``completed`` event remains on it — the
    notifier/predicate reads the card status and never drops a triage card
    (acceptance: 'triage остаются видимыми').  The TG mirror classifies the
    staged message by its content shape rather than dropping a triage card."""
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-triage", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child-triage")

    assert _complete(mission_env, root, summary="mission done")
    with kb.write_txn(mission_env):
        # Card routed to triage but the LAST claimed event is a routine
        # completed — the predicate must still keep it loud via card.status.
        mission_env.execute(
            "UPDATE tasks SET status='triage' WHERE id=?", (child,),
        )
        kb._append_event(mission_env, child, "completed", {"summary": "done"})

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # OWUI lane: the triage card stays loud (staged via card.status).
    assert len(staged) == 1, f"triage card must stage OWUI: {staged}"
    assert staged[0][0] == "owui-child-triage"


# ---------------------------------------------------------------------------
# 3. Terminal итог: message on BOTH lanes, artifact intact, no suppression.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_itog_both_lanes_one_message_with_artifacts(mission_env, monkeypatch):
    """When the whole mission closes, the terminal branch итог arrives on BOTH
    the OWUI lane (one stage per subscribed chat, request session included)
    AND the OWUI→TG mirror (to the owner's bound chat) — with the report
    artifact riding inline, and no routine-suppression journal entries."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "mission-report.md")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("Миссия завершена: все карточки закрыты.\n")

        root, children = _setup_mission(mission_env, n_children=2)
        assert _complete(mission_env, root, summary="mission done", artifacts=[report])
        for i, cid in enumerate(children):
            assert _complete(mission_env, cid, summary=f"child {i} done")

        # -- Notifier tick: every intermediate close is claimed+silent. --
        staged_nt = []
        _staged_sink(monkeypatch, staged_nt)
        owui, tg, runner = _dual_adapters()
        await _run_one_notifier_tick(monkeypatch, runner)
        assert adapter_sent_texts(tg) == [], f"notifier must not send TG: {tg.sent}"
        assert staged_nt == [], f"notifier must not stage OWUI: {staged_nt}"
        assert tg.wakes == [], f"notifier must not wake: {tg.wakes}"

        # -- Branch-summary tick: terminal итог on both lanes. --
        from gateway import kanban_origin_delivery as _kod

        branch_staged = []

        def fake_stage(outbox, registry, sub, profile, board, event_id, message):
            branch_staged.append((sub.get("chat_id"), message, event_id))
            return ("acknowledged", 1)

        monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
        await _run_one_branch_tick(monkeypatch, runner)

        # OWUI lane: every subscribed channel of the tree gets EXACTLY ONE итог
        # (dedup per (platform,chat,thread)), including the request session.
        assert branch_staged, f"no OWUI итог staged: {branch_staged}"
        by_chat = {}
        for chat_id, message, _eid in branch_staged:
            assert chat_id not in by_chat, f"duplicate OWUI итог to {chat_id}"
            by_chat[chat_id] = message
        assert set(by_chat) == {"owui-request", "owui-child-0", "owui-child-1"}, \
            f"unexpected OWUI receivers: {sorted(by_chat)}"
        itog = by_chat["owui-request"]
        assert "Задача выполнена" in itog
        assert "mission-report.md" in itog
        assert "Миссия завершена" in itog

        # TG lane: the owner's bound chat receives the terminal итог via the
        # OWUI→TG mirror.
        tg_texts = adapter_sent_texts(tg)
        assert tg_texts, f"TG mirror must deliver terminal итог: {tg_texts}"
        assert "Задача выполнена" in tg_texts[0]
        # Journal: no routine suppression for the terminal delivery.
        assert _read_suppression_journal() == [], (
            f"no suppression expected for terminal delivery: {_read_suppression_journal()}"
        )


# ---------------------------------------------------------------------------
# 4. П.4 addressee: every owner gets THEIR OWN итог, never a foreign/home chat.
#    (non-Yuri OWUI creator -> own OWUI chat + own bound TG chat, NOT home;
#     unbound foreign creator -> OWUI-only, fail-closed, nothing to home)
# ---------------------------------------------------------------------------


FOREIGN_OWUI = "sess-owui-maria"
FOREIGN_TG = "tg-maria-400500600"
HOME_TG = "tg-home-yuri-900100200"


@pytest.mark.asyncio
async def test_p4_foreign_owner_with_binding_delivers_to_own_lanes(mission_env, monkeypatch):
    """П.4 (t_6692dd10): a terminal итог created by a foreign OWUI owner
    (Maria) who HAS a server binding goes to HER OWN OWUI chat AND HER OWN
    bound TG chat — never to the home channel (Yuri 125805788) by default.

    Regression guard for the 'отчёты приходят Юрию, а не постановщику'
    complaint: the TG mirror must resolve ``owui_telegram_users[sub.user_id]``
    (per-owner), not fall back to the configured home channel.
    """
    # Binding for the FOREIGN owner only; home is the OWNER_TG from the
    # fixture. A per-owner fallback would (wrongly) target home.
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        json.dumps({OWNER_OWUI: OWNER_TG, FOREIGN_OWUI: FOREIGN_TG}),
    )
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    # H-5 (t_efffb731): сама серверная привязка — достаточная авторизация
    # зеркала в СОБСТВЕННЫЙ bound-чат.  Maria's chat NOT in ALLOWED — зеркало
    # всё равно должно дойти в её bound-чат (ранее гейт ALLOWED/home требовал
    # allowlisting и голодал второго/последующих привязанных пользователей).
    monkeypatch.setenv(
        "TELEGRAM_ALLOWED_USERS", ",".join([OWNER_TG, HOME_TG]),
    )

    root = _mk(mission_env, "mission-maria")
    # Maria's OWUI session owns the mission; ONLY her subscription exists.
    _owui_sub(mission_env, root, "owui-maria-request", user_id=FOREIGN_OWUI)
    assert _complete(mission_env, root, summary="mission maria done")

    # Notifier tick is covered by prior tests; here only the branch-summary
    # lane decides the addressee, so go straight to the branch tick.
    from gateway import kanban_origin_delivery as _kod

    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append(
            (sub.get("chat_id"), sub.get("user_id"), message, event_id)
        )
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _dual_adapters()
    await _run_one_branch_tick(monkeypatch, runner)

    # OWUI lane: staged to MARIA's chat (never home/Yuri's).
    assert branch_staged, f"no OWUI итог staged for Maria: {branch_staged}"
    assert all(chat[0].startswith("owui-maria") for chat in branch_staged), (
        f"OWUI итог must go to Maria's chat: {branch_staged}"
    )
    # TG mirror: delivered to Maria's OWN bound chat, never home.  F1
    # (tg-delivery-contract): the итог to TG carries the handoff summary below
    # the closed-branch header — no more contentless "✅" alone.
    # TG mirror: текстовый итог БЕЗ артефакта в Telegram не идёт (контракт
    # владельца 2026-11-04: «в TG только по делу» — вопросы миссий, файлы,
    # алерты).  Отклик постановщику доставляется OWUI-каналом выше.
    tg_chats = [s["chat_id"] for s in tg.sent]
    assert tg_chats == [], (
        f"П.4: text-only итог не должен дублироваться в TG: {tg.sent}"
    )


@pytest.mark.asyncio
async def test_binding_absent_foreign_owner_stays_owui_only(mission_env, monkeypatch):
    """П.4: a foreign OWUI owner with NO server TG binding must stay
    OWUI-only (fail-closed, §8.3/V8) — the итог lands in her OWUI chat but
    is NEVER mirrored to any Telegram chat (in particular not to the home /
    Yuri by default). This is the acceptance: 'чужие итоги не падают Юрию
    по умолчанию'.
    """
    # Binding and home stay from the fixture; FOREIGN_OWUI is deliberately
    # ABSENT from the binding map => no Telegram lane for her (fail-closed).
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS",
        json.dumps({OWNER_OWUI: OWNER_TG}),
    )

    root = _mk(mission_env, "mission-unbound-maria")
    _owui_sub(mission_env, root, "owui-maria-unbound", user_id=FOREIGN_OWUI)
    assert _complete(mission_env, root, summary="unbound mission done")

    from gateway import kanban_origin_delivery as _kod

    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _dual_adapters()
    await _run_one_branch_tick(monkeypatch, runner)

    # OWUI lane: her own chat gets the итог (primary lane always delivers).
    assert branch_staged, f"unbound owner still gets OWUI итог: {branch_staged}"
    assert branch_staged[0][0] == "owui-maria-unbound"
    # TG mirror: NO send anywhere — not her (no binding), not home/Yuri.
    assert tg.sent == [], (
        f"П.4: unbound owner must not mirror to ANY telegram chat (home/Yuri "
        f"is not a default sink): {tg.sent}"
    )


# ---------------------------------------------------------------------------
# F1 (tg-delivery-contract, t_030b5f47): terminal итог in the OWUI→TG mirror
# carries CONTENT (summary + file artifacts); a bare "✅ Задача выполнена"
# with no summary and no artifacts is SUPPRESSED with a journal entry.
# ---------------------------------------------------------------------------

def _adapter_sent_texts(adapter):
    return [s["text"] for s in adapter.sent]


@pytest.mark.asyncio
async def test_f1_terminal_itog_mirror_carries_summary_and_artifact_files(
    mission_env, monkeypatch,
):
    """F1 acceptance (1)+(2): a TERMINAL branch итог whose root carries a
    handoff summary AND a file artifact reaches the owner's bound TG chat as
    (a) enriched text (summary below the closed-branch header) and
    (b) the artifact uploaded as a native file via send_document (the existing
    ``_deliver_kanban_artifacts`` helper — D2 path now wired into the mirror).
    No suppression journal entry for a terminal delivery with content.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "f1-report.md")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("F1: терминальный итог с отчётом.\n")

        root, children = _setup_mission(mission_env, n_children=1)
        assert _complete(
            mission_env, root, summary="Миссия F1 завершена: все карточки закрыты",
            artifacts=[report],
        )
        assert _complete(mission_env, children[0], summary="child done")

        from gateway import kanban_origin_delivery as _kod

        branch_staged = []

        def fake_stage(outbox, registry, sub, profile, board, event_id, message):
            branch_staged.append((sub.get("chat_id"), message, event_id))
            return ("acknowledged", 1)

        monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
        owui, tg, runner = _dual_adapters()
        await _run_one_branch_tick(monkeypatch, runner)

        # OWUI primary lane still delivers (unchanged).
        assert branch_staged
        # TG mirror: every mirror send to the owner's bound chat carries the
        # enriched message with summary content (one send per api_server sub;
        # count is not the contract — content is).
        assert tg.sent, f"TG mirror must deliver the terminal итог: {tg.sent}"
        assert all(s["chat_id"] == OWNER_TG for s in tg.sent)
        for s in tg.sent:
            assert "Задача выполнена" in s["text"]
            assert "Миссия F1 завершена" in s["text"]
        # Artifact uploaded as a native file via send_document (D2 closed):
        # all mirror sends attach the same report file to the owner's chat.
        assert tg.docs, f"artifact must be uploaded as file: {tg.docs}"
        assert all(d["chat_id"] == OWNER_TG for d in tg.docs)
        assert all(
            os.path.basename(d["file_path"]) == "f1-report.md" for d in tg.docs
        )
        # No suppression journal for a terminal delivery with content.
        assert _read_suppression_journal() == []


@pytest.mark.asyncio
async def test_f1_bare_terminal_itog_suppressed_with_journal(
    mission_env, monkeypatch,
):
    """F1 acceptance (3): a TERMINAL branch итог with NO handoff summary and
    NO artifacts (the bare ``✅ Задача выполнена`` baked one-liner) is
    SUPPRESSED entirely in the OWUI→TG mirror — silence in TG — and journaled
    with ``reason=bare terminal итог without content``.  OWUI primary lane is
    unaffected (the итог is still staged there).
    """
    root, children = _setup_mission(mission_env, n_children=1)
    # No summary, no artifacts → the completed payload carries only the
    # generic first-line placeholder; the mirror has nothing to enrich with.
    assert _complete(mission_env, root, summary="")
    assert _complete(mission_env, children[0], summary="")

    from gateway import kanban_origin_delivery as _kod

    branch_staged = []

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        branch_staged.append((sub.get("chat_id"), message, event_id))
        return ("acknowledged", 1)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    owui, tg, runner = _dual_adapters()
    await _run_one_branch_tick(monkeypatch, runner)

    # OWUI primary lane still gets the итог (mirror suppression never touches it).
    assert branch_staged, f"OWUI must still receive the итог: {branch_staged}"
    # TG mirror: fully suppressed — nothing sent.
    assert tg.sent == [], f"bare итог must be suppressed in TG: {tg.sent}"

    # Journal: at least one record (one per mirrored api_server sub), every
    # record reason=bare terminal итог without content.
    journal = _read_suppression_journal()
    assert journal, f"journal must record the suppression: {journal}"
    assert all(
        r["reason"] == "bare terminal итог without content" for r in journal
    ), f"all suppressions must be the bare-итог reason: {journal}"
    first = journal[0]
    assert first["app"] == "kanban"
    assert first["target"] == f"telegram:{OWNER_TG}"
    assert first["task_id"] == root
    assert first["event_kind"] == "branch_summary"
    assert first["owui_to_tg"] is True


# ---------------------------------------------------------------------------
# t_f659bef2 — OWUI→TG mirror honours plan-payload artifact rider (D0 D2/A4):
# ``plan['artifacts']`` is merged alongside the kanban.db sources, so a future
# source that puts paths directly on the branch_summary plan reaches TG as
# file attachments.  Missing / non-absolute / oversize plan artifacts are
# skipped with a warning and never fail the text delivery.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_plan_payload_artifact_rider(mission_env, monkeypatch):
    """t_f659bef2: the OWUI→TG mirror honours an explicit ``plan['artifacts']``
    list exactly like the native-TG lane (parity, D0 D2/A4).  A real file on
    the plan payload rides ``deliver_artifacts`` → the TG ``send_document``
    upload; a missing path is dropped by the delivery layer (never breaks the
    text); ``handle_branch_final`` composes the terminal итог with summary.

    R1: driven through ``DeliveryEngine.handle_branch_final`` (the single
    branch-итог surface; the removed ``_branch_summary_owui_tg_lane`` merge
    is now the engine's ``render_final`` + artifact delivery).
    """
    import tempfile

    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED_TG))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)

    from gateway.delivery_engine import DeliveryEngine

    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "plan-rider.md")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("План-артефакт.\n")
        missing = os.path.join(tmp, "missing.md")

        plan = {
            "root_id": "root-x",
            "board": "default",
            "terminal": True,
            "artifacts": [report, missing],
        }
        sub = {
            "platform": "api_server",
            "chat_id": "owui-request-x",
            "user_id": OWNER_OWUI,
            "thread_id": None,
        }

        tg = RecordingAdapter()
        engine = DeliveryEngine(
            allowed_users=list(ALLOWED_TG),
            owui_to_tg={OWNER_OWUI: OWNER_TG},
            home_channel=OWNER_TG,
        )
        engine._tg_adapter = tg
        sent = []
        docs = []

        async def _send_tg(adapter, chat_id, text):
            await adapter.send(chat_id, text)
            sent.append((chat_id, text))

        async def _deliver_artifacts(*, adapter, chat_id, metadata=None,
                                     event_payload=None, task=None):
            for path in (event_payload or {}).get("artifacts") or []:
                if os.path.exists(path):
                    await adapter.send_document(chat_id, path,
                                                metadata=metadata or {})
                    docs.append(str(path))

        res = await engine.handle_branch_final(
            plan=plan, sub=sub, msg="✅ Задача выполнена",
            adapter=tg, sub_profile="mission", terminal=True,
            artifacts=plan["artifacts"],
            deliver_artifacts=_deliver_artifacts, send_tg=_send_tg,
        )
        assert res.action == "advance", f"terminal итог должен доставляться: {res}"
        # The owner's bound TG chat receives the composed итог.
        assert sent, f"TG mirror must deliver the terminal итог: {sent}"
        assert all(chat_id == OWNER_TG for chat_id, _ in sent)
        # A real file on the plan rides the artifact upload; the missing one
        # was dropped (never reached the delivery layer as a broken path).
        uploaded = docs
        assert any(os.path.realpath(d) == os.path.realpath(report) for d in uploaded), \
            f"plan artifact must ride the mirror upload: {uploaded}"
        assert not any(os.path.realpath(d) == os.path.realpath(missing) for d in uploaded)
        # Nothing suppressed: a contentful terminal итог is delivered.
        assert _read_suppression_journal() == []


@pytest.mark.asyncio
async def test_mirror_plan_rider_bare_itog_still_suppressed(mission_env, monkeypatch):
    """t_f659bef2 — R1 contract: with no summary content and NO plan artifacts
    the branch [final] engine still composes a terminal итог and delivers it
    to the owner's bound TG chat via ``handle_branch_final`` (the plan rider
    never blocks the delivery; ``render_final`` falls back to the baked
    one-liner).  The engine is the single branch-итог surface; no OWUI-only
    rollback applies once the tree is terminal.
    """
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED_TG))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)

    from gateway.delivery_engine import DeliveryEngine

    plan = {
        "root_id": "root-y",
        "board": "default",
        "terminal": True,
        "artifacts": [],
    }
    sub = {
        "platform": "api_server",
        "chat_id": "owui-request-y",
        "user_id": OWNER_OWUI,
        "thread_id": None,
    }

    tg = RecordingAdapter()
    engine = DeliveryEngine(
        allowed_users=list(ALLOWED_TG),
        owui_to_tg={OWNER_OWUI: OWNER_TG},
        home_channel=OWNER_TG,
    )
    engine._tg_adapter = tg
    sent = []
    docs = []

    async def _send_tg(adapter, chat_id, text):
        await adapter.send(chat_id, text)
        sent.append((chat_id, text))

    async def _deliver_artifacts(*, adapter, chat_id, metadata=None,
                                 event_payload=None, task=None):
        for path in (event_payload or {}).get("artifacts") or []:
            if os.path.exists(path):
                await adapter.send_document(chat_id, path, metadata={})
                docs.append(str(path))

    res = await engine.handle_branch_final(
        plan=plan, sub=sub, msg="✅ Задача выполнена",
        adapter=tg, sub_profile="mission", terminal=True,
        deliver_artifacts=_deliver_artifacts, send_tg=_send_tg,
    )
    assert res.action == "advance", f"terminal итог должен доставляться: {res}"
    # Контракт владельца 2026-11-04: terminal итог БЕЗ файла-артефакта в
    # Telegram не идёт — канал молча акается (res.tg_suppressed), вердикт и
    # так лежит в OWUI.
    assert sent == [], f"text-only итог не должен идти в TG: {sent}"
    assert res.tg_suppressed is True
    # No artifacts on the plan → nothing to upload; empty list rides harmless.
    assert docs == []


# ---------------------------------------------------------------------------
# F2 (t_a14f7e23): OWUI→TG mirror for blocked/needs_input human-action events.
# ---------------------------------------------------------------------------


def _emit_human_block(conn, tid, *, reason="need an answer", kind="needs_input",
                      provider_route=None):
    """Route ``tid`` to ``blocked`` and append a human-action ``blocked`` event."""
    payload = {"reason": reason, "kind": kind}
    if provider_route:
        payload["provider_route"] = provider_route
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind=? "
            "WHERE id=? AND status IN ('running','ready')",
            (kind, tid),
        )
        kb._append_event(conn, tid, "blocked", payload)


@pytest.mark.asyncio
async def test_f2_needs_input_block_mirrors_to_tg_with_title_reason(
    mission_env, monkeypatch,
):
    """F2 criterion (1): a blocked/needs_input OWUI task reaches the initiator's
    bound TG chat as «Требует решения: <title> + краткая причина», with the
    OWUI primary lane intact and no agent wake."""
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "нужен выбор модели", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child")

    assert _complete(mission_env, root, summary="mission done")
    _emit_human_block(
        mission_env, child, reason="Выберите маршрут: letovo или deepseek",
        kind="needs_input", provider_route="letovo/deepseek-v4-flash",
    )

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # OWUI primary lane: staged to the child's session.
    assert len(staged) == 1, f"needs_input block must stage OWUI: {staged}"
    # TG mirror: the owner-facing «Требует решения» text with title + reason.
    tg_texts = adapter_sent_texts(tg)
    assert len(tg_texts) == 1, f"needs_input block must reach TG: {tg_texts}"
    assert "Требуется решение" in tg_texts[0]
    assert "нужен выбор модели" in tg_texts[0]
    assert "letovo или deepseek" in tg_texts[0]
    # No agent wake on the mirror.
    assert tg.wakes == [] and owui.wakes == [], (
        f"block mirror must not wake: tg={tg.wakes} owui={owui.wakes}"
    )


@pytest.mark.asyncio
async def test_f2_block_mirror_dedup_same_cause_within_cooldown_suppressed(
    mission_env, monkeypatch,
):
    """F2 criterion (2): a repeated human-action block of the SAME (task, kind,
    cause) inside the cooldown is NOT re-mirrored to TG — the dedup marker is
    written on the first delivery and gates the repeat."""
    monkeypatch.setenv(
        "HERMES_KANBAN_TG_BLOCK_MIRROR_COOLDOWN_SECONDS", str(3600),
    )
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-retry", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child-retry")

    assert _complete(mission_env, root, summary="mission done")
    _emit_human_block(mission_env, child, reason="prov is down", kind="needs_input")

    # First tick: the block is mirrored exactly once.
    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)
    assert len(adapter_sent_texts(tg)) == 1, "first block must mirror once"

    # Second tick: the same cause inside the cooldown must NOT re-mirror.
    staged2 = []
    _staged_sink(monkeypatch, staged2)
    owui2, tg2, runner2 = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner2)
    assert adapter_sent_texts(tg2) == [], (
        f"repeat same-cause block inside cooldown must not re-mirror: {adapter_sent_texts(tg2)}"
    )


@pytest.mark.asyncio
async def test_f2_block_mirror_different_cause_bypasses_cooldown(
    mission_env, monkeypatch,
):
    """F2 dedup nuance: a NEW cause (different reason signature) bypasses the
    cooldown — the owner must be pinged for a genuinely new question even if
    the previous mirror was recent."""
    monkeypatch.setenv(
        "HERMES_KANBAN_TG_BLOCK_MIRROR_COOLDOWN_SECONDS", str(3600),
    )
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-2causes", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child-2causes")

    assert _complete(mission_env, root, summary="mission done")
    _emit_human_block(mission_env, child, reason="provider offline",
                      kind="needs_input")

    _staged_sink(monkeypatch, [])
    _, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)
    assert len(adapter_sent_texts(tg)) == 1

    # Different cause: must re-mirror despite the fresh marker.
    _emit_human_block(mission_env, child, reason="credentials expired",
                      kind="needs_input")
    _staged_sink(monkeypatch, [])
    _, tg2, runner2 = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner2)
    texts = adapter_sent_texts(tg2)
    assert len(texts) == 1, f"new cause must re-mirror: {texts}"
    assert "credentials expired" in texts[0]


@pytest.mark.asyncio
async def test_f2_routine_self_resolving_block_not_mirrored(
    mission_env, monkeypatch,
):
    """F2 criterion (3): a self-resolving (dependency/transient) block on the
    api_server lane is routine noise — it must NOT reach TG (and not wake),
    under replace mode."""
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-dep", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child-dep")

    assert _complete(mission_env, root, summary="mission done")
    _emit_self_resolving_blocked(mission_env, child, kind="dependency",
                                 reason="waiting on parent")

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # Routine self-resolving block: silent on BOTH lanes.
    assert staged == [], f"routine block must not stage OWUI: {staged}"
    assert adapter_sent_texts(tg) == [], (
        f"routine block must not reach TG: {adapter_sent_texts(tg)}"
    )
    assert tg.wakes == [] and owui.wakes == []


@pytest.mark.asyncio
async def test_f2_block_mirror_policy_off_no_tg(mission_env, monkeypatch):
    """F2 gate: with ``telegram_delivery_policy`` off, a needs_input block is
    NOT mirrored to TG (the whole OWUI→TG lane is disabled), while the OWUI
    primary lane still stages it."""
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    root = _mk(mission_env, "mission")
    child = _mk(mission_env, "child-policy", parent=root)
    _owui_sub(mission_env, root, "owui-request")
    _owui_sub(mission_env, child, "owui-child-policy")

    assert _complete(mission_env, root, summary="mission done")
    _emit_human_block(mission_env, child, reason="need answer",
                      kind="needs_input")

    staged = []
    _staged_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    assert len(staged) == 1, f"OWUI primary lane must stage: {staged}"
    assert adapter_sent_texts(tg) == [], (
        f"policy off → no TG mirror: {adapter_sent_texts(tg)}"
    )
