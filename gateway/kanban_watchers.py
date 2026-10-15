"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from contextvars import Context
from pathlib import Path
from typing import Any, Callable, Optional

from agent.i18n import t

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


def _resolve_auto_decompose_settings(
    load_config: Callable[[], Any],
) -> "tuple[bool, int]":
    """Resolve the live (enabled, per_tick) auto-decompose settings.

    Read fresh from config on every dispatcher tick (#49638) so that flipping
    ``kanban.auto_decompose: false`` to STOP runaway fan-out takes effect on the
    next tick instead of requiring a gateway restart. Auto-decompose is a
    safety toggle — a user who sees it create and launch tasks they didn't
    intend reaches for this flag to halt it, and a stale boot-captured value
    silently ignoring that change is the bug reported in #49638.

    Fails **safe**: if the config read raises, return ``(False, 3)`` — a
    transient read error must never re-enable a feature the user turned off,
    nor fall back to the burst-prone default-on behaviour. ``per_tick`` is
    clamped to ``>= 1``.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    enabled = bool(kcfg.get("auto_decompose", True))
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    if per_tick < 1:
        per_tick = 1
    return enabled, per_tick


def _kanban_dispatch_allowed() -> bool:
    """Return False while the global emergency stop (`hermes pause`) is engaged.

    Checked every dispatcher tick BEFORE spawning new workers so a pause takes
    effect on the next tick without a gateway restart. In-flight workers are
    never touched — this only stops NEW spawns. Fails open: if the estop
    module is unimportable, dispatch proceeds (the sentinel gate must not
    become a new crash surface for the dispatcher).
    """
    try:
        from agent.estop import check_paused
    except ImportError:
        return True
    return not check_paused("kanban", logger)


def _kanban_branch_summary_replace() -> bool:
    """Return whether ``kanban.branch_summary_replace`` is enabled.

    When enabled, the notifier suppresses the INDIVIDUAL per-task terminal
    pings for every task that is a strict descendant of a subscribed branch
    root (the branch итог delivered by ``_kanban_branch_summary_watcher`` is
    the single channel message). Default ``False``: the branch итог is ADDED
    on top of the existing per-event notifications, which stay untouched.
    """
    try:
        from hermes_cli.config import load_config as _load_cfg
        _cfg = _load_cfg() or {}
        _kanban_cfg = _cfg.get("kanban") or {}
        return bool(_kanban_cfg.get("branch_summary_replace", False))
    except Exception:
        return False


def _task_do_not_deliver(task) -> bool:
    """True when a card is marked ``do_not_deliver`` (synthetic readback).

    Synthetic [F7 readback] verification fixtures and watcher-created
    bookkeeping cards carry ``tasks.do_not_deliver = 1``: they are
    bookkeeping-only and must NEVER become a delivery subject for the
    terminal notifier (Telegram mirrors, api_server staging, notifier wakes)
    or the branch-summary итог routing. Ordinary user-created cards leave the
    flag at its default ``False`` and are delivered exactly as before.

    The guard is defensive: ``task`` may be ``None`` (task row missing) and
    the attribute may not exist on older cached model instances.
    """
    if task is None:
        return False
    return bool(getattr(task, "do_not_deliver", False))


def _tg_delivery_policy_allowed_users() -> list:
    """Parse ``TELEGRAM_ALLOWED_USERS`` env into a list of chat/user ids."""
    try:
        raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        return [p.strip() for p in raw.split(",") if p.strip()]
    except Exception:
        return []


def _tg_notify_policy_decision(sub, ev, kind: str, msg: str, task, allowed_users: list,
                                *, task_is_root: Optional[bool] = None,
                                mission_live: bool = False,
                                task_tg_owner: str = ""):
    """Apply the TG delivery-policy gate to ONE kanban notifier event.

    Returns ``None`` when the gate is OFF (policy disabled) or the
    subscription is not telegram (api_server subs are verified by the
    server-owned registry and are out of the gate's scope).  Otherwise
    returns a ``PolicyDecision``:
      * ``deliver`` — event may be sent+woken;
      * ``suppress(reason)`` — event must be skipped (text send AND wake) and
        the reason journaled (ИНВ1/ИНВ2).

    K1 RF1 (fail-open закрыт): когда модуль политики недоступен или
    ``policy_enabled()`` бросил исключение — возвращается
    ``suppress("policy unavailable")`` (журнал + ERROR-лог), а НЕ ``None``
    (``None`` означал бы «deliver»).  ``task_is_root`` — явный маркер
    «закрытие корня ветки» (RF2); ``task_tg_owner`` — TG-идентичность
    владельца ветки (RF3) — оба вычисляются сборщиком, где есть conn.
    """
    try:
        from hermes_cli.delivery_policy import (
            InitiatorRef,
            TargetRef,
            classify,
            default_context,
            log_suppression,
            policy_enabled,
            verify_subscription_recipient,
        )
    except Exception as exc:
        logger.error(
            "delivery-policy: module import failed (%s) — kanban telegram "
            "events FAIL-CLOSED (suppressed)", exc,
        )
        return _fail_closed_decision(sub, ev, kind, msg, exc)
    try:
        if not policy_enabled():
            return None
    except Exception as exc:
        logger.error(
            "delivery-policy: policy_enabled() raised (%s) — kanban telegram "
            "events FAIL-CLOSED (suppressed)", exc,
        )
        return _fail_closed_decision(sub, ev, kind, msg, exc)
    platform = str(sub.get("platform") or "").lower()
    if platform != "telegram":
        return None
    if not verify_subscription_recipient(
        sub, allowed_users, task_tg_owner=task_tg_owner,
    ):
        log_suppression(
            app="kanban",
            who=str(sub.get("user_id") or sub.get("chat_id") or ""),
            target=f"telegram:{sub.get('chat_id')}",
            decision=_suppress_decision(
                "recipient mismatch", detail=f"sub user not in ALLOWED_USERS"
            ),
            snippet=(msg or "")[:200],
            extra={
                "task_id": sub.get("task_id"),
                "event_kind": kind,
                "event_id": getattr(ev, "id", None),
            },
        )
        return _suppress_decision(
            "recipient mismatch", detail=f"sub user not in ALLOWED_USERS"
        )
    payload = getattr(ev, "payload", None) or {}
    artifacts = list(payload.get("artifacts") or ()) if isinstance(payload, dict) else []
    # K1 RF2 (ИНВ2): терминальность — ЯВНЫЙ маркер финала, а НЕ «есть
    # summary». Финал = task.status=="archived" ЛИБО факт закрытия корня
    # ветки (task_is_root && kind=="completed"). Промежуточная суб-задача,
    # даже с «Окончательным отчётом» в summary, — НЕ терминальна: её
    # completed подавляется как routine status close (§5). Артефакты и
    # human-action события доставляются независимо от этого флага.
    task_terminal = bool(task and task.status == "archived")
    root_closing = bool(task_is_root and kind == "completed")
    # F9 (t_85a095e0): a root card whose mission controller is still live is
    # NOT a terminal close — the mission is in flight, so de-terminalize.
    if root_closing and mission_live:
        root_closing = False
    ctx = default_context(
        kind="kanban",
        initiator=InitiatorRef.from_origin({
            "platform": "telegram",
            "chat_id": sub.get("user_id") or sub.get("chat_id") or "",
        }),
        target=TargetRef.from_dict({
            "platform": "telegram",
            "chat_id": sub.get("chat_id") or "",
            "thread_id": sub.get("thread_id"),
        }),
        home_channel=os.environ.get("TELEGRAM_HOME_CHANNEL", ""),
        allowed_users=allowed_users,
        event_kind=kind,
        artifacts=artifacts,
        has_media=bool(artifacts),
        completed_terminal=bool(task_terminal or root_closing),
        no_agent=False,
    )
    decision = classify(msg, ctx)
    if decision.suppressed:
        log_suppression(
            app="kanban",
            who=str(sub.get("user_id") or sub.get("chat_id") or ""),
            target=f"telegram:{sub.get('chat_id')}",
            decision=decision,
            snippet=(msg or "")[:200],
            extra={
                "task_id": sub.get("task_id"),
                "event_kind": kind,
                "event_id": getattr(ev, "id", None),
            },
        )
    return decision


def _suppress_decision(reason: str = "", detail: str = "") -> "PolicyDecision":
    from hermes_cli.delivery_policy import suppress

    return suppress(reason, detail)


class _UnavailableSuppress:
    """Module-free ``PolicyDecision`` stand-in for the fail-closed path (K1 RF1).

    Used ONLY when ``hermes_cli.delivery_policy`` itself is unimportable —
    the one case where ``_suppress_decision()`` / ``log_suppression()`` are
    unreachable.  Exposes the same shape the notifier reads (``suppressed``,
    ``reason``, ``detail``) so a broken policy module still FAILS CLOSED and
    the event id lands in ``_tg_suppressed_event_ids`` (wake suppressed too).
    """

    action = "suppress"

    def __init__(self, reason: str = "policy unavailable", detail: str = ""):
        self.reason = reason
        self.detail = detail

    @property
    def suppressed(self) -> bool:
        return True

    @property
    def delivered(self) -> bool:
        return False


def _fail_closed_decision(sub, ev, kind, msg, exc) -> "PolicyDecision":
    """Journal + build ``suppress("policy unavailable")`` for one event (RF1).

    Best-effort: uses the real module's ``suppress``/``log_suppression`` when
    importable (writes the standard JSONL record); when the module is
    unimportable falls back to ``_UnavailableSuppress`` and relies on the
    callers' ERROR log as the audit trail.  Never returns None.
    """
    who = str(sub.get("user_id") or sub.get("chat_id") or "")
    target = f"telegram:{sub.get('chat_id')}"
    detail = f"{type(exc).__name__}: {exc}"
    try:
        from hermes_cli.delivery_policy import log_suppression, suppress

        decision = suppress("policy unavailable", detail=detail)
        log_suppression(
            app="kanban", who=who, target=target, decision=decision,
            snippet=(msg or "")[:200],
            extra={
                "task_id": sub.get("task_id"),
                "event_kind": kind,
                "event_id": getattr(ev, "id", None),
            },
        )
        return decision
    except Exception:
        return _UnavailableSuppress("policy unavailable", detail)


def _kanban_branch_tg_owner(conn, kb, root_id: str) -> str:
    """Return the branch-root owner's TG user_id (K1 RF3), or ``""``.

    The branch owner is the user who created the root (via ``/kanban
    create``); their TG identity is recorded on the root's telegram
    subscription row.  We take the *first-created* telegram subscription on
    the root (≈ the creator) and return its ``user_id`` — profile slugs
    (``created_by``/``assignee``) are NOT comparable to TG ids, so the root's
    own telegram subscription is the only trustworthy source.  Empty string
    when there is none → caller falls back to the allowlist-only check.
    """
    try:
        subs = kb.list_notify_subs(conn, task_id=root_id)
        tg_subs = [
            s for s in subs
            if str(s.get("platform") or "").lower() == "telegram"
            and s.get("user_id")
        ]
        if not tg_subs:
            return ""
        tg_subs.sort(key=lambda s: s.get("created_at") or 0)
        return str(tg_subs[0]["user_id"])
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# api_server / Open WebUI inline report delivery
#
# The api_server adapter implements no send()/send_document(): the persisted
# chat message produced by the outbox worker is the ONLY surface a kanban
# result can appear on. The generic notifier message is a short one-liner
# (completed = ``✔ … done — <first 200 chars of summary>``), so a completed
# task's actual report never reached the chat while the outbox ACK recorded
# ``delivered``. These helpers embed the FULL summary plus the primary text
# report artifact inline, so a (re)load of the chat shows the whole result.
# ---------------------------------------------------------------------------

_OWUI_ARTIFACT_TEXT_EXTS = frozenset({".md", ".txt", ".rst", ".log"})
_OWUI_ARTIFACT_MAX_BYTES = 300_000
_OWUI_ARTIFACT_MAX_CHARS = 60_000


def _owui_artifact_text(artifacts: Any) -> str:
    """Read the first local text artifact (the board report) for inline delivery.

    api_server has no attachment channel, so the report file's text must ride
    inside the persisted chat message itself. Returns ``""`` when nothing
    readable is found (missing file, binary/oversized, non-text extension).
    """
    if artifacts is None:
        return ""
    paths = artifacts if isinstance(artifacts, (list, tuple)) else [artifacts]
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            p = Path(raw.strip())
            if not p.is_file() or p.suffix.lower() not in _OWUI_ARTIFACT_TEXT_EXTS:
                continue
            if p.stat().st_size > _OWUI_ARTIFACT_MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")[:_OWUI_ARTIFACT_MAX_CHARS]
            if text.strip():
                return f"📄 Отчёт <b>{p.name}</b>:\n\n{text}"
        except (OSError, ValueError):
            continue
    return ""


def _owui_completed_text(task_id: str, title: str, payload: Any, fallback: str) -> str:
    """Full result text for an api_server ``completed`` event.

    Returns ``fallback`` when the payload carries nothing richer than the
    generic one-liner, so payload-less events keep their existing behavior.
    """
    if not isinstance(payload, dict):
        return fallback
    parts = [f"✅ Kanban {task_id} done — {title}"]
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    art = _owui_artifact_text(payload.get("artifacts"))
    if art:
        parts.append(art)
    full = "\n\n".join(parts)
    return full if len(full) > len(fallback) else fallback


def _owui_branch_result_text(plan: dict, msg: str) -> str:
    """api_server branch итог: append the root node's report artifact inline.

    Covers ``branch_summary_replace`` mode, where the individual ``completed``
    pings (which carry the report inline) are suppressed and the branch итог
    is the only message the channel receives. Harmless no-op when the root has
    no readable completed-event artifact.
    """
    from hermes_cli import kanban_db as _kb
    root_id = plan.get("root_id")
    board = plan.get("board")
    conn = _kb.connect(board=board)
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed' "
            "ORDER BY id DESC LIMIT 1",
            (root_id,),
        ).fetchone()
        if not row or not row[0]:
            return msg
        import json as _json
        payload = _json.loads(row[0])
    except Exception:
        return msg
    finally:
        conn.close()
    if not isinstance(payload, dict):
        return msg
    art = _owui_artifact_text(payload.get("artifacts"))
    if not art:
        return msg
    return f"{msg}\n\n{art}"


#: Kinds that ALWAYS stay loud on the api_server lane, even in
#: branch_summary_replace mode.  These are either terminal-failure states the
#: branch итог lists, or explicit "needs a human decision" transitions that
#: must reach the origin OWUI session immediately (they carry no equivalent in
#: the branch_summary_message, and the whole point of ``block_loop_detected`` /
#: ``review_requested`` is to force human attention).
_OWUI_ALWAYS_LOUD_KINDS = frozenset({
    "gave_up", "crashed", "timed_out",
    "block_loop_detected", "review_requested",
})


def should_suppress_api_server_event(event, card, parent,
                                     branch_summary_replace) -> bool:
    """Pure predicate: is ``event`` an intermediate api_server-lane noise.

    With ``kanban.branch_summary_replace: true`` the api_server (OWUI) lane
    must NOT emit a per-event ping for every intermediate state change of a
    mission tree — the user only wants the terminal branch итог (delivered by
    ``_kanban_branch_summary_watcher``, which appends the root report artifact
    inline) plus anything that genuinely needs their action.

    Returns True to SUPPRESS the event (skip OWUI staging + OWUI→TG mirror +
    text ping — the event is still claimed so the subscription cursor advances
    and it can never wedge/replay), False to deliver normally.

    Suppressed (True) — INTERMEDIATE events:
      * any descendant card (``parent`` present): child / sub-branch cards,
        including abrupt mid-tree closes ([split N/M], one-off child done);
      * plain non-terminal ``completed`` / ``status`` / ``archived`` /
        ``unblocked`` and a self-resolving ``blocked`` (dependency/transient);
      * the SAME routine kinds even for ROOT / unsubscribed tasks — under
        replace mode the root's own completion ping is not the итог: the
        branch_summary stage owns that single channel message.

    Loud (False) — NOT suppressed:
      * the terminal ``branch_summary`` event itself (the whole-branch итог);
      * ``_OWUI_ALWAYS_LOUD_KINDS`` — terminal-failure / human-attention kinds
        (gave_up, crashed, timed_out, block_loop_detected, review_requested);
      * ``blocked`` that requires an operator route choice or a human fix —
        provider outage (payload ``provider_route``) or a needs-a-fix
        ``block_kind`` (``needs_input`` / ``capability`` / legacy NULL);
      * a card routed to ``triage`` (human decision needed).

    The predicate is PURE and side-effect free. ``card``/``parent`` may be
    ``None`` (row missing / no parent). Returns False whenever
    ``branch_summary_replace`` is off — replace mode OFF means additive,
    per-event notifications keep flowing exactly as before. The TG
    delivery-policy gate is deliberately OUT of scope: this predicate is
    api_server-lane only and never alters Telegram behavior.
    """
    if not branch_summary_replace:
        return False
    if event is None:
        return False
    kind = str(getattr(event, "kind", "") or "").strip()
    # An unclassifiable event (no kind) must never be silently dropped — a
    # malformed/unknown event is ambiguous, and on the api_server lane the
    # cost of losing a real terminal итог far outweighs one extra ping.
    if not kind:
        return False
    try:
        payload = getattr(event, "payload", None) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # The terminal whole-branch итог is the single channel message — it is
    # NEVER suppressed (nothing downstream re-emits it if it is).
    if kind == "branch_summary":
        return False
    # Terminal-failure / human-attention kinds always stay loud.
    if kind in _OWUI_ALWAYS_LOUD_KINDS:
        return False
    # A card routed to triage needs a human decision — never drop it, whether
    # signalled via an explicit ``triage`` event kind or the card status.
    if kind == "triage":
        return False
    card_status = str(getattr(card, "status", "") or "").strip()
    if card_status == "triage":
        return False
    if kind == "blocked":
        block_kind = str(payload.get("kind") or "").strip() or None
        if payload.get("provider_route"):
            return False  # provider outage — operator must pick a route
        if block_kind in ("needs_input", "capability"):
            return False  # human fix required
        if block_kind is None:
            # Legacy un-typed block → treated as a human blocker (same rule as
            # classify_branch_node_status / branch_state).
            return False
        # dependency / transient / any other auto-resolving kind → routine.
        return True
    # completed / status / archived / unblocked / any other routine kind —
    # intermediate on the mission tree, suppressed under replace mode
    # (including root / unsubscribed cards: the branch итог is the итог).
    return True


def _owui_suppress_intermediate_event(kind: str, ev: Any,
                                      replace_summary: bool,
                                      platform_str: str) -> bool:
    """Legacy forwarding shim for ``should_suppress_api_server_event``.

    Keeps the existing call site (which passes ``kind``/``ev``/platform
    separately) behaviourally identical while routing through the canonical
    pure predicate.  Gates on the api_server lane + replace mode first, then
    delegates; ``card``/``parent`` are unknown at this call site so they are
    passed as None (the canonical predicate still suppresses all routine
    intermediate kinds and keeps the loud set + blocked-with-provider_route /
    needs-human-fix cases).
    """
    if platform_str != "api_server" or not replace_summary:
        return False
    return should_suppress_api_server_event(
        ev, card=None, parent=None, branch_summary_replace=replace_summary,
    )


#: Live statuses for a goal_mode mission controller: while ANY of these hold
#: the mission is still in flight, so a leaf/daughter close must NOT be treated
#: as the terminal итог of the whole tree.
_MISSION_CONTROLLER_LIVE_STATUSES = frozenset({
    "todo", "ready", "running", "review", "blocked", "triage", "scheduled",
})


#: D5 (delivery-fix): detached controllers and their work cards subscribe
#: back-to-back from the same mission run; independent tasks land in the same
#: owner chat much later (or earlier). Only subscriptions created within this
#: window of each other count as same mission session.
_MISSION_SESSION_WINDOW_SECONDS = 3600


def _mission_controller_related(conn, root_id, controller_id) -> bool:
    """D5: is the live goal_mode controller part of the SAME mission as the
    closing branch root?

    Attribution paths (either makes them related):
      1. task_links: the controller is an ancestor of the root (linked tree);
      2. session window: both api_server subscriptions were created in the
         same chat within the window - the detached-controller case.

    Anything else (a live controller that merely shares the chat) is an
    INDEPENDENT task: its terminality must not be hijacked.
    """
    try:
        ancestors = kb.get_ancestor_ids(conn, root_id)
        if controller_id in ancestors:
            return True
    except Exception:
        pass
    try:
        row = conn.execute(
            "SELECT n.chat_id, n.created_at FROM kanban_notify_subs n "
            "WHERE n.task_id = ? AND n.platform = 'api_server' "
            "ORDER BY n.created_at LIMIT 1",
            (root_id,),
        ).fetchone()
        if not row or not row["chat_id"] or not row["created_at"]:
            return False
        chat, created = str(row["chat_id"]), int(row["created_at"])
        crow = conn.execute(
            "SELECT 1 FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = 'api_server' AND chat_id = ? "
            "AND ABS(created_at - ?) <= ? LIMIT 1",
            (controller_id, chat, created, _MISSION_SESSION_WINDOW_SECONDS),
        ).fetchone()
        return crow is not None
    except Exception:
        return False


def _has_live_mission_controller(conn, kb, task_id: str) -> bool:
    """True when a LIVE goal_mode mission controller belongs to the SAME
    mission tree/session as task_id (D5-scoped).

    A kanban "mission" is driven by an orchestrator controller card created
    with ``goal_mode=True`` (a Ralph-style goal loop worker).  The controller
    keeps the mission open until IT closes — so any leaf/daughter close while
    the controller is live is an INTERMEDIATE close of the whole mission, not
    its terminal итог (F9, t_85a095e0).

    Controllers are located via ``task_links`` when linked, and via a
    subscription-timing window when detached (the R4 sno-economics case:
    controller + its work cards subscribe back-to-back from one mission run).
    D5: a live controller that merely shares the chat (an independent task in
    flight) NO LONGER de-terminalizes unrelated closed trees - that chat-wide
    reading starved the owner of every terminal result while ANY mission was
    open.

    Returns ``False`` when ``task_id`` has no api_server subscription of its
    own (the signal is meaningless outside an OWUI mission) and when no live
    controller belongs to the same mission.  Fail-closed: a controller that
    cannot be attributed to this mission does not change the terminal
    decision.
    """
    row = conn.execute(
        "SELECT chat_id FROM kanban_notify_subs "
        "WHERE task_id = ? AND platform = 'api_server' ORDER BY chat_id LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row or not row["chat_id"]:
        return False
    session = str(row["chat_id"])
    # D5: candidates are live goal_mode controllers subscribed in the same
    # chat; each must additionally pass same-mission attribution before it
    # may de-terminalize this root (see _mission_controller_related).
    cand_rows = conn.execute(
        "SELECT DISTINCT t.id FROM tasks t "
        "WHERE t.goal_mode = 1 AND t.status IN (%s) "
        "AND EXISTS ("
        "  SELECT 1 FROM kanban_notify_subs n "
        "  WHERE n.task_id = t.id AND n.platform = 'api_server' "
        "  AND n.chat_id = ?"
        ")"
        % (", ".join("?" * len(_MISSION_CONTROLLER_LIVE_STATUSES))),
        (*sorted(_MISSION_CONTROLLER_LIVE_STATUSES), session),
    ).fetchall()
    for cand in cand_rows:
        cid = str(cand["id"])
        if cid == task_id:
            # The closing card IS the live controller: the mission it
            # drives is unambiguously in flight.
            return True
        if _mission_controller_related(conn, task_id, cid):
            return True
    return False


def _branch_summary_terminal(conn, kb, root_id: str) -> bool:
    """True when the branch итог for ``root_id`` is the TERMINAL итог of the
    whole mission/root tree — i.e. no open/running card remains ABOVE it.

    A branch root is the topmost *subscribed* task of a subtree (see
    ``subscribed_branch_roots``), but it can be a mid-tree node: a
    ``[split N/M]`` card, a one-off daughter card, or any sub-branch whose
    ancestor mission is still in flight.  Those intermediate closes must NOT
    reach the Telegram mirror (F6-style hygiene); OWUI api_server is
    unaffected.  The итог is terminal only when the branch's OWN tree is
    closed (guaranteed by the caller — a summary is only planned for a
    terminal ``branch_state``) AND every ancestor via ``task_links`` has its
    whole subtree terminal-closed (``branch_state(aid) is not None``).

    F9 (t_85a095e0): terminality ALSO fails when a live goal_mode mission
    controller shares the root's OWUI session — even with zero ancestors via
    ``task_links`` (a ``[split N/M]`` / daughter card whose controller is
    detached, exactly the sno-economics-v2 R4 case).  The mission is still in
    flight while its goal_mode controller is alive, regardless of the broken
    link structure.
    """
    ancestors = kb.get_ancestor_ids(conn, root_id)
    if ancestors:
        for aid in sorted(ancestors):
            if kb.branch_state(conn, aid) is None:
                # An ancestor mission tree is still open/running → intermediate.
                return False
    if _has_live_mission_controller(conn, kb, root_id):
        # A live goal_mode controller owns the same OWUI session → intermediate.
        return False
    # Top of the whole tree (no ancestors) AND no live mission controller —
    # this close IS the terminal итог.
    return True


def _branch_summary_owned_subs(
    conn, kb, root_id: str, node_ids: set, subs_by_task: dict,
    branch_roots: set,
) -> list[dict]:
    """Collect the subscriptions whose channel the branch root ``root_id`` OWNS.

    ``subscribed_branch_roots`` returns the topmost subscribed task of every
    subtree, but a shared descendant inherits notifications from EVERY parent
    (``_inherit_notify_subs`` copies each parent's routing row onto the child).
    A readback/synthetic card linked under two already-closed roots therefore
    carries BOTH roots' channels — an F8 wave regression the naïve dedupe-by-
    channel in the caller turned into **cross-contaminated** per-root packets:
    root A planned delivery to root B's channel (and vice versa) each time the
    shared descendant closed in a wave, replaying already-delivered old roots
    to Telegram as a duplicated "OWUI→TG mirror terminal итог".

    Ownership rule (extends ``subscribed_branch_roots`` to the channel level):
    a channel K (platform/chat/thread) subscribed on task T belongs to the
    BRANCH ROOT that owns K.  That is:

      1. find ``candidate`` = the TOPMOST ancestor-or-self of T that subscribes
         to K (a mid-tree node inheriting K from two parents never counts as
         the owner of K by itself);
      2. resolve ``candidate`` to its branch root = the highest ancestor-or-self
         of ``candidate`` that is in ``branch_roots`` (usually ``candidate``
         itself when it IS a branch root, e.g. root_b for the chat it owns);
      3. the root owns K iff that branch root == ``root_id``.

    Rule 2 keeps the legitimate case working: a child card carrying its OWN
    distinct channel (not shared with a sibling root) still resolves to the
    governing branch root and is delivered by it.

    Returns the deduped list of subs owned by ``root_id`` (order: deterministic
    by subtree node id for stable deliver/ack ordering).
    """
    owned: list[dict] = []
    seen_channels: set[tuple] = set()
    for tid in sorted(node_ids):
        for sub in subs_by_task.get(tid, []):
            key = kb.branch_summary_channel_key(
                sub.get("platform"), sub.get("chat_id"), sub.get("thread_id"),
            )
            if key in seen_channels:
                continue
            # (1) TOPMOST subscribed-to-`key` ancestor-or-self of `tid`.
            # `get_ancestor_ids` excludes the task itself; walk deepest→topmost
            # so a later (higher) subscribed match wins.
            candidate = None
            ancestry = [tid] + sorted(kb.get_ancestor_ids(conn, tid))
            for anc in ancestry:
                for anc_sub in subs_by_task.get(anc, []):
                    akey = kb.branch_summary_channel_key(
                        anc_sub.get("platform"), anc_sub.get("chat_id"),
                        anc_sub.get("thread_id"),
                    )
                    if akey == key:
                        candidate = anc
            if candidate is None:
                continue
            # (2) resolve `candidate` to the governing branch root (topmost
            # ancestor-or-self in branch_roots).
            owner = candidate
            for anc in [candidate] + sorted(kb.get_ancestor_ids(conn, candidate)):
                if anc in branch_roots:
                    owner = anc
            if owner == root_id:
                seen_channels.add(key)
                owned.append(sub)
    return owned


#: Terminal close kinds whose presence in a wave marks a branch root as
#: "touched" — a completed descendant, an archived node, or a needs-a-fix
#: block/gave-up that makes ``branch_state`` terminal. Any of these on a node
#: of the root's subtree means the wave can legitimately change the branch
#: close-state / итог.
_BRANCH_SUMMARY_WAVE_TOUCH_KINDS = frozenset({
    "completed", "archived", "gave_up", "blocked", "status",
})


def _branch_summary_subtree_event_since(
    conn, kb, node_ids: set, since_event_id: int,
) -> bool:
    """True when an event for any node of ``node_ids`` arrived after ``since``.

    The wave filter (t_09f66666): a branch root is replanned only when the
    current wave actually touches its subtree.  "Touched" = any close-state
    relevant event (completed / archived / gave_up / blocked / status) on the
    root itself or a descendant, with an event id strictly after ``since``.
    ``since`` is the root's own branch_summary marker event id for an
    already-delivered root, so a NEW descendant close, an archive, or a reopen
    status move all re-trigger evaluation — while a batch INSERT closing 12
    unrelated F7 readback cards does NOT touch the old root's subtree and the
    old root is skipped (the F8/F7 incident shape).
    """
    if not node_ids:
        return False
    ordered = sorted(node_ids)
    placeholders = ", ".join("?" * len(ordered))
    kinds = _BRANCH_SUMMARY_WAVE_TOUCH_KINDS
    kind_csv = ", ".join("?" * len(kinds))
    row = conn.execute(
        "SELECT 1 FROM task_events "
        f"WHERE task_id IN ({placeholders}) AND id > ? "
        f"AND kind IN ({kind_csv}) LIMIT 1",
        [*ordered, int(since_event_id), *sorted(kinds)],
    ).fetchone()
    return row is not None


def _branch_summary_skip_untouched_delivered_root(
    conn, kb, root_id: str, root_subs, marker, node_ids: set,
) -> bool:
    """Skip an already-delivered, still-closed root the wave did not touch.

    Returns ``True`` when the caller should return ``None`` (no plan/delivery)
    WITHOUT clearing/rewriting the marker.  Conditions, ALL required:

      1. a marker already exists (branch delivered at least once);
      2. every channel is already acked (nothing pending → the only reason to
         re-plan would be a fresh close, which the wave filter below decides);
      3. the branch is STILL terminal (``branch_state`` not None) — a reopen
         (even one emitted without an event row, e.g. direct SQL) must keep
         flowing into the normal reactivation path;
      4. NO close-state event for the root's subtree arrived after the marker
         was written — i.e. the current wave does not touch this root.

    This is the loop-level short-circuit for the F8/F7 incident: a batch wave
    of unrelated readback closes must not re-plan old already-delivered roots.
    """
    if marker is None:
        return False
    if kb.branch_summary_pending_subs(marker, root_subs):
        # Still pending channels → a real retry/resume; never skip.
        return False
    st = kb.branch_state(conn, root_id)
    if st is None:
        # Branch reopened → normal reactivation path must run (clears marker).
        return False
    if _branch_summary_subtree_event_since(
        conn, kb, node_ids, since_event_id=int(marker.get("_event_id") or 0),
    ):
        # The wave touched the subtree (new descendant close / archive / etc.)
        # → let the normal marker logic decide.
        return False
    return True


def _tg_owui_dual_lane(sub, ev, kind: str, msg: str, task, allowed_users: list,
                       *, task_is_root: Optional[bool] = None,
                       mission_live: bool = False,
                       home_channel: str = "") -> Optional[tuple]:
    """Complementary OWUI→Telegram delivery lane for a non-RGZ kanban mission.

    Mirror of cron T2 (``cron/scheduler.py::_augment_owui_telegram_lane`` +
    ``_tg_gate_target``, спека §8.2.2/§8.3): an ``api_server`` subscription's
    PRIMARY lane is the exact-owner OWUI chat staged in the server-owned
    outbox.  When the OWUI owner (``sub.user_id``) is server-bound to a
    Telegram chat via ``openwebui_telegram_users`` AND that chat is in
    ``TELEGRAM_ALLOWED_USERS``/home, the SAME event is also evaluated for a
    Telegram delivery to that bound chat through the SAME RGZ classifier
    (classify + verify_recipient + log_suppression).

    Returns:
      * ``None`` — no Telegram lane: not an api_server subscription, policy
        disabled, policy module unavailable (fail-closed), no binding for the
        owner, or bound chat not in ALLOWED/home.  Nothing is sent, nothing
        journaled — the mission stays OWUI-only (V8 behavior).
      * ``(decision, bound_chat)`` — ``decision.delivered`` → send ``msg`` to
        Telegram ``bound_chat``; ``decision.suppressed`` → already journaled,
        do NOT send.  ``verify_recipient`` enforces recipient == initiator
        (ИНВ1); a foreign recipient is never delivered.  Bound chat matching
        (owner → chat) is exclusive to the server registry: the chat of one
        owner can never receive another owner's mission.
    """
    if str(sub.get("platform") or "").lower() != "api_server":
        return None
    try:
        from hermes_cli.delivery_policy import (
            InitiatorRef,
            TargetRef,
            classify,
            default_context,
            log_suppression,
            owui_telegram_users,
            policy_enabled,
            verify_recipient,
        )
    except Exception as exc:
        # Fail-closed: a broken policy module must not open a new lane.
        logger.error(
            "delivery-policy: module import failed (%s) — kanban OWUI→TG "
            "lane FAIL-CLOSED (no lane)", exc,
        )
        return None
    try:
        if not policy_enabled():
            return None
        binding = owui_telegram_users() or {}
    except Exception as exc:
        # Fail-closed: конфиг/модуль недоступен — TG-ленту не добавляем.
        logger.error(
            "delivery-policy: policy_enabled()/owui_telegram_users() raised "
            "(%s) — kanban OWUI→TG lane FAIL-CLOSED (no lane)", exc,
        )
        return None
    owner_id = str(sub.get("user_id") or "").strip()
    if not owner_id:
        return None
    bound_chat = str(binding.get(owner_id) or "").strip()
    if not bound_chat:
        # Нет серверной привязки у владельца → OWUI-only (§8.3/V8).
        return None
    if not home_channel:
        home_channel = os.environ.get("TELEGRAM_HOME_CHANNEL", "") or ""
    allowed: set = {
        str(p).strip() for p in (allowed_users or ()) if str(p).strip()
    }
    if bound_chat != home_channel and bound_chat not in allowed:
        return None
    initiator = InitiatorRef.from_origin({
        "platform": "api_server",
        "chat_id": sub.get("chat_id") or "",
        "user_id": owner_id,
        "thread_id": sub.get("thread_id") or "",
    })
    target = TargetRef.from_dict({
        "platform": "telegram",
        "chat_id": bound_chat,
        "thread_id": None,
    })
    payload = getattr(ev, "payload", None) or {}
    artifacts = list(payload.get("artifacts") or ()) if isinstance(payload, dict) else []
    task_terminal = bool(task and getattr(task, "status", None) == "archived")
    root_closing = bool(task_is_root and kind == "completed")
    # F9 (t_85a095e0): a live goal_mode controller in the same OWUI session
    # keeps the mission in flight — a root card's completed must not be the
    # terminal итог to Telegram while the controller is alive (the R4
    # sno-economics case: parent-less leaf, controller detached from
    # task_links, yet the mission was running).
    if root_closing and mission_live:
        root_closing = False
    completed_terminal = bool(task_terminal or root_closing)
    # F6 (tg-delivery-hygiene, применяется ко ВСЕМ binding
    # openwebui_telegram_users): в TG-ленте (dual-lane) доставляется ТОЛЬКО
    # терминальный итог миссии / корневой карточки.  Промежуточный completed
    # дочерней карточки — даже с артефактами сплита (отчёты/диффы/тестлоги) —
    # в TG подавляется (ИНВ2, reason=routine status close — прецедент в
    # classify); OWUI-лента (primary) при этом НЕ затрагивается.
    # Гейт стоит ДО classify, чтобы не срабатывала артефактная ветка:
    # completed терминален только когда закрывается корень ветки
    # (task_is_root) либо карточка архивирована (task.status==archived).
    if kind == "completed" and not completed_terminal:
        _suppr = _suppress_decision(
            "routine status close",
            detail=(
                "intermediate completed child — non-terminal mission card "
                "suppressed in OWUI→TG lane"
            ),
        )
        log_suppression(
            app="kanban",
            who=str(sub.get("user_id") or sub.get("chat_id") or ""),
            target=f"telegram:{bound_chat}",
            decision=_suppr,
            snippet=(msg or "")[:200],
            extra={
                "task_id": sub.get("task_id"),
                "event_kind": kind,
                "event_id": getattr(ev, "id", None),
                "owui_to_tg": True,
            },
        )
        logger.info(
            "delivery-policy: OWUI→TG lane suppressed intermediate completed "
            "for %s to telegram:%s (not terminal mission card)",
            sub.get("task_id"), bound_chat,
        )
        return (_suppr, bound_chat)
    ctx = default_context(
        kind="kanban",
        initiator=initiator,
        target=target,
        home_channel=home_channel,
        allowed_users=allowed_users,
        owui_to_tg=binding,
        event_kind=kind,
        artifacts=artifacts,
        has_media=bool(artifacts),
        completed_terminal=completed_terminal,
        no_agent=False,
    )
    decision = classify(msg, ctx)
    if not decision.suppressed:
        if verify_recipient(
            initiator, target,
            home_channel=home_channel,
            allowed_users=allowed_users,
            owui_to_tg=binding,
        ):
            return (decision, bound_chat)
        decision = _suppress_decision(
            "recipient mismatch",
            detail=f"cross-platform api_server→telegram target={bound_chat}",
        )
    log_suppression(
        app="kanban",
        who=str(sub.get("user_id") or sub.get("chat_id") or ""),
        target=f"telegram:{bound_chat}",
        decision=decision,
        snippet=(msg or "")[:200],
        extra={
            "task_id": sub.get("task_id"),
            "event_kind": kind,
            "event_id": getattr(ev, "id", None),
            "owui_to_tg": True,
        },
    )
    return (decision, bound_chat)


def _branch_summary_owui_tg_lane(sub, plan: dict, msg: str, allowed_users: list,
                                 *, home_channel: str = "") -> Optional[tuple]:
    """Complementary OWUI→Telegram mirror for a kanban branch итог.

    F7 do-implementation (t_ebc0a351): the branch-summary watcher delivers the
    итог to every subscribed channel of a branch root.  For an OWUI mission the
    root is subscribed ONLY via ``api_server`` (0 native telegram rows, verified
    live in t_2a586264) — so the terminal итог never reached Telegram even when
    the OWUI owner was server-bound via ``openwebui_telegram_users``.  This
    lane mirrors F6 semantics: when the branch итог is TERMINAL (the whole
    mission/root tree is closed, ``plan["terminal"]``), the SAME итог is also
    sent to the owner's bound Telegram chat; when it is INTERMEDIATE (a
    [split N/M] or one-off daughter branch whose ancestor mission is still
    running), the Telegram mirror is suppressed with a journal entry
    (reason=..., owui_to_tg=True).  The OWUI api_server PRIMARY lane is never
    touched.

    Returns:
      * ``None`` — no Telegram lane: not an api_server subscription, policy
        disabled, policy module unavailable (fail-closed), no binding for the
        owner, or bound chat not in ALLOWED/home.  Mission stays OWUI-only.
      * ``(decision, bound_chat)`` — ``decision.delivered`` → send ``msg`` to
        Telegram ``bound_chat``; ``decision.suppressed`` → already journaled,
        do NOT send.  ``verify_recipient`` enforces recipient == initiator
        (ИНВ1); a foreign recipient is never delivered.
    """
    if str(sub.get("platform") or "").lower() != "api_server":
        return None
    try:
        from hermes_cli.delivery_policy import (
            InitiatorRef,
            TargetRef,
            classify,
            default_context,
            log_suppression,
            owui_telegram_users,
            policy_enabled,
            verify_recipient,
        )
    except Exception as exc:
        # Fail-closed: a broken policy module must not open a new lane.
        logger.error(
            "delivery-policy: module import failed (%s) — kanban branch summary "
            "OWUI→TG lane FAIL-CLOSED (no lane)", exc,
        )
        return None
    try:
        if not policy_enabled():
            return None
        binding = owui_telegram_users() or {}
    except Exception as exc:
        # Fail-closed: конфиг/модуль недоступен — TG-ленту не добавляем.
        logger.error(
            "delivery-policy: policy_enabled()/owui_telegram_users() raised "
            "(%s) — kanban branch summary OWUI→TG lane FAIL-CLOSED (no lane)",
            exc,
        )
        return None
    owner_id = str(sub.get("user_id") or "").strip()
    if not owner_id:
        return None
    bound_chat = str(binding.get(owner_id) or "").strip()
    if not bound_chat:
        # Нет серверной привязки у владельца → OWUI-only (§8.3/V8).
        return None
    if not home_channel:
        home_channel = os.environ.get("TELEGRAM_HOME_CHANNEL", "") or ""
    allowed: set = {
        str(p).strip() for p in (allowed_users or ()) if str(p).strip()
    }
    if bound_chat != home_channel and bound_chat not in allowed:
        return None
    initiator = InitiatorRef.from_origin({
        "platform": "api_server",
        "chat_id": sub.get("chat_id") or "",
        "user_id": owner_id,
        "thread_id": sub.get("thread_id") or "",
    })
    target = TargetRef.from_dict({
        "platform": "telegram",
        "chat_id": bound_chat,
        "thread_id": None,
    })
    root_id = plan.get("root_id")
    terminal = bool(plan.get("terminal"))
    # F6-style terminal gate applied to the branch итог (t_ebc0a351): the
    # OWUI→TG mirror carries ONLY the terminal итог of the whole mission/root
    # tree.  An intermediate branch close ([split N/M], one-off daughter card,
    # sub-branch of a still-running mission) is suppressed — journaled, never
    # sent to the owner's Telegram — while the OWUI api_server PRIMARY lane is
    # unaffected.  Same reason/detail as F6 (routine status close) so the
    # suppression journal stays a single consistent audit stream.
    if not terminal:
        _suppr = _suppress_decision(
            "routine status close",
            detail=(
                "intermediate branch summary — non-terminal mission tree "
                "suppressed in OWUI→TG lane"
            ),
        )
        log_suppression(
            app="kanban",
            who=str(sub.get("user_id") or sub.get("chat_id") or ""),
            target=f"telegram:{bound_chat}",
            decision=_suppr,
            snippet=(msg or "")[:200],
            extra={
                "task_id": root_id,
                "event_kind": "branch_summary",
                "event_id": plan.get("marker_id") or 0,
                "owui_to_tg": True,
            },
        )
        logger.info(
            "delivery-policy: OWUI→TG lane suppressed intermediate branch "
            "summary for %s to telegram:%s (non-terminal mission tree)",
            root_id, bound_chat,
        )
        return (_suppr, bound_chat)
    ctx = default_context(
        kind="kanban",
        initiator=initiator,
        target=target,
        home_channel=home_channel,
        allowed_users=allowed_users,
        owui_to_tg=binding,
        event_kind="branch_summary",
        artifacts=[],
        has_media=False,
        completed_terminal=True,
        no_agent=False,
    )
    decision = classify(msg, ctx)
    if not decision.suppressed:
        if verify_recipient(
            initiator, target,
            home_channel=home_channel,
            allowed_users=allowed_users,
            owui_to_tg=binding,
        ):
            return (decision, bound_chat)
        decision = _suppress_decision(
            "recipient mismatch",
            detail=f"cross-platform api_server→telegram target={bound_chat}",
        )
    log_suppression(
        app="kanban",
        who=str(sub.get("user_id") or sub.get("chat_id") or ""),
        target=f"telegram:{bound_chat}",
        decision=decision,
        snippet=(msg or "")[:200],
        extra={
            "task_id": root_id,
            "event_kind": "branch_summary",
            "event_id": plan.get("marker_id") or 0,
            "owui_to_tg": True,
        },
    )
    return (decision, bound_chat)


def _run_in_fresh_context(func: Callable[..., Any], /, *args: Any) -> Any:
    """Run *func* in an empty ``Context`` so request-local ContextVars stay behind.

    ``asyncio.to_thread`` copies the calling task's context onto the worker
    thread. Supervised Kanban ticks are process-owned writers; if that copy
    still carries a ``delegate_task`` child marker, ``write_txn``
    false-trips. Since watchers spawn from a fresh ``Context``
    (``_spawn_supervised``), this offload-boundary scrub is defense in
    depth: it covers non-supervised spawn paths and any task context frozen
    before spawn isolation shipped. An empty Context keeps the DB guard
    intact for real children without exempting dispatcher writes.
    """
    return Context().run(func, *args)


async def _to_thread_process_service(func: Callable[..., Any], /, *args: Any) -> Any:
    """Offload blocking process-service work (dispatcher + notifier writers)
    without inheriting request-local ContextVars."""
    return await asyncio.to_thread(_run_in_fresh_context, func, *args)


def _acquire_singleton_lock(lock_path) -> "tuple[Optional[object], str]":
    """Take an exclusive, non-blocking advisory lock for the sole dispatcher.

    Only one gateway process machine-wide may run the embedded kanban
    dispatcher: concurrent dispatchers double the reclaim frequency (each
    runs its own ``release_stale_claims`` → promote → dispatch loop), double
    claim-attempt events in the event log, and — with ``wal_autocheckpoint=0`` —
    concurrent manual WAL checkpoints can corrupt index pages. The
    ``dispatch_in_gateway`` config flag is the primary control; this lock is the
    backstop that survives config drift and same-profile restart races.

    Delegates to :func:`gateway.status._try_acquire_file_lock` (``fcntl`` on
    POSIX, ``msvcrt`` on Windows) so the guard is cross-platform.

    Returns ``(handle, "held")`` on success — the caller keeps the file handle
    for the process lifetime and **must** release it via
    :func:`_release_singleton_lock` when done. ``(None, "contended")`` when
    another process holds the lock (caller must NOT dispatch). ``(None,
    "unavailable")`` when locking cannot be performed (non-POSIX filesystem
    without flock, or the status.py helpers are unimportable) — caller falls
    back to config-only control.
    """
    try:
        from gateway.status import _try_acquire_file_lock  # deferred; same package
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    return handle, "held"


def _release_singleton_lock(handle) -> None:
    """Release a dispatcher singleton lock acquired via :func:`_acquire_singleton_lock`."""
    if handle is None:
        return
    try:
        from gateway.status import _release_file_lock
        _release_file_lock(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def _wake_scope_id(adapter: Any, sub: dict) -> Optional[str]:
    """Return the tenant scope (Slack workspace) a subscription's wake keys to.

    ``build_session_key()`` includes ``SessionSource.scope_id`` on platforms
    where one bot serves several isolated tenants, so a wake source must carry
    the same scope as inbound messages from that chat to resolve to the same
    session.

    The subscription's persisted ``delivery_metadata`` wins over the adapter's
    live chat → scope mapping, because it records the scope the subscription was
    created from; the mapping only covers rows that stored no metadata. ``None``
    means the chat has no scope, which is what an unscoped platform's key
    contains.
    """
    delivery_meta = sub.get("delivery_metadata")
    if isinstance(delivery_meta, dict):
        for key in ("scope_id", "slack_team_id", "team_id"):
            value = delivery_meta.get(key)
            if value:
                return str(value)
    resolver = getattr(adapter, "scope_id_for_chat", None)
    if callable(resolver):
        try:
            resolved = resolver(str(sub.get("chat_id") or ""))
        except Exception as exc:
            # An adapter-side lookup failure yields no scope, never an error.
            logger.debug(
                "kanban notifier: scope lookup failed for chat %s: %s",
                sub.get("chat_id"),
                exc,
                exc_info=True,
            )
            return None
        if resolved:
            return str(resolved)
    return None


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _owns_kanban_dispatcher_lock(self) -> bool:
        """Return whether this gateway currently owns the singleton lock."""
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        _release_singleton_lock(handle)

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the terminal set (``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``,
        ``review_requested``, ``block_loop_detected``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. The subscription is removed only when the
        task is ``archived``. A ``done`` task can be reopened for review or
        continuation, so its subscription and origin-session ownership must
        survive completion. Cursor advancement prevents old events replaying
        when that happens.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Each gateway polls only subscriptions owned by profiles whose
        adapters it hosts. The dispatch-owning gateway also handles legacy
        subscriptions without a profile stamp.
        """
        # Dispatch and delivery have separate ownership. A deployment may run
        # one dispatcher while each profile has its own gateway credentials;
        # those adapter-owning gateways must still poll and deliver their own
        # subscriptions. Legacy rows without a notifier_profile are visible
        # only while this process holds the actual singleton dispatcher lock.
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        # "status" covers dashboard drag-drop and `_set_status_direct()`
        # writes — surface those transitions to subscribers too.
        # ``review_requested`` wakes the origin subscriber like a block does,
        # but is not a block (see kanban_db.request_review); the task is not
        # archived, so the subscription stays alive and later review
        # cycles keep notifying.
        TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status", "archived", "unblocked", "block_loop_detected", "review_requested")
        # Subscriptions are removed only when the task reaches the irreversible
        # archived status. ``done`` is reversible in review/controller flows,
        # so removing its subscription would silence a later reopen. We used
        # to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is archived lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        # Raised from 3 to 12 (~60s at the 5s tick cadence): now that a
        # reported SendResult(success=False) also lands here (see the
        # delivery loop below), a transient Telegram/API outage of a few
        # ticks must NOT permanently unsubscribe a live review-gate channel.
        # A genuinely dead chat still drops, just ~60s later — a fine trade
        # for an unattended gate where a false drop means silent work pileup.
        MAX_SEND_FAILURES = 12
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        # Per-source-event api_server staging retry budget (TTL). The exact
        # api_server branch below rewinds the notify cursor on any
        # non-acknowledged stage result so a server-owned readback can gate
        # advancement. A permanently-undeliverable event (missing/unbound
        # registry, outbox row stuck in a bad state) would otherwise rewind
        # and re-claim the SAME event every 5s tick forever, spamming
        # "delivery ...; cursor retained" at WARNING (incident 2026-10-08/09,
        # 12,689 identical lines for one event in ~24h). After
        # MAX_API_STAGE_RETRIES consecutive non-acknowledged results for one
        # (task, event) the notifier gives up on it: it advances the cursor
        # past the event (the row stays in the outbox for independent drain)
        # and logs the drop once, instead of pinning the cursor forever.
        MAX_API_STAGE_RETRIES = 8
        api_stage_counts: dict[tuple, int] = getattr(
            self, "_kanban_api_stage_failures", {}
        )
        self._kanban_api_stage_failures = api_stage_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        # Stale done-sub GC cadence. Subscriptions survive ``done`` (it is
        # reversible), so boards that never archive would otherwise
        # accumulate rows scanned on every 5s tick forever. The sweep is a
        # single DELETE per board, gated to once per watcher startup and at
        # most once per hour thereafter — cheap relative to the tick's own
        # per-sub claims. Retention is kanban.done_sub_retention_days in
        # config.yaml (default 30; 0 disables), re-read at each sweep so a
        # config change applies without a restart.
        _GC_INTERVAL_SECONDS = 3600.0
        _gc_next_at = 0.0  # 0 → sweep on the first tick after startup

        while self._running:
            try:
                # Opt-in privileged local registry: drain cron and Kanban
                # outbox even if the board has no subscriptions this tick.
                # A missing registry never turns an HTTP self-post into ACK.
                _owui_registry = os.environ.get("HERMES_OPENWEBUI_DB_PATH")
                if (_owui_registry
                        and os.environ.get("HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED") == "1"):
                    from hermes_constants import get_hermes_home
                    from gateway.origin_delivery_worker import drain_openwebui
                    await asyncio.to_thread(
                        drain_openwebui,
                        get_hermes_home() / "delivery-outbox.sqlite",
                        _owui_registry,
                    )
                _gc_due = time.monotonic() >= _gc_next_at
                _gc_retention_days = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    try:
                        from hermes_cli.config import load_config as _load_cfg

                        _kanban_cfg = (_load_cfg() or {}).get("kanban") or {}
                        _gc_retention_days = int(
                            _kanban_cfg.get("done_sub_retention_days", 30)
                        )
                    except Exception:
                        # Fail safe on the shipped default; the sweep itself
                        # treats <= 0 as disabled.
                        _gc_retention_days = 30

                def _collect():
                    deliveries: list[dict] = []
                    include_unowned = self._owns_kanban_dispatcher_lock()
                    notifier_profiles = {notifier_profile}
                    notifier_profiles.update(
                        str(profile).strip()
                        for profile in getattr(self, "_profile_adapters", {})
                        if str(profile).strip()
                    )
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    # Widen to every platform any secondary profile has live,
                    # not just the default profile's. This is only a coarse
                    # pre-filter to skip claiming events for subs nobody can
                    # possibly deliver — the precise per-profile check (via
                    # gateway/authz_mixin.py::_authorization_adapter, which
                    # forbids default-profile fallback) still runs at delivery
                    # time below, rewinding the claim if it resolves to None.
                    # Without this, a subscription owned by a secondary
                    # profile on a platform the DEFAULT profile never
                    # connected (e.g. beta owns discord, default doesn't) was
                    # dropped here before ever being claimed — no rewind
                    # applies to an unclaimed event, so it silently never
                    # retries.
                    for _profile_adapter_map in getattr(self, "_profile_adapters", {}).values():
                        active_platforms.update(
                            getattr(platform, "value", str(platform)).lower()
                            for platform in _profile_adapter_map.keys()
                        )
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        # Zero-subscription early exit: probe the board with a
                        # cheap read-only connection BEFORE the writable
                        # `connect()`. A board with no subscriptions has
                        # nothing to notify, and the writable open (schema
                        # init/migration on first open, WAL/-shm sidecars,
                        # checkpoint traffic) is exactly the per-tick cost
                        # this skip avoids.
                        try:
                            if _kb.count_notify_subs(
                                board=slug,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            ) == 0:
                                logger.debug(
                                    "kanban notifier: board %s has no subscriptions owned by %s; skipping open",
                                    slug, sorted(notifier_profiles),
                                )
                                continue
                        except Exception as exc:
                            logger.debug(
                                "kanban notifier: read-only subscription probe failed "
                                "for board %s (%s); falling back to writable open",
                                slug, exc,
                            )
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            if _gc_due:
                                # Hourly (plus once at startup) stale-sub GC:
                                # drop subscriptions for tasks that have been
                                # ``done`` untouched past the retention
                                # window. Best-effort — a failed sweep never
                                # blocks delivery; the next hourly gate
                                # retries it.
                                try:
                                    _purged = _kb.purge_stale_done_notify_subs(
                                        conn,
                                        max_age_days=_gc_retention_days,
                                    )
                                    if _purged:
                                        logger.info(
                                            "kanban notifier: purged %d stale done-task subscription(s) on board %s (retention %dd)",
                                            _purged, slug, _gc_retention_days,
                                        )
                                except Exception as _gc_exc:
                                    logger.debug(
                                        "kanban notifier: stale-sub GC failed for board %s: %s",
                                        slug, _gc_exc,
                                    )
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(
                                conn,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            )
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            replace_summary = _kanban_branch_summary_replace()
                            subscribed_ids = {str(sub.get("task_id")) for sub in subs}
                            for sub in subs:
                                try:
                                    owner_profile = sub.get("notifier_profile") or None
                                    if owner_profile and owner_profile != notifier_profile:
                                        _owner_adapters = getattr(self, "_profile_adapters", {}).get(owner_profile)
                                        if not _owner_adapters:
                                            logger.debug(
                                                "kanban notifier: subscription for %s owned by profile %s; current profile %s has no adapter for it, skipping",
                                                sub.get("task_id"), owner_profile, notifier_profile,
                                            )
                                            continue
                                    platform = (sub.get("platform") or "").lower()
                                    if platform not in active_platforms:
                                        logger.debug(
                                            "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                            sub.get("task_id"), platform or "<missing>",
                                        )
                                        continue
                                    old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                        conn,
                                        task_id=sub["task_id"],
                                        platform=sub["platform"],
                                        chat_id=sub["chat_id"],
                                        thread_id=sub.get("thread_id") or "",
                                        kinds=TERMINAL_KINDS,
                                    )
                                    if not events:
                                        continue
                                    task = _kb.get_task(conn, sub["task_id"])
                                    # Synthetic readback cards (do_not_deliver=1,
                                    # e.g. [F7 readback] verification fixtures)
                                    # must NEVER be delivered to any channel:
                                    # no text ping, no api_server staging, no
                                    # wake. The events stay CLAIMED so the
                                    # cursor advances past them below (same
                                    # non-replay semantics as the `silent`
                                    # branch_summary_replace skip) — they can
                                    # never wedge the subscription.
                                    do_not_deliver = _task_do_not_deliver(task)
                                    # K1 RF2/RF3: expose branch-root marker and
                                    # the branch owner's TG id to the policy
                                    # gate. A task is a branch ROOT when NONE
                                    # of its ancestors carries a subscription
                                    # (same rule as subscribed_branch_roots).
                                    # The branch OWNER is the creator of the
                                    # topmost root — resolved by walking the
                                    # parent chain to the top (RF3); the root's
                                    # first-created telegram subscription
                                    # identifies that creation user.
                                    task_is_root = False
                                    task_tg_owner = ""
                                    # F9 (t_85a095e0): a LIVE goal_mode
                                    # mission controller sharing the task's
                                    # OWUI session keeps the mission in flight
                                    # even without a task_links ancestor — so a
                                    # parent-less root card's completed event
                                    # must NOT be treated as the terminal итог
                                    # (the R4 sno-economics case). Computed here
                                    # where conn is available, threaded through
                                    # to both terminality gates below.
                                    mission_live = _has_live_mission_controller(
                                        conn, _kb, sub["task_id"],
                                    )
                                    # Branch-root detection is shared by both
                                    # delivery lanes. The telegram gate needs
                                    # it for K1 RF2 terminality; the OWUI→TG
                                    # dual lane (non-RGZ missions, T2 parity,
                                    # §8.2.2) treats a root-closing completed
                                    # as terminal exactly the same way.
                                    if platform in ("telegram", "api_server"):
                                        _top = sub["task_id"]
                                        _depth = 0
                                        while True:
                                            _parents = _kb.parent_ids(conn, _top)
                                            if not _parents or _depth >= 32:
                                                break
                                            _top = _parents[0]
                                            _depth += 1
                                        task_is_root = _top == sub["task_id"]
                                        if platform == "telegram":
                                            # The branch OWNER is the creator
                                            # of the topmost root — resolved
                                            # by walking the parent chain to
                                            # the top (RF3). For api_server the
                                            # owner identity comes from the
                                            # server-owned binding instead, so
                                            # no telegram lookup applies.
                                            task_tg_owner = _kanban_branch_tg_owner(
                                                conn, _kb, _top,
                                            )
                                    # branch_summary_replace mode: tasks that
                                    # are a strict descendant of a subscribed
                                    # branch root have their individual pings
                                    # suppressed — the whole-branch итог
                                    # (delivered by
                                    # _kanban_branch_summary_watcher) is the
                                    # single channel message. compute once per
                                    # subscription.
                                    silent = False
                                    if replace_summary:
                                        silent = any(
                                            pid in subscribed_ids
                                            for pid in _kb.parent_ids(conn, sub["task_id"])
                                        )
                                    logger.debug(
                                        "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s%s",
                                        len(events), sub["task_id"], slug, old_cursor, cursor,
                                        " (silent: branch_summary_replace)" if silent else "",
                                    )
                                    deliveries.append({
                                        "sub": sub,
                                        "old_cursor": old_cursor,
                                        "cursor": cursor,
                                        "events": events,
                                        "task": task,
                                        "board": slug,
                                        "silent": silent,
                                        "replace_summary": replace_summary,
                                        "do_not_deliver": do_not_deliver,
                                        "task_is_root": task_is_root,
                                        "mission_live": mission_live,
                                        "task_tg_owner": task_tg_owner,
                                    })
                                except Exception as sub_exc:
                                    # Isolate per-subscription failures so one
                                    # bad subscription cannot block delivery for
                                    # all other subscriptions in this tick.
                                    logger.warning(
                                        "kanban notifier: subscription for %s on board %s failed: %s",
                                        sub.get("task_id"), slug, sub_exc,
                                    )
                        finally:
                            conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    sub_profile = sub.get("notifier_profile") or ""
                    # Route via the SAME chokepoint the authorization path uses
                    # (gateway/authz_mixin.py::_authorization_adapter): a stamped
                    # profile with its own adapter-registry entry must be served
                    # by THAT profile's same-platform adapter and must NOT silently
                    # fall back to the default profile's adapter — otherwise a
                    # secondary profile's task notification is delivered by the
                    # wrong bot (the cross-profile mis-delivery this whole change
                    # exists to fix). The helper returns None only when the profile
                    # (or default) genuinely has no adapter for the platform.
                    adapter = self._authorization_adapter(plat, sub_profile or None)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await _to_thread_process_service(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    board_tag = f"[{board_slug}] " if board_slug else ""
                    # API notifications are staged by exact event id below.
                    # A self-post cannot deliver into the owner's browser; the
                    # outbox ACK alone allows the provisional cursor to advance.
                    # Per-subscription failure-counter key for push delivery
                    # and non-API wake failures.
                    sub_key = (
                        sub["task_id"], sub["platform"],
                        sub["chat_id"], sub.get("thread_id") or "",
                    )
                    mode = sub.get("delivery_mode") or "notify"
                    # API self-post is NOT a user-visible receipt. Its exact
                    # terminal event goes through the persisted chat outbox.
                    wake_agent = mode in ("notify+wake", "wake") and platform_str != "api_server"
                    send_passive = mode != "wake" or platform_str == "api_server"
                    if d.get("silent") or d.get("do_not_deliver"):
                        # silent (branch_summary_replace): nothing to send or
                        # wake for this subscription — the branch итог is the
                        # single channel message.  do_not_deliver: synthetic
                        # readback cards are never a delivery subject at all.
                        # Either way nothing is sent or woken, and the cursor
                        # still advances below (events already claimed).
                        wake_agent = False
                        send_passive = False
                    # Worker handoff carried into the synthetic wake turn below
                    # (#70752): without it the woken creator only sees
                    # "Task X completed" and re-decomposes work that already
                    # exists on the board.
                    wake_handoff = ""
                    # TG delivery-policy gate (D0 F1): allowed users and the
                    # set of event ids suppressed for THIS subscription, so the
                    # wake computation below never wakes on a suppressed event.
                    _tg_allowed = _tg_delivery_policy_allowed_users()
                    _tg_suppressed_event_ids: set = set()
                    for ev in d["events"]:
                        kind = ev.kind
                        if (d.get("silent") and kind in TERMINAL_KINDS) or d.get("do_not_deliver"):
                            # silent (branch_summary_replace): the branch итог
                            # is the single channel message. do_not_deliver:
                            # synthetic readback cards are never delivered.
                            # Either way the event is claimed so the cursor
                            # advances below and it can never replay/wedge
                            # the sub.
                            continue
                        if _owui_suppress_intermediate_event(
                            kind, ev, bool(d.get("replace_summary")),
                            platform_str,
                        ):
                            # api_server lane in branch_summary_replace mode:
                            # an intermediate state change (plain completed/
                            # status/archived/unblocked, self-resolving blocked)
                            # stays claimed+silent — the branch итог from
                            # _kanban_branch_summary_watcher is the single
                            # channel message, and human-action kinds are kept
                            # loud by the helper. Nothing is staged to the
                            # OWUI outbox and the OWUI→TG mirror is skipped
                            # (the same event must not leak to TG either).
                            continue
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        if kind == "completed":
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                try:
                                    # RESULT-BLOCK: показать первую FACTS-строку
                                    # (компактная сводка) вместо сырого "RESULT-BLOCK v1".
                                    from hermes_cli.handoff_result_block import chat_line as _hrb_chat
                                    h = _hrb_chat(payload_summary) or payload_summary
                                except Exception:
                                    h = payload_summary
                                if len(h) > 200:
                                    h = h[:200]
                                handoff = f"\n{h}"
                                wake_handoff = h
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                                wake_handoff = r
                            msg = (
                                f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                if ev.payload.get("provider_route"):
                                    # A provider outage requires an actual
                                    # operator choice. Never truncate the
                                    # question before the alternative is shown.
                                    reason = f": {str(ev.payload['reason'])[:500]}"
                                    wake_handoff = str(ev.payload["reason"])[:500]
                                else:
                                    reason = f": {str(ev.payload['reason'])[:160]}"
                            msg = f"⏸ {board_tag}{tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            msg = (
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} gave up "
                                f"after repeated spawn failures{err}"
                            )
                        elif kind == "crashed":
                            msg = (
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} worker crashed "
                                f"(pid gone); dispatcher will retry"
                            )
                        elif kind == "timed_out":
                            limit = 0
                            if ev.payload and ev.payload.get("limit_seconds"):
                                limit = int(ev.payload["limit_seconds"])
                            msg = (
                                f"⏱ {board_tag}{tag}Kanban {sub['task_id']} timed out "
                                f"(max_runtime={limit}s); will retry"
                            )
                        elif kind == "status":
                            new_status = ""
                            if ev.payload and ev.payload.get("status"):
                                new_status = str(ev.payload["status"])
                            msg = f"🔄 {board_tag}{tag}Kanban {sub['task_id']} → {new_status}"
                        elif kind == "review_requested":
                            # Implementation complete; task moved to the
                            # first-class review lane. Wake the origin thread.
                            handoff = ""
                            if ev.payload and ev.payload.get("summary"):
                                try:
                                    # RESULT-BLOCK: компактная сводка из FACTS.
                                    from hermes_cli.handoff_result_block import chat_line as _hrb_chat
                                    handoff = f"\n{(_hrb_chat(str(ev.payload['summary'])) or str(ev.payload['summary']))[:200]}"
                                except Exception:
                                    handoff = f"\n{str(ev.payload['summary'])[:200]}"
                            msg = (
                                f"👀 {board_tag}{tag}Kanban {sub['task_id']} ready for review"
                                f" — {title}{handoff}"
                            )
                        elif kind == "block_loop_detected":
                            # A task re-blocked for the same cause past the
                            # recurrence limit and was routed to `triage` for a
                            # human decision. This is the ONE transition that
                            # exists to force human attention, yet it emits no
                            # `blocked`/`status` event — so before adding it to
                            # TERMINAL_KINDS it produced zero notification and
                            # the task stalled in triage silently. Ping loudly.
                            reason = ""
                            recurrences = None
                            if ev.payload:
                                if ev.payload.get("reason"):
                                    reason = f": {str(ev.payload['reason'])[:160]}"
                                recurrences = ev.payload.get("recurrences")
                            rc = f" (blocked {recurrences}x for the same cause)" if recurrences else ""
                            msg = (
                                f"🛑 {board_tag}{tag}Kanban {sub['task_id']} routed to TRIAGE"
                                f" — needs a human decision{rc}{reason}"
                            )
                        else:
                            # archived / unblocked are claimed by TERMINAL_KINDS
                            # (so the cursor advances past them and they can't
                            # wedge a later completed/blocked event behind an
                            # unclaimed row) but are intentionally SILENT: an
                            # archive needs no user ping, and unblocked is an
                            # internal transition. They are also excluded from
                            # _WAKE_KINDS below, so they never wake the creator.
                            continue

                        # TG delivery-policy gate (D0 F1, ИНВ1/ИНВ2): skip
                        # recipient-mismatched and routine telegram events
                        # (text send AND wake). api_server events are handled
                        # in their own branch below — the gate never applies
                        # to them (server-owned registry verifies them).
                        _tg_policy = _tg_notify_policy_decision(
                            sub, ev, kind, msg, task, _tg_allowed,
                            task_is_root=d.get("task_is_root"),
                            mission_live=bool(d.get("mission_live")),
                            task_tg_owner=d.get("task_tg_owner") or "",
                        )
                        if _tg_policy is not None and _tg_policy.suppressed:
                            _tg_suppressed_event_ids.add(getattr(ev, "id", None))
                            logger.info(
                                "kanban notifier: telegram event %s for %s suppressed by delivery policy (%s)",
                                kind, sub["task_id"],
                                _tg_policy.reason or "unspecified",
                            )
                            continue

                        delivery_metadata = sub.get("delivery_metadata")
                        metadata: dict[str, Any] = (
                            dict(delivery_metadata)
                            if isinstance(delivery_metadata, dict)
                            else {}
                        )

                        if sub.get("thread_id") and not metadata.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        if platform_str == "api_server":
                            if kind == "completed":
                                # The persisted chat message is the ONLY surface
                                # for the api_server result — embed the full
                                # summary + report artifact inline instead of the
                                # short notifier excerpt.
                                msg = _owui_completed_text(
                                    sub["task_id"], title, ev.payload or {}, msg,
                                )
                            # A claimed cursor is only provisional. Stage one
                            # exact event and wait for server-owned chat readback
                            # before letting the cursor move. Repeated ticks
                            # never create another message/agent turn.
                            from hermes_constants import get_hermes_home
                            from gateway.kanban_origin_delivery import stage_terminal
                            _registry = os.environ.get("HERMES_OPENWEBUI_DB_PATH")
                            _state = "unbound"
                            if _registry and (not sub_profile or sub_profile == notifier_profile):
                                _state, _delivery_id = await asyncio.to_thread(
                                    stage_terminal,
                                    get_hermes_home() / "delivery-outbox.sqlite",
                                    _registry, sub, sub_profile or notifier_profile,
                                    board_slug or "default", ev.id, msg,
                                )
                            if _state != "acknowledged":
                                _stage_key = (sub["task_id"], ev.id)
                                _stage_fails = api_stage_counts.get(_stage_key, 0) + 1
                                api_stage_counts[_stage_key] = _stage_fails
                                # Every non-acknowledged result counts toward
                                # one budget — pending (staged, awaiting
                                # server-owned readback that normally lands next
                                # tick) AND failed/unbound. Dropping past a
                                # pending event is lossless: the row is already
                                # persisted in the outbox and the delivery
                                # worker performs the actual chat-write/ack
                                # independently of this notifier cursor.
                                if _stage_fails >= MAX_API_STAGE_RETRIES:
                                    logger.warning(
                                        "kanban notifier: exact api_server event %s/%s "
                                        "stuck at delivery %s after %d ticks; dropping "
                                        "event (cursor advances, row stays in outbox)",
                                        sub["task_id"], ev.id, _state, _stage_fails,
                                    )
                                    api_stage_counts.pop(_stage_key, None)
                                    await _to_thread_process_service(
                                        self._kanban_advance, sub, d["cursor"], board_slug,
                                    )
                                else:
                                    logger.warning(
                                        "kanban notifier: exact api_server event %s/%s "
                                        "delivery %s; cursor retained (attempt %d/%d)",
                                        sub["task_id"], ev.id, _state,
                                        _stage_fails, MAX_API_STAGE_RETRIES,
                                    )
                                    await _to_thread_process_service(
                                        self._kanban_rewind, sub, d["cursor"],
                                        d.get("old_cursor", 0), board_slug,
                                    )
                                break
                            api_stage_counts.pop((sub["task_id"], ev.id), None)
                            # Non-RGZ/OWUI mission (T2 parity, спека §8.2.2/
                            # §8.3): the exact-owner api_server staging above
                            # is the PRIMARY lane.  When the OWUI owner is
                            # server-bound to a Telegram chat in ALLOWED/HOME,
                            # ALSO evaluate a Telegram delivery of the SAME
                            # text to the bound chat through the SAME RGZ
                            # classifier.  Routine/status-flip events are
                            # suppressed with a journal entry; the lane is
                            # fail-closed and never delivers to a foreign
                            # recipient (verify_recipient ИНВ1).
                            try:
                                _tg_lane = _tg_owui_dual_lane(
                                    sub, ev, kind, msg, task, _tg_allowed,
                                    task_is_root=d.get("task_is_root"),
                                    mission_live=bool(d.get("mission_live")),
                                )
                            except Exception as _lane_exc:
                                logger.warning(
                                    "kanban notifier: OWUI→TG lane for %s/%s failed: %s",
                                    sub["task_id"], ev.id, _lane_exc,
                                )
                                _tg_lane = None
                            if _tg_lane is not None:
                                _tg_decision, _tg_bound_chat = _tg_lane
                                if not _tg_decision.suppressed:
                                    _tg_adapter = self._authorization_adapter(
                                        _Platform.TELEGRAM, sub_profile or None,
                                    )
                                    if _tg_adapter is not None:
                                        try:
                                            _tg_send_res = await _tg_adapter.send(
                                                _tg_bound_chat, msg, metadata={},
                                            )
                                            if getattr(_tg_send_res, "success", True) is False:
                                                # Complementary best-effort lane:
                                                # a TG transport failure must NOT
                                                # rewind the OWUI cursor (the
                                                # primary lane already staged and
                                                # ACKed) — log for observability.
                                                logger.warning(
                                                    "kanban notifier: OWUI→TG lane send to telegram:%s reported failure: %s",
                                                    _tg_bound_chat,
                                                    getattr(_tg_send_res, "error", None) or "unknown error",
                                                )
                                            else:
                                                logger.debug(
                                                    "kanban notifier: OWUI→TG lane delivered %s event for %s to telegram:%s on board %s",
                                                    kind, sub["task_id"],
                                                    _tg_bound_chat, board_slug,
                                                )
                                            if kind == "completed":
                                                # Native artifact upload for the
                                                # TG lane, mirroring the telegram
                                                # branch below. Best-effort: a
                                                # failure must not fail the send.
                                                try:
                                                    await self._deliver_kanban_artifacts(
                                                        adapter=_tg_adapter,
                                                        chat_id=_tg_bound_chat,
                                                        metadata={},
                                                        event_payload=getattr(ev, "payload", None),
                                                        task=task,
                                                    )
                                                except Exception as _art_exc:
                                                    logger.debug(
                                                        "kanban notifier: OWUI→TG artifact delivery for %s failed: %s",
                                                        sub["task_id"], _art_exc,
                                                    )
                                        except Exception as _tg_exc:
                                            logger.warning(
                                                "kanban notifier: OWUI→TG send failed for %s to telegram:%s: %s",
                                                sub["task_id"], _tg_bound_chat, _tg_exc,
                                            )
                            continue
                        # Adapters with no push channel (the API server —
                        # ``supports_async_delivery = False``) can NEVER
                        # satisfy a text-send: ``send()`` always reports
                        # SendResult(success=False) by design (see
                        # ApiServerAdapter.send()). Treating that as a
                        # delivery failure would rewind/drop the subscription
                        # forever and — because the wake dispatch below lives
                        # in this loop's ``else`` clause — would also make the
                        # wake-on-completion path (the actual fix for the
                        # api_server wrong-session bug) unreachable. So for
                        # non-push adapters, skip the doomed send attempt
                        # entirely: there is nothing to text-notify, the
                        # creator is woken via the self-post below instead.
                        from gateway.wake import adapter_supports_push

                        if not adapter_supports_push(adapter) and wake_agent:
                            logger.debug(
                                "kanban notifier: adapter %s has no push "
                                "channel; skipping text ping for %s, relying "
                                "on wake self-post instead",
                                platform_str, sub["task_id"],
                            )
                            # Do NOT reset the failure counter here: on this
                            # path the wake self-post below IS the delivery,
                            # so the counter is resolved (reset or bumped) by
                            # the self-post outcome, not by skipping the send.
                            continue
                        if not send_passive:
                            # Wake-only subscriptions intentionally skip the
                            # visible platform message. The retained wake path
                            # below is the sole delivery — the failure counter
                            # is resolved (reset or bumped) by the wake
                            # outcome there, not by skipping the send here.
                            continue
                        try:
                            _send_res = await adapter.send(
                                sub["chat_id"], msg, metadata=metadata,
                            )
                            # A SendResult(success=False) without an exception
                            # (returned by push-capable adapters on a genuine
                            # transient failure) must count as a FAILED
                            # delivery — otherwise the cursor advances and the
                            # event is permanently lost. Adapters returning
                            # None (or anything non-SendResult shaped) keep
                            # the legacy "no exception == delivered" contract.
                            if getattr(_send_res, "success", True) is False:
                                raise RuntimeError(
                                    "adapter send() reported failure: "
                                    f"{getattr(_send_res, 'error', None) or 'unknown error'}"
                                )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if kind == "completed":
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await _to_thread_process_service(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All text pings delivered (or intentionally skipped
                        # for non-push adapters, whose delivery is the wake
                        # self-post below). Whether the cursor may advance now
                        # depends on the adapter class:
                        #
                        # * push-capable: the text send WAS the delivery, so
                        #   advance immediately (pre-existing behavior); the
                        #   wake injection below stays best-effort.
                        # * non-API adapters without a push channel retain the
                        #   historical best-effort wake path. API server events
                        #   are handled above: only outbox readback ACK advances
                        #   their claimed cursor, never an HTTP self-post.
                        task_terminal = task and task.status == "archived"
                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
                        _wake_kinds = (
                            {
                                ev.kind
                                for ev in d["events"]
                                if ev.kind in _WAKE_KINDS
                                and getattr(ev, "id", None) not in _tg_suppressed_event_ids
                            }
                            if wake_agent
                            else set()
                        )
                        from gateway.wake import adapter_supports_push as _adapter_push_ok

                        _is_push_adapter = _adapter_push_ok(adapter)
                        _session_key = ""
                        _synth = ""
                        if _wake_kinds:
                            if _is_push_adapter:
                                _session_key = getattr(task, "session_id", None) or ""
                            else:
                                # Non-push (api_server) wakes go to the
                                # subscription's delivery destination —
                                # sub["chat_id"] IS the raw session id the
                                # subscriber registered with. task.session_id
                                # is worker/creator provenance and may point
                                # at a WORKER session for child tasks with
                                # inherited subscriptions; falling back to it
                                # only when chat_id is empty (legacy rows).
                                _session_key = (
                                    sub["chat_id"]
                                    or getattr(task, "session_id", None)
                                    or ""
                                )
                        if _wake_kinds:
                            _title = (task.title if task else sub["task_id"])[:120]
                            _assignee = task.assignee if task else ""
                            _parts = []
                            if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
                            if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
                            if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
                            if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
                            if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
                            _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
                            _synth = t(
                                "gateway.kanban.wake.message",
                                task_id=sub["task_id"],
                                status=_status,
                                title=_title,
                                assignee=_assignee,
                                board=board_slug,
                            )
                            # Graph-safe wake turn (#70752): carry the worker's
                            # completion handoff into the synthetic turn and
                            # label it as an automatic notification so the woken
                            # creator inspects the board instead of
                            # re-decomposing work that already exists.
                            if wake_handoff:
                                _synth += "\n" + t(
                                    "gateway.kanban.wake.handoff",
                                    summary=wake_handoff,
                                )
                            _synth += "\n\n" + t(
                                "gateway.kanban.wake.guidance"
                            )

                        if not _is_push_adapter and _wake_kinds and _session_key:
                            # Wake self-post IS the delivery on this path —
                            # it must succeed BEFORE the cursor advances.
                            from gateway.wake import deliver_wake

                            try:
                                await deliver_wake(
                                    adapter,
                                    text=_synth,
                                    session_id=_session_key,
                                )
                                logger.info(
                                    "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                    sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                                )
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake self-post failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.error(
                                        "kanban notifier: delivery pending for %s on %s after %d wake failures; "
                                        "subscription and cursor retained for retry/operator inspection",
                                        sub["task_id"], platform_str, fails,
                                    )
                                # Never drop the sole delivery path for a
                                # blocked model-choice question. The event is
                                # durable in task_events; rewind the claim on
                                # every failure, including after the threshold.
                                await _to_thread_process_service(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                                continue

                        async def _push_wake() -> None:
                            """Wake the creator session behind a push adapter.

                            Shared by the wake-only (pre-advance, delivery)
                            and notify+wake (post-advance, best-effort)
                            branches below; raises on failure so the caller
                            decides whether to rewind or merely log.
                            """
                            from gateway.session import SessionSource
                            from gateway.wake import deliver_wake
                            # Rebuild the creator's real session scope from
                            # the chat_type persisted on the subscription
                            # row (#56580). build_session_key() keys DMs
                            # (":dm:<chat_id>") on a wholly different shape
                            # from group/thread, so the old hardcoded
                            # "group" mis-routed DM/thread creators into a
                            # fresh session. Legacy rows written before the
                            # column existed may still carry chat_type in
                            # delivery_metadata (#60600 rows) — fall back
                            # to that, then to "group" (the historical
                            # default that suits the dashboard/group flows).
                            # handle_message() get_or_create_session's the
                            # target, so a mismatch only ever degrades to a
                            # fresh session, never an exception.
                            _chat_type = str(sub.get("chat_type") or "").strip()
                            if not _chat_type:
                                _delivery_meta = sub.get("delivery_metadata")
                                if isinstance(_delivery_meta, dict):
                                    _chat_type = str(
                                        _delivery_meta.get("chat_type") or ""
                                    ).strip()
                            _chat_type = _chat_type or "group"
                            _source = SessionSource(
                                platform=plat,
                                chat_id=sub["chat_id"],
                                chat_type=_chat_type,
                                thread_id=sub.get("thread_id") or None,
                                user_id=sub.get("user_id"),
                                user_id_alt=sub.get("user_id_alt"),
                                profile=sub_profile or None,
                                scope_id=_wake_scope_id(adapter, sub),
                            )
                            # deliver_wake preserves the synthetic
                            # MessageEvent/handle_message path for
                            # push-capable adapters (the non-push /
                            # self-post branch is handled BEFORE the
                            # cursor advance above).
                            await deliver_wake(
                                adapter,
                                text=_synth,
                                session_id=_session_key,
                                source=_source,
                            )
                            logger.info(
                                "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                            )

                        if _is_push_adapter and not send_passive and _wake_kinds:
                            # Wake-only (delivery_mode='wake') push sub: the
                            # text ping was intentionally skipped above, so
                            # the wake IS the sole delivery. It must succeed
                            # BEFORE the cursor advances — advancing first
                            # would let a failed wake (previously swallowed
                            # by the best-effort except below) permanently
                            # lose the event. Mirrors the non-push
                            # (api_server) self-post ordering above.
                            try:
                                await _push_wake()
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake-only delivery failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, fails,
                                    )
                                    await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                    sub_fail_counts.pop(sub_key, None)
                                else:
                                    # Rewind the pre-send claim so the next
                                    # tick retries the wake — the event is
                                    # NOT lost.
                                    await _to_thread_process_service(
                                        self._kanban_rewind,
                                        sub,
                                        d["cursor"],
                                        d.get("old_cursor", 0),
                                        board_slug,
                                    )
                                continue

                        # Delivery complete (text ping for push adapters, wake
                        # self-post for non-push, wake injection for wake-only
                        # push subs): advance cursor. The cursor is the dedup
                        # mechanism — it prevents re-delivery of the same
                        # event on subsequent ticks.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        if not _is_push_adapter:
                            # Nothing left to deliver on this path (the wake,
                            # if any, already succeeded above).
                            sub_fail_counts.pop(sub_key, None)
                        # Unsubscribe only on archive. Completion (``done``)
                        # remains reversible: controllers reopen completed
                        # work for review corrections and continuation. The
                        # retained cursor prevents replay while preserving the
                        # original delivery and wake ownership for that cycle.
                        if _is_push_adapter and send_passive and _wake_kinds:
                            # notify+wake: the text ping above was the
                            # delivery and the cursor has advanced; the wake
                            # injection stays best-effort.
                            try:
                                await _push_wake()
                            except Exception as _wk_err:
                                # Best-effort: the notification itself already
                                # delivered and the cursor has advanced, so a
                                # broken wake path must not wedge the tick — but
                                # log at WARNING with a traceback rather than
                                # DEBUG so a persistently-failing wake is visible
                                # in normal logs instead of silently no-op'ing.
                                logger.warning(
                                    "kanban notifier: wakeup injection failed for %s: %s",
                                    sub["task_id"], _wk_err, exc_info=True,
                                )
                        if task_terminal:
                            await _to_thread_process_service(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    async def _kanban_branch_summary_watcher(self, interval: float = 45.0) -> None:
        """Deliver ONE short branching result (итог) when a whole branch closes.

        For every subscribed branch root (a task with a ``kanban_notify_subs``
        row whose ancestors have no subscription) the tick expands the
        transitive ``task_links`` subtree and, when the WHOLE branch is
        terminal, sends a single short message to the root's originating
        channel(s): ``✅ Задача выполнена`` / ``❌ Задача не выполнена (нужен
        фикс): <id:title>``.

        Once-only state is a ``branch_summary`` event row on the ROOT task
        (``task_events``) whose payload holds the итог + a per-channel
        ``delivered`` ack list — entirely outside the subscription cursor
        machinery, so this stage never moves or conflicts with per-sub
        cursors (see kanban_db.branch_state / mark_branch_summary /
        ack_branch_summary_channel).

        A node reopening after the итог (done→running and so on) makes the
        branch non-terminal again → the marker is cleared and the next full
        close re-sends a fresh итог.

        Default semantics: the итог is ADDITIVE — the existing per-task
        terminal pings keep flowing. With ``kanban.branch_summary_replace:
        true`` the notifier suppresses the individual descendant pings and the
        итог is the single channel message.

        Cadence is a separate ~45s supervised tick (deliberately NOT inside
        the 5s notifier loop: the per-subscription cursor machinery stays
        untouched, and the subtree scan must not add latency to event
        delivery).
        """
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban branch summary: kanban_db not importable; disabled")
            return

        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Per-(root, channel) delivery-failure counter. A permanently broken
        # chat must not spin forever inside a 45s tick; after MAX consecutive
        # failures the channel is acked anyway and dropped (an ERROR is
        # logged). Resets on a successful delivery.
        MAX_FAILURES = 12
        fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_branch_summary_fail_counts", {}
        )
        self._kanban_branch_summary_fail_counts = fail_counts

        # Let the gateway finish wiring adapters + the notifier take its first
        # pass before branch scanning starts.
        await asyncio.sleep(10)

        while self._running:
            try:
                def _collect():
                    plans: list[dict] = []
                    reactivated: list[tuple[str, str, int]] = []
                    include_unowned = self._owns_kanban_dispatcher_lock()
                    notifier_profiles = {notifier_profile}
                    notifier_profiles.update(
                        str(profile).strip()
                        for profile in getattr(self, "_profile_adapters", {})
                        if str(profile).strip()
                    )
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    for _profile_adapter_map in getattr(self, "_profile_adapters", {}).values():
                        active_platforms.update(
                            getattr(platform, "value", str(platform)).lower()
                            for platform in _profile_adapter_map.keys()
                        )
                    if not active_platforms:
                        return plans, reactivated
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = (
                                str(Path(db_path).expanduser().resolve())
                                if db_path else str(_kb.kanban_db_path(slug).resolve())
                            )
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            continue
                        seen_db_paths.add(resolved_db_path)
                        try:
                            if _kb.count_notify_subs(
                                board=slug,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            ) == 0:
                                continue
                        except Exception:
                            pass
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug(
                                "kanban branch summary: cannot open board %s: %s",
                                slug, exc,
                            )
                            continue
                        try:
                            subs = _kb.list_notify_subs(
                                conn,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            )
                            if not subs:
                                continue
                            # Only branch ROOTS may own a branch итог: the
                            # topmost subscribed task of a subtree (see
                            # kanban_db.subscribed_branch_roots).
                            roots = _kb.subscribed_branch_roots(conn)
                            branch_roots = set(roots)
                            subs_by_task: dict[str, list[dict]] = {}
                            for sub in subs:
                                subs_by_task.setdefault(
                                    str(sub.get("task_id")), []
                                ).append(sub)
                            for root_id in roots:
                                # A synthetic readback / bookkeeping root
                                # (do_not_deliver=1) is never a branch-summary
                                # delivery subject: the итог must not be
                                # planned, marked, or delivered for it.
                                _root_task = _kb.get_task(conn, root_id)
                                if _task_do_not_deliver(_root_task):
                                    continue
                                # A channel subscribed to ANY task of the
                                # branch must receive the итог — in
                                # ``kanban.branch_summary_replace`` mode its
                                # individual per-node pings are suppressed, so
                                # the whole-branch message is its only
                                # notification. Collect the subs of the root's
                                # subtree whose channel the root OWNS, deduped
                                # by (platform, chat_id, thread_id) so a
                                # channel subscribed to both a node and its
                                # ancestor still receives exactly ONE message
                                # — and a channel a shared descendant merely
                                # inherited from a SIBLING root is claimed by
                                # that sibling, not by this root (F8 wave:
                                # otherwise an old already-delivered root is
                                # re-planned/re-delivered on a wave of
                                # descendant closes).
                                node_ids = {root_id} | _kb.get_descendant_ids(
                                    conn, root_id
                                )
                                root_subs = _branch_summary_owned_subs(
                                    conn, _kb, root_id, node_ids, subs_by_task,
                                    branch_roots,
                                )
                                if not root_subs:
                                    continue
                                # Only roots with ≥1 locally-deliverable sub
                                # are scanned; another gateway owns the rest.
                                if not any(
                                    (str(sub.get("platform") or "").lower() in active_platforms)
                                    for sub in root_subs
                                ):
                                    continue
                                # Wave root-event filter (t_09f66666): before
                                # planning a root, skip an ALREADY-DELIVERED,
                                # still-closed root whose subtree has NO close
                                # event in the current wave.  This is the F8/F7
                                # incident shape — a batch INSERT closing 12
                                # unrelated F7 readback cards re-planned +
                                # re-delivered OLD done roots (t_0f0e834d,
                                # t_0db5f307): they carry a delivered marker,
                                # no pending channels, a still-terminal branch,
                                # and no subtree event since their marker → the
                                # plan must be skipped here, before
                                # ``branch_state``.
                                marker = _kb.has_branch_summary(conn, root_id)
                                if _branch_summary_skip_untouched_delivered_root(
                                    conn, _kb, root_id, root_subs, marker,
                                    node_ids,
                                ):
                                    continue
                                try:
                                    plan = self._plan_branch_summary(
                                        conn, root_id, root_subs, slug,
                                    )
                                    if plan is None:
                                        continue
                                    if plan.get("reactivated"):
                                        reactivated.append(
                                            (root_id, slug, plan.get("cleared", 0))
                                        )
                                        continue
                                    plans.append(plan)
                                except Exception as exc:
                                    logger.warning(
                                        "kanban branch summary: plan for root %s on board %s failed: %s",
                                        root_id, slug, exc,
                                    )
                                    continue
                        finally:
                            conn.close()
                    return plans, reactivated

                plans, reactivated = await asyncio.to_thread(_collect)
                for root_id, slug, cleared in reactivated:
                    logger.info(
                        "kanban branch summary: branch %s on board %s re-opened "
                        "(cleared %d summary marker(s)); a fresh итог will be "
                        "sent on the next full close",
                        root_id, slug, cleared,
                    )
                for plan in plans:
                    root_id = plan["root_id"]
                    board_slug = plan["board"]
                    for sub in plan["pending"]:
                        sub_key = (
                            root_id, (sub.get("platform") or "").lower(),
                            sub.get("chat_id"), sub.get("thread_id") or "",
                        )
                        try:
                            ok = await self._deliver_branch_summary(
                                plan, sub, notifier_profile,
                            )
                        except Exception as exc:
                            logger.warning(
                                "kanban branch summary: delivery failed for %s on "
                                "%s/%s: %s",
                                root_id, sub.get("platform"), sub.get("chat_id"), exc,
                            )
                            ok = False
                        if ok:
                            fail_counts.pop(sub_key, None)
                        else:
                            n = fail_counts.get(sub_key, 0) + 1
                            fail_counts[sub_key] = n
                            if n >= MAX_FAILURES:
                                # Permanent failure: drop the channel by
                                # acking it so the marker completes and the
                                # tick stops hammering it. The individual
                                # (non-summary) notifier is unaffected.
                                await _to_thread_process_service(
                                    self._ack_branch_summary,
                                    board_slug, root_id,
                                    (sub.get("platform") or "").lower(),
                                    sub.get("chat_id"),
                                    sub.get("thread_id") or "",
                                )
                                logger.error(
                                    "kanban branch summary: dropping channel "
                                    "%s/%s for root %s after %d consecutive "
                                    "delivery failures (итог not delivered there)",
                                    sub.get("platform"), sub.get("chat_id"),
                                    root_id, n,
                                )
                                fail_counts.pop(sub_key, None)
            except Exception as exc:
                logger.warning("kanban branch summary tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _plan_branch_summary(
        self,
        conn, root_id: str, root_subs: list[dict], board_slug: str,
    ) -> Optional[dict]:
        """Decide + claim the branch итог for ONE root (runs in a thread).

        Returns ``None`` when the root has nothing to do this tick; a dict
        describing a fresh/resumed delivery otherwise. Marker mutations are
        atomic per-root (``mark_branch_summary`` opens its own write txn), so
        concurrent gateways cannot double-deliver a fresh итог.
        """
        from hermes_cli import kanban_db as _kb
        # Synthetic readback/bookkeeping roots (do_not_deliver=1) are never a
        # branch-summary subject; a direct caller (e.g. a test) must get the
        # same skip the watcher loop applies before planning.
        if _task_do_not_deliver(_kb.get_task(conn, root_id)):
            return None
        st = _kb.branch_state(conn, root_id)
        if st is None:
            # Branch is open. If a marker from a previous close lingers, clear
            # it so the next full close re-sends the итог.
            if _kb.has_branch_summary(conn, root_id) is not None:
                cleared = _kb.clear_branch_summary(conn, root_id)
                return {"reactivated": True, "cleared": cleared, "root_id": root_id}
            return None
        outcome = st["outcome"]
        msg = _kb.branch_summary_message(outcome, st["failed"])
        # F6-style terminal gate for the TG mirror of the branch итог: the
        # итог is TERMINAL only when the whole mission/root tree above the
        # branch root is closed too (no open/running ancestor remains).  A
        # mid-tree close ([split N/M], one-off daughter card, sub-branch of a
        # still-running mission) is intermediate → delivered to OWUI but NOT
        # mirrored to Telegram (computed once per root, consumed by
        # _deliver_branch_summary).
        terminal = _branch_summary_terminal(conn, _kb, root_id)
        marker = _kb.has_branch_summary(conn, root_id)
        if marker is not None:
            # The marker is the branch's ONCE-ONLY claim: every channel that
            # already received the итог is acked inside it. A fingerprint
            # difference without the branch passing through open MUST NOT
            # clear+re-mark — that would replay the OLD итог to every acked
            # channel. Closing a NEW descendant (batch synthetic-card wave,
            # direct block->archived mutation, added child of an already
            # closed root) changes the close-state fingerprint but the branch
            # never re-opened, so the previously delivered итог stands.
            #
            # Only channels not yet acked (partial delivery, or a newly added
            # subscription) need the итог this tick. A reopen (branch open)
            # above already clears the marker via the reactivation path, so a
            # full re-close still re-sends a fresh итог.
            #
            # F8 (РГЗ/tghyg): already-delivered old roots must NOT be
            # re-marked/re-delivered on a wave of descendant closes.
            #
            # Duplicate-marker guard (t_46cdd83e): an existing marker whose
            # fingerprint EQUALS the branch's current close-state fingerprint
            # proves the branch has not changed since it was delivered. If
            # nothing is pending (every channel already acked) there is
            # nothing to do this tick — return None immediately instead of
            # ever planning/enqueueing a duplicate delivery.  This is the
            # in-function short-circuit: even when a wave reaches planning
            # (e.g. a synthetic-card close that task-0's loop-level filter
            # cannot prove "untouched" without event rows), a same-fingerprint
            # already-acked root is never re-marked or re-delivered.
            if (
                marker.get("fingerprint") is not None
                and marker.get("fingerprint") == st["fingerprint"]
                and not _kb.branch_summary_pending_subs(marker, root_subs)
            ):
                return None
            pending = _kb.branch_summary_pending_subs(marker, root_subs)
            if not pending:
                return None
            return {
                "root_id": root_id,
                "board": board_slug,
                "subs": root_subs,
                "pending": pending,
                "outcome": outcome,
                "msg": msg,
                "marker_id": marker.get("_event_id"),
                "fingerprint": st["fingerprint"],
                "terminal": terminal,
            }
        payload = {
            "fingerprint": st["fingerprint"],
            "outcome": outcome,
            "failed": [{"id": tid, "title": title} for tid, title in st["failed"]],
            "message": msg,
            "delivered": [],
            "root": root_id,
            "board": board_slug,
        }
        marker_id = _kb.mark_branch_summary(conn, root_id, payload)
        return {
            "root_id": root_id,
            "board": board_slug,
            "subs": root_subs,
            "pending": list(root_subs),
            "outcome": outcome,
            "msg": msg,
            "marker_id": marker_id,
            "fingerprint": st["fingerprint"],
            "terminal": terminal,
        }

    async def _deliver_branch_summary(
        self, plan: dict, sub: dict, notifier_profile: str,
    ) -> bool:
        """Send one branch итог to one subscription channel.

        Returns True when delivered (or the channel is intentionally dropped),
        False when the channel should stay pending for a later tick. On
        success the channel is acked inside the marker payload so repeated
        ticks never re-send it.
        """
        from hermes_cli import kanban_db as _kb
        from gateway.config import Platform as _Platform
        board = plan["board"]
        root_id = plan["root_id"]
        msg = plan["msg"]
        platform_str = (sub.get("platform") or "").lower()
        sub_profile = sub.get("notifier_profile") or ""
        try:
            plat = _Platform(platform_str)
        except ValueError:
            # Unknown platform — nothing can ever deliver; ack to stop retries.
            await _to_thread_process_service(
                self._ack_branch_summary, board, root_id,
                platform_str, sub.get("chat_id"), sub.get("thread_id") or "",
            )
            return True
        adapter = self._authorization_adapter(plat, sub_profile or None)
        if adapter is None:
            # Channel adapter not connected this tick — keep the channel
            # pending; the message arrives when the adapter is back.
            logger.debug(
                "kanban branch summary: adapter %s disconnected for %s; deferred",
                platform_str, root_id,
            )
            return False
        delivery_metadata = sub.get("delivery_metadata")
        metadata: dict[str, Any] = (
            dict(delivery_metadata) if isinstance(delivery_metadata, dict) else {}
        )
        if sub.get("thread_id") and not metadata.get("thread_id"):
            metadata["thread_id"] = sub["thread_id"]
        if platform_str == "api_server":
            from hermes_constants import get_hermes_home
            from gateway.kanban_origin_delivery import stage_terminal
            _registry = os.environ.get("HERMES_OPENWEBUI_DB_PATH")
            _state = "unbound"
            marker_id = plan.get("marker_id") or 0
            # In branch_summary_replace mode the individual completed pings
            # are suppressed — the branch итог must then carry the report
            # inline itself, or the api_server channel never sees it.
            _msg = _owui_branch_result_text(plan, msg)
            if (
                _registry
                and (not sub_profile or sub_profile == notifier_profile)
                and int(marker_id or 0) > 0
            ):
                _state, _delivery_id = await asyncio.to_thread(
                    stage_terminal,
                    get_hermes_home() / "delivery-outbox.sqlite",
                    _registry, sub, sub_profile or notifier_profile,
                    board or "default", int(marker_id), _msg,
                )
            if _state != "acknowledged":
                logger.warning(
                    "kanban branch summary: api_server delivery for %s state=%s; "
                    "channel kept pending",
                    root_id, _state,
                )
                return False
            # F7 do-implementation (t_ebc0a351): complement the OWUI api_server
            # PRIMARY lane with the OWUI→Telegram mirror.  An OWUI mission root
            # is subscribed ONLY via api_server (0 native telegram rows), so the
            # terminal branch итог previously never reached Telegram even when
            # the owner was server-bound via openwebui_telegram_users.  The
            # mirror sends the SAME terminal итог to the owner's bound TG chat
            # (F6 gate: only a fully-closed mission/root tree; intermediate
            # splits are journaled, never sent).  Best-effort: a TG send /
            # mirror failure is logged but NEVER blocks or re-queues the OWUI
            # channel (the api_server primary lane is acked below regardless),
            # and the mirror never touches the marker's delivered list, so a
            # later delivery of the OWUI итог can't be suppressed by it.
            try:
                _tg_lane = _branch_summary_owui_tg_lane(
                    sub, plan, msg, _tg_delivery_policy_allowed_users(),
                )
            except Exception as _lane_exc:
                logger.warning(
                    "kanban branch summary: OWUI→TG mirror for %s/%s failed: %s",
                    platform_str, root_id, _lane_exc,
                )
                _tg_lane = None
            if _tg_lane is not None:
                _tg_decision, _tg_bound_chat = _tg_lane
                if not _tg_decision.suppressed:
                    _tg_adapter = self._authorization_adapter(
                        _Platform.TELEGRAM, sub_profile or None,
                    )
                    if _tg_adapter is not None:
                        try:
                            _tg_send_res = await _tg_adapter.send(
                                _tg_bound_chat, msg, metadata={},
                            )
                            if getattr(_tg_send_res, "success", True) is False:
                                raise RuntimeError(
                                    "branch summary OWUI→TG mirror send() "
                                    f"reported failure: "
                                    f"{getattr(_tg_send_res, 'error', None) or 'unknown error'}"
                                )
                            logger.info(
                                "kanban branch summary: OWUI→TG mirror terminal "
                                "итог for %s delivered to telegram:%s",
                                root_id, _tg_bound_chat,
                            )
                        except Exception as _tg_send_exc:
                            logger.warning(
                                "kanban branch summary: OWUI→TG mirror send "
                                "failed for %s to telegram:%s: %s",
                                root_id, _tg_bound_chat, _tg_send_exc,
                            )
                    else:
                        logger.debug(
                            "kanban branch summary: OWUI→TG mirror telegram "
                            "adapter disconnected for %s; deferred",
                            root_id,
                        )
            await _to_thread_process_service(
                self._ack_branch_summary, board, root_id,
                platform_str, sub.get("chat_id"), sub.get("thread_id") or "",
            )
            return True
        # F6-style terminal gate (t_f285cb14): the branch итог reaches the
        # Telegram mirror ONLY when it is the TERMINAL close of the whole
        # mission/root tree (plan["terminal"]).  Intermediate closes —
        # [split N/M], one-off daughter cards, any sub-branch with a still-
        # open ancestor mission — are suppressed for telegram while OWUI
        # api_server (above) is unaffected.  The channel is acked so the
        # marker completes and the tick stops retrying; only the terminal
        # root close ever mirrors to TG.
        if platform_str == "telegram" and not plan.get("terminal", True):
            _suppr = _suppress_decision(
                "routine status close",
                detail=(
                    "intermediate branch summary — non-terminal mission tree "
                    "suppressed in TG mirror"
                ),
            )
            try:
                from hermes_cli.delivery_policy import log_suppression

                log_suppression(
                    app="kanban",
                    who=str(sub.get("user_id") or sub.get("chat_id") or ""),
                    target=f"telegram:{sub.get('chat_id')}",
                    decision=_suppr,
                    snippet=(msg or "")[:200],
                    extra={
                        "task_id": root_id,
                        "event_kind": "branch_summary",
                        "event_id": plan.get("marker_id") or 0,
                        "owui_to_tg": True,
                    },
                )
            except Exception as exc:  # journal must never break delivery
                logger.debug(
                    "delivery-policy: branch summary suppression journal failed: %s",
                    exc,
                )
            logger.info(
                "delivery-policy: intermediate branch summary suppressed for "
                "%s to telegram:%s (non-terminal mission tree)",
                root_id, sub.get("chat_id"),
            )
            await _to_thread_process_service(
                self._ack_branch_summary, board, root_id,
                platform_str, sub.get("chat_id"), sub.get("thread_id") or "",
            )
            return True
        try:
            _send_res = await adapter.send(
                sub["chat_id"], msg, metadata=metadata,
            )
            if getattr(_send_res, "success", True) is False:
                raise RuntimeError(
                    f"adapter send() reported failure: "
                    f"{getattr(_send_res, 'error', None) or 'unknown error'}"
                )
        except Exception as exc:
            logger.warning(
                "kanban branch summary: send failed for %s on %s/%s: %s",
                root_id, platform_str, sub.get("chat_id"), exc,
            )
            return False
        await _to_thread_process_service(
            self._ack_branch_summary, board, root_id,
            platform_str, sub.get("chat_id"), sub.get("thread_id") or "",
        )
        return True

    def _ack_branch_summary(
        self,
        board: Optional[str],
        root_id: str,
        platform: str,
        chat_id: str,
        thread_id: str,
    ) -> None:
        """Sync helper: mark one channel as delivered in the marker payload."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.ack_branch_summary_channel(
                conn, root_id,
                platform=platform, chat_id=chat_id,
                thread_id=thread_id or None,
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen_paths: set[str] = set()
        # Content-addressed dedup (F9, t_85a095e0): workers frequently ship
        # the same deliverable twice — once from the scratch/attachments copy
        # and once from the durable copy (the sno-economics R4 case:
        # `kanban/attachments/<task>/ref_midels.md` + `profiles/.../durable/
        # .../ref_midels.md`, same file bytes).  Sending the same file to a
        # channel twice is noise: dedupe by (filename, sha256) keeping the
        # first occurrence, exactly as `_persist_scratch_completion_artifacts`
        # stages one copy.  NOTE: only identical NAME+CONTENT collapses;
        # distinct files that happen to share a name or bytes are unaffected.
        _seen_fingerprints: dict[tuple[str, str], str] = {}

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen_paths:
                return
            if not os.path.isfile(expanded):
                return
            seen_paths.add(expanded)
            try:
                name = os.path.basename(expanded)
                with open(expanded, "rb") as _fh:
                    digest = hashlib.sha256()
                    while chunk := _fh.read(1024 * 1024):
                        digest.update(chunk)
                    content_hash = digest.hexdigest()
                key = (name, content_hash)
                if key in _seen_fingerprints:
                    logger.info(
                        "kanban notifier: skipping duplicate artifact %s (same "
                        "content as already-uploaded %s)",
                        expanded, _seen_fingerprints[key],
                    )
                    return
                _seen_fingerprints[key] = expanded
            except OSError as _exc:
                logger.debug(
                    "kanban notifier: could not hash artifact %s (%s); shipping it",
                    expanded, _exc,
                )
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        # Single-dispatcher backstop. dispatch_in_gateway defaults to true, so a
        # new profile gateway (or a same-profile restart race) can silently
        # start a second dispatcher; concurrent dispatchers double reclaim
        # frequency, double claim-attempt events, and — with
        # wal_autocheckpoint=0 — concurrent manual WAL checkpoints can corrupt
        # index pages. The lock lives at the machine-global kanban root
        # (shared across profiles by design), so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info(
                "kanban dispatcher: another gateway already holds the dispatcher "
                "lock (%s); this gateway will NOT dispatch.", _lock_path,
            )
            return
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning(
                "kanban dispatcher: advisory lock unavailable at %s; proceeding "
                "on config control alone.", _lock_path,
            )

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info("kanban dispatcher: max_in_progress=%s", max_in_progress)
        # When the operator never set kanban.max_in_progress, fall back to a
        # memory-derived default (OOF-30/OOF-77): unbounded fan-out on small
        # hosted VMs has repeatedly swap-thrashed the whole machine. Explicit
        # config always wins; None stays None on hosts where total memory
        # can't be read (macOS/Windows dev machines).
        effective_max_in_progress = _kb.resolve_max_in_progress(max_in_progress)
        if max_in_progress is None and effective_max_in_progress is not None:
            logger.info(
                "kanban dispatcher: kanban.max_in_progress unset; using "
                "memory-derived default max_in_progress=%d "
                "(set kanban.max_in_progress in config.yaml to override)",
                effective_max_in_progress,
            )
        max_in_progress = effective_max_in_progress

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # kanban.reconcile_orphans (config.yaml, default true): each tick,
        # requeue 'running' cards whose claim bookkeeping is broken (no
        # valid claim, dead/gone worker) — the zombie-card reconciliation
        # pass. Set false to keep orphans frozen for manual forensics.
        reconcile_orphans = bool(kanban_cfg.get("reconcile_orphans", True))

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                    reconcile_orphans=reconcile_orphans,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            # Only probe the review column when autonomous review dispatch is
            # actually on. With ``review_dispatch`` off (the default — no
            # sdlc-review agent), a task parked in 'review' is "correctly idle"
            # waiting for a human, not a stuck dispatcher; probing it here would
            # fire a false "dispatcher stuck" warning that never clears. Shares
            # the exact gate the dispatcher uses so the two can't drift.
            _review_probe = _kb.review_dispatch_enabled()
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _review_probe and _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        #
        # The flag is re-read from config EVERY tick (#49638) rather than
        # captured once at boot. Auto-decompose is a safety toggle: a user who
        # sees it fan out and run tasks they didn't intend reaches for
        # ``kanban.auto_decompose: false`` to STOP it — and that must take
        # effect on the next tick, not require a gateway restart. (Reported:
        # auto-decompose created and launched destructive tasks while the user
        # was still typing the task description, and the flag "couldn't be
        # disabled" because the gateway had captured its boot-time value.)
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """Re-resolve (enabled, per_tick) from current config each tick."""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await _to_thread_process_service(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Global emergency stop (`hermes pause`): skip auto-decompose
                # and dispatch entirely — no new workers while paused. Running
                # workers finish naturally; zombie reaping above still runs.
                if not _kanban_dispatch_allowed():
                    ready_pending = False
                    bad_ticks = 0
                else:
                    # Re-read the auto-decompose toggle live each tick so a user
                    # flipping kanban.auto_decompose=false to STOP runaway fan-out
                    # takes effect on the next tick, not on gateway restart (#49638).
                    _ad_enabled, _ad_per_tick = _read_auto_decompose_settings()
                    if _ad_enabled:
                        await _to_thread_process_service(_auto_decompose_tick, _ad_per_tick)
                    results = await _to_thread_process_service(_tick_once)
                    any_spawned = False
                    reclaimed_this_tick = 0
                    for slug, res in (results or []):
                        if res is not None and getattr(res, "spawned", None):
                            any_spawned = True
                            # Quiet by default — only log when something actually
                            # happened, so an idle gateway stays silent.
                            logger.info(
                                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                                "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                                slug,
                                len(res.spawned),
                                res.reclaimed,
                                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                                res.promoted,
                                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                            )
                        if res is not None:
                            _gen_reclaimed = getattr(res, "generation_reclaimed", None) or []
                            if getattr(_gen_reclaimed, "__len__", None):
                                reclaimed_this_tick += len(_gen_reclaimed)
                    # FAST_RESTART_STATS observer: count dead-generation cards
                    # claimed back to ready on this boot's dispatch pass
                    # (no-op on non-fast boots — no stamp → no emit).
                    if reclaimed_this_tick:
                        try:
                            from gateway.fast_restart_stats import record_cards_reclaimed

                            record_cards_reclaimed(reclaimed_this_tick)
                        except Exception:
                            pass
                    # Health telemetry (aggregate across boards)
                    ready_pending = await _to_thread_process_service(_ready_nonempty)
                    if ready_pending and not any_spawned:
                        bad_ticks += 1
                    else:
                        bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                self._release_kanban_dispatcher_lock()
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0

        self._release_kanban_dispatcher_lock()
