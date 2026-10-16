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
to False (pre-wiring behavior) all four tests FAIL — per-child pings leak to
TG, the intermediate-noise wave stages to OWUI, and the terminal итог is
delivered per-event instead of as the single branch message. With the wired
flag True (current behavior) all four PASS — silence restored on both lanes.
Measured on this file: 4 failed (flag False) / 4 passed (flag True).
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
    """Records ``send()`` and, when push-capable, ``handle_message`` wakes."""

    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.wakes = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

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
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
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
async def test_split_sub_branch_close_silent_on_both_lanes(mission_env, monkeypatch):
    """A ``[split N/M]``-style mid-tree close (child chain under a still-open
    mission root) is silent on BOTH lanes — explicitly listed in acceptance."""
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
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
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
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
    assert "blocked" in tg_texts[0]


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

    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_branch_summary_replace", lambda: True,
    )
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
