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
import re
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
#: Cap for requested/attached root artifacts (F1).  Telegram's hard platform
#: limit is 50 MB; the contract fixes a comfortable 25 MB ceiling so an
#: oversized artifact is skipped with a warning instead of failing the whole
#: delivery.  Shared by the notifier artifact path and the branch-summary
#: routing in gateway/delivery_engine.py.
_OWUI_TG_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024


def _owui_artifact_text(artifacts: Any) -> str:
    """Read the first local text artifact (the board report) for inline delivery.

    api_server has no attachment channel, so the report file's text must ride
    inside the persisted chat message itself. Returns ``""`` when nothing
    readable is found (missing file, binary/oversized, non-text extension).
    """
    if artifacts is None:
        return ""
    from hermes_cli.delivery_policy import sanitize_delivery_text
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
                # Reasoning hygiene: the report artifact rides inline in the
                # OWUI chat message — strip reasoning_content / condense any
                # leftover long interim blocks the report may carry.
                text = sanitize_delivery_text(text)
                if text:
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
    from hermes_cli.delivery_policy import sanitize_delivery_text, _strip_mission_slang
    # Человекочитаемый заголовок без служебных кодов (канон R7): «✅ Готово — {title}».
    clean_title = _strip_mission_slang(title or "")
    clean_title = re.sub(r"^\s*(?:РГЗ|РГЗ:|rgz)[:\s-]*", "", clean_title, flags=re.IGNORECASE).strip()
    parts = [f"✅ Готово — {clean_title or task_id}"]
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        # Словарная чистка служебных токенов (R6) + reasoning-чистка
        parts.append(sanitize_delivery_text(summary.strip()))
    art = _owui_artifact_text(payload.get("artifacts"))
    if art:
        parts.append(art)
    full = "\n\n".join(parts)
    return full if len(full) > len(fallback) else fallback


#: Human-action block kinds mirrored to the initiator's bound TG chat as a
#: «требует решения» message (F2).  block_loop_detected routes the card to
#: triage and is already loud on both lanes; it is included so the TRIAGE ping
#: and the «требует решения» ping stay consistent for the owner.
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
    try:
        stage_children = kb.get_descendant_ids(conn, root_id)
    except Exception:
        stage_children = set()
    if stage_children:
        # F10 (chamber-graves-ex / sno-economics): a branch root that closes
        # while its OWN descendants are still open/running is NOT the mission
        # итог — an acceptance (приёмка) card, a rework card or any still
        # attached step is downstream work of the same mission.  The pre-F10
        # gate looked ONLY upward (ancestors), so the «готов» итог of an
        # upstream stage (report assembly) fired the moment that stage closed,
        # while the independent review card was already spawned and running —
        # the owner received the итог of a mission whose приёмка had not
        # finished.  branch_state(root) being terminal therefore means "this
        # subtree closed", not "the mission closed": a close is terminal only
        # when nothing below it is still in flight either.  Status
        # classification reuses ``classify_branch_node_status`` so this gate
        # cannot drift from ``branch_state``'s own notion of "open".
        ordered_children = sorted(stage_children)
        placeholders = ", ".join("?" * len(ordered_children))
        rows = conn.execute(
            "SELECT id, status, block_kind FROM tasks "
            f"WHERE id IN ({placeholders})",
            ordered_children,
        ).fetchall()
        for row in rows:
            raw_status = str(row["status"] or "").strip()
            block_kind = str(row["block_kind"] or "").strip() or None
            cls = kb.classify_branch_node_status(raw_status, block_kind)
            if cls == kb.SA_BRANCH_NODE_OPEN:
                return False
            if cls == kb.SA_BRANCH_NODE_FAIL:
                # Отказ ветки — законный терминальный итог: владелец должен
                # узнать, что приёмка упёрлась в неисправимое.  Глушить его
                # нельзя даже ради «отчёт только по завершении»: отказ и есть
                # завершение, а молчание здесь оставило бы миссию без вердикта.
                continue
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

        # R2 fix (t_702c62f2): the claim set must match the engine's own
        # ENGINE_CLAIM_KINDS (which adds ``spawned``/``claimed`` for
        # live-status).  The legacy TERMINAL_KINDS below filtered those out,
        # so live-status events were never delivered to DeliveryEngine and
        # delivery_live_status stayed empty.
        from gateway.delivery_engine import ENGINE_CLAIM_KINDS as _ENGINE_CLAIM_KINDS

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
                                # Terminal-card ADDRESS rows (t_099801f5) are a
                                # subscription record, not a second channel:
                                # the card's итог already reaches that chat via
                                # the api_server OWUI→TG mirror. Enumerating
                                # them here would send it twice.
                                include_terminal_mirror=False,
                            )
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
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
                                        kinds=_ENGINE_CLAIM_KINDS,
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
                                    # descendant replace-mode skip) — they can
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
                                    # branch_summary_replace semantics are the
                                    # SINGLE behavior (R0(в) 10): tasks that are
                                    # a strict descendant of a subscribed branch
                                    # root have their individual pings
                                    # suppressed — the whole-branch итог
                                    # (delivered by
                                    # _kanban_branch_summary_watcher) is the
                                    # single channel message. computed once per
                                    # subscription.
                                    silent = any(
                                        pid in subscribed_ids
                                        for pid in _kb.parent_ids(conn, sub["task_id"])
                                    )
                                    # P2 (t_ecab4990): does the completing card
                                    # still have NON-terminal descendants
                                    # (children/sub-branches alive)? When yes,
                                    # its ``completed`` is NOT the branch итог
                                    # and the OWUI→TG mirror must stay silent
                                    # (computed here where ``conn`` is live).
                                    has_open_desc = False
                                    _terms_statuses = {"done", "archived"}
                                    _scan = list(_kb.child_ids(conn, sub["task_id"]))
                                    while _scan and not has_open_desc:
                                        _cid = _scan.pop()
                                        _crow = conn.execute(
                                            "SELECT status FROM tasks WHERE id=?",
                                            (_cid,),
                                        ).fetchone()
                                        if not _crow:
                                            continue
                                        if str(_crow["status"] or "").strip() not in _terms_statuses:
                                            has_open_desc = True
                                            break
                                        _scan = list(_kb.child_ids(conn, _cid)) + _scan
                                    # P2 refinement (standalone OWUI root): a
                                    # root whose subtree carries NO subscribed
                                    # descendant is the only subscription in its
                                    # branch — the branch-summary watcher has
                                    # nothing else to collapse, so the notifier
                                    # must stage the root's OWN terminal
                                    # completion to the OWUI outbox itself
                                    # (cursor stays pinned until the OWUI ACKs).
                                    # A root that OWNS subscribed descendants
                                    # delegates the whole-tree итог to the
                                    # branch-summary watcher and the notifier
                                    # stays silent here (replace-mode, R0(в) 10).
                                    has_subscribed_desc = False
                                    if subscribed_ids:
                                        _scan2 = list(
                                            _kb.child_ids(conn, sub["task_id"])
                                        )
                                        while _scan2 and not has_subscribed_desc:
                                            _cid2 = _scan2.pop()
                                            if _cid2 in subscribed_ids:
                                                has_subscribed_desc = True
                                                break
                                            _scan2 = list(
                                                _kb.child_ids(conn, _cid2)
                                            ) + _scan2
                                    logger.debug(
                                        "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s%s",
                                        len(events), sub["task_id"], slug, old_cursor, cursor,
                                        " (silent: descendant replace-mode)" if silent else "",
                                    )
                                    # ОВНЕР-ПРАВИЛО (03.11.2026, t_f6790ef5;
                                    # t_5f76c8d0): «завершено» корня проходит как
                                    # итог только если ревью ветки закрылось
                                    # ПОЛОЖИТЕЛЬНО — причём «положительно» теперь
                                    # означает МАШИНОЧИТАЕМЫЙ вердикт ревьюера,
                                    # а не факт `done` его карточки.
                                    # Единственная точка правды —
                                    # ``_kb.branch_summary_decision`` (обёртка
                                    # над ``branch_review_gate``); значение
                                    # пробрасывается в движок как
                                    # ``review_passed`` и глушит только успешный
                                    # итог. Вердикт ОТКАЗА не глушится: он
                                    # доставляется отдельным событием карточки
                                    # ревьюера (см. путь «отказ приёмки» ниже).
                                    #
                                    # Значение нужно только для api_server-итога
                                    # (движок применяет флаг лишь там), поэтому
                                    # для остальных платформ остаётся True —
                                    # их поведение не меняется, лишних чтений БД
                                    # нет.
                                    review_passed = True
                                    if platform == "api_server":
                                        try:
                                            _rst = _kb.branch_state(
                                                conn, sub["task_id"],
                                            )
                                            if _rst is not None:
                                                _dec = (
                                                    _kb.branch_summary_decision(
                                                        conn, sub["task_id"],
                                                        st=_rst,
                                                    )
                                                )
                                                # ``review_passed`` here guards
                                                # ONLY the success [final], so it
                                                # must be the GATE, not
                                                # ``deliver``: a negative verdict
                                                # is deliverable (deliver=True)
                                                # but must never produce a
                                                # «✅ выполнено» on this lane.
                                                # The refusal wording travels the
                                                # branch-summary lane instead.
                                                review_passed = bool(
                                                    _dec is not None
                                                    and _dec["review_passed"]
                                                )
                                        except Exception as _rp_exc:
                                            # Fail closed: не отдаём «выполнено»,
                                            # если проверку ревью доказать
                                            # нельзя. Итог по-прежнему доставит
                                            # ветка-итог, когда ревью закроется.
                                            review_passed = False
                                            logger.warning(
                                                "kanban notifier: review gate for %s "
                                                "failed (%s); suppressing the "
                                                "per-event OWUI final",
                                                sub["task_id"], _rp_exc,
                                            )
                                    deliveries.append({
                                        "sub": sub,
                                        "old_cursor": old_cursor,
                                        "cursor": cursor,
                                        "events": events,
                                        "task": task,
                                        "board": slug,
                                        "silent": silent,
                                        "do_not_deliver": do_not_deliver,
                                        "task_is_root": task_is_root,
                                        "mission_live": mission_live,
                                        "has_open_descendants": has_open_desc,
                                        "has_subscribed_descendant": has_subscribed_desc,
                                        "review_passed": review_passed,
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
                    if d.get("do_not_deliver"):
                        # Synthetic readback cards are never a delivery
                        # subject at all: no text ping, no wake, no staging.
                        wake_agent = False
                        send_passive = False
                    elif d.get("silent"):
                        # silent (replace-mode descendant): the branch итог is
                        # the single channel MESSAGE, so no per-event text
                        # ping. Human-action kinds (blocked+provider_route,
                        # block_loop_detected, triage, terminal failures) are
                        # STILL allowed to wake — the wake is a synthetic
                        # agent turn, not a channel message, and it is the
                        # only way a descendant's "needs you now" reaches the
                        # creator without spamming their channel. The per-event
                        # predicate inside _wake_kinds decides which events
                        # qualify; routine intermediate closes wake nothing.
                        #
                        # F2 (t_03af6568): the api_server OWUI→TG block mirror
                        # is a complementary F2 channel that must stay live for
                        # silent descendants too (a needs_input/provider_route/
                        # triage block on a child must reach the owner's bound
                        # TG chat).  It is SAFE because the delivery engine's
                        # classifier already gates dispatch: an intermediate
                        # child `completed` is silenced and never reaches the
                        # TG send, while human-action needs-decision blocks do.
                        # Only NON-api_server (native push) silent subs drop
                        # the per-event text-ping entirely here.
                        if platform_str != "api_server":
                            send_passive = False
                    # Worker handoff carried into the synthetic wake turn below
                    # (#70752): without it the woken creator only sees
                    # "Task X completed" and re-decomposes work that already
                    # exists on the board. The engine's [final]/[needs-decision]
                    # wake callback reuses this surface.
                    wake_handoff = ""

                    # -------- R1 delivery engine wiring (single policy) --------
                    # The engine owns classification, render, dual-lane dispatch
                    # and wake decisions for every claimed event. The notifier
                    # keeps only cursor mechanics (claim/rewind/advance) and the
                    # per-sub push-failure budget.
                    from gateway.delivery_engine import (
                        DeliveryEngine as _DeliveryEngine,
                        ENGINE_CLAIM_KINDS as _ENGINE_CLAIM_KINDS,
                    )
                    from gateway.config import Platform as _Platform
                    # Instance-level hooks (NOT class-dict lambdas): a lambda
                    # declared in the class dict binds as an instance method,
                    # so ``resolve_tg_adapter(sub)`` receives two positional
                    # args (self+sub) and raises TypeError on every live-status
                    # tick.  Assigning to the instance keeps the 1-arg shape
                    # and lets ``ensure_live_status`` resolve the TG adapter
                    # (and thus reuse the saved message_id after a restart).
                    # Empty class: all hooks are assigned per-instance below
                    # (never in the class dict, which would bind lambdas as
                    # instance methods and break the 1-arg call shape).
                    _engine_hooks = type("EngineHooks", (), {})()
                    _engine_hooks.resolve_tg_adapter = (
                        lambda _sub: self._authorization_adapter(
                            _Platform.TELEGRAM,
                            (_sub or {}).get("notifier_profile") or sub_profile or None,
                        )
                    )
                    engine = _DeliveryEngine(hooks=_engine_hooks)
                    engine._tg_adapter = self._authorization_adapter(
                        _Platform.TELEGRAM, sub_profile or None,
                    )
                    try:
                        from hermes_cli.delivery_policy import owui_telegram_users
                        engine.owui_to_tg = dict(owui_telegram_users()) or {}
                    except Exception:
                        engine.owui_to_tg = {}
                    ENGINE_CLAIM_KINDS = _ENGINE_CLAIM_KINDS

                    # Push send with the subscription's failure budget.
                    async def _engine_send_tg(tg_adapter, chat_id, text):
                        nonlocal sub_fail_counts
                        if not send_passive:
                            return None
                        try:
                            _res = await tg_adapter.send(
                                chat_id, text, metadata={},
                            )
                        except Exception as _send_exc:
                            logger.warning(
                                "kanban notifier: TG send for %s to telegram:%s failed: %s",
                                sub["task_id"], chat_id, _send_exc,
                            )
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            return None
                        if not getattr(_res, "success", True):
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            if fails >= MAX_SEND_FAILURES:
                                logger.error(
                                    "kanban notifier: dropping subscription %s on telegram:%s after %d consecutive failures",
                                    sub["task_id"], chat_id, fails,
                                )
                                await _to_thread_process_service(
                                    self._kanban_unsub, sub, board_slug,
                                )
                                sub_fail_counts.pop(sub_key, None)
                            return None
                        sub_fail_counts.pop(sub_key, None)
                        return _res

                    async def _engine_edit_tg(tg_adapter, chat_id, msg_id, text):
                        try:
                            return await tg_adapter.edit_message(
                                chat_id, msg_id, text,
                            )
                        except Exception as _edit_exc:
                            logger.debug(
                                "delivery engine: TG edit of %s/%s failed: %s",
                                chat_id, msg_id, _edit_exc,
                            )
                            return None

                    # Hooks for the live-status path (engine.ensure_live_status
                    # falls back to hooks when it is not given explicit
                    # send/edit callables).  Same functions handle_event uses
                    # for the publish lanes, so the first-send message_id is
                    # recorded and later ticks edit it in place.
                    _engine_hooks.edit_tg = _engine_edit_tg
                    _engine_hooks.send_tg = _engine_send_tg

                    def _engine_stage_owui(msg_text: str = ""):
                        # api_server lane: stage the exact event into the
                        # persisted outbox. Only a bound registry may ack.
                        from hermes_constants import get_hermes_home
                        from gateway.kanban_origin_delivery import stage_terminal
                        _registry = os.environ.get("HERMES_OPENWEBUI_DB_PATH")
                        _ev_id = getattr(ev, "id", None)
                        if (
                            not _registry
                            or (sub_profile and sub_profile != notifier_profile)
                            or not _ev_id
                        ):
                            return "unbound", None
                        return stage_terminal(
                            get_hermes_home() / "delivery-outbox.sqlite",
                            _registry, sub, sub_profile or notifier_profile,
                            board_slug or "default", int(_ev_id),
                            msg_text or "✅ Задача выполнена",
                        )

                    def _engine_render_owui(task_id: str, title_text: str, msg_text: str) -> str:
                        """Render the OWUI-lane staging text: title + summary + artifacts."""
                        return _owui_completed_text(
                            task_id, title_text, getattr(ev, "payload", None) or {}, msg_text,
                        )

                    async def _engine_wake(*, text: str = "") -> bool:
                        """Wake the creator on [final]/[needs-decision] (engine rule).

                        Returns True when the wake delivered (or was a no-op that
                        must not rewind, e.g. api_server outbox-persist wake or a
                        non-wake kind), False when a PUSH wake failed.  The
                        engine turns a False return into ``action=rewind`` ONLY
                        for ``delivery_mode='wake'`` push subs (where the wake is
                        the sole delivery); notify+wake keeps best-effort.
                        """
                        # Reuse the graph-safe wake surface: for push adapters
                        # deliver_wake builds the session scope from the sub row.
                        from gateway.wake import adapter_supports_push as _adapter_push_ok2
                        if not _adapter_push_ok2(adapter):
                            return True  # api_server: outbox persist is the wake
                        _WAKE_KINDS = (ev.kind,) if ev.kind in (
                            "completed", "gave_up", "crashed", "timed_out",
                            "blocked", "needs_input", "capability",
                            "block_loop_detected", "review_requested",
                        ) else ()
                        if not _WAKE_KINDS:
                            return True
                        return await _engine_push_wake()
                    # TG delivery-policy gate (D0 F1): allowed users, used by
                    # the wake computation below so it never wakes on a
                    # suppressed event.  Recipient policy itself lives in the
                    # platform authz layer (TELEGRAM_ALLOWED_USERS) — the
                    # legacy per-event gate was absorbed by the delivery
                    # engine.
                    try:
                        _tg_allowed = [
                            p.strip()
                            for p in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
                            if p.strip()
                        ]
                    except Exception:
                        _tg_allowed = []
                    _tg_suppressed_event_ids: set = set()
                    async def _engine_push_wake() -> bool:
                        """Deliver one push wake; return True on success, False on failure."""
                        from gateway.session import SessionSource
                        from gateway.wake import deliver_wake
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
                        _status_ev = ev.kind
                        _status_map = {
                            "completed": "gateway.kanban.wake.completed",
                            "gave_up": "gateway.kanban.wake.gave_up",
                            "crashed": "gateway.kanban.wake.crashed",
                            "timed_out": "gateway.kanban.wake.timed_out",
                        }
                        _status = t(
                            _status_map.get(_status_ev, "gateway.kanban.wake.status_default")
                        )
                        _title = (task.title if task else sub["task_id"])[:120]
                        _assignee = task.assignee if task else ""
                        _synth2 = t(
                            "gateway.kanban.wake.message",
                            task_id=sub["task_id"],
                            status=_status,
                            title=_title,
                            assignee=_assignee,
                            board=board_slug,
                        )
                        if wake_handoff:
                            _synth2 += "\n" + t(
                                "gateway.kanban.wake.handoff",
                                summary=wake_handoff,
                            )
                        _synth2 += "\n\n" + t("gateway.kanban.wake.guidance")
                        _session_key = (
                            sub["chat_id"] or getattr(task, "session_id", None) or ""
                        )
                        try:
                            await deliver_wake(
                                adapter,
                                text=_synth2,
                                session_id=_session_key,
                                source=_source,
                            )
                            logger.info(
                                "kanban notifier: woke agent for %s on %s/%s profile=%s event=%s",
                                sub["task_id"], platform_str, sub["chat_id"],
                                sub_profile or "default", _status_ev,
                            )
                            return True
                        except Exception as _wk_err:
                            logger.warning(
                                "kanban notifier: wake injection failed for %s: %s",
                                sub["task_id"], _wk_err, exc_info=True,
                            )
                            return False
                    for ev in d["events"]:
                        kind = ev.kind
                        # -------- single delivery decision (R1 engine) --------
                        # The engine owns ALL classification/render/dispatch now.
                        # Everything that is not [live-status]/[final]/
                        # [needs-decision]/[aborted] is silenced in ONE place
                        # (gateway/delivery_engine.py). The notifier only keeps
                        # the cursor mechanics (claim/rewind/advance) driven by
                        # the engine's returned action.
                        if kind not in ENGINE_CLAIM_KINDS:
                            # Event is outside the engine's claimed set (e.g.
                            # the legacy non-claimed statuses). Advance so it
                            # never replays.
                            await _to_thread_process_service(
                                self._kanban_advance, sub, d["cursor"], board_slug,
                            )
                            continue
                        # Wake path for non-push api_server: the outbox persist
                        # below IS the delivery/wake. Wake on human-action kinds
                        # for push adapters happens inside the engine callback.
                        _engine_res = await engine.handle_event(
                            sub=sub,
                            ev=ev,
                            task=task,
                            adapter=adapter,
                            sub_profile=sub_profile or "",
                            board=board_slug,
                            cursor=d["cursor"],
                            old_cursor=d.get("old_cursor", 0),
                            task_is_root=bool(d.get("task_is_root")),
                            mission_live=bool(d.get("mission_live")),
                            has_open_descendants=bool(d.get("has_open_descendants")),
                            has_subscribed_descendant=bool(d.get("has_subscribed_descendant")),
                            # t_f6790ef5: гейт положительного ревью больше не
                            # мёртвый — _collect считает его канонической
                            # ``kb.summary_has_passed_review``.
                            review_passed=bool(d.get("review_passed", True)),
                            do_not_deliver=bool(d.get("do_not_deliver")),
                            silent=bool(d.get("silent")),
                            platform_str=platform_str,
                            notifier_profile=sub_profile or "",
                            send_tg=_engine_send_tg,
                            edit_tg=_engine_edit_tg,
                            stage_owui=_engine_stage_owui,
                            render_owui=_engine_render_owui,
                            wake=_engine_wake,
                            deliver_artifacts=self._deliver_kanban_artifacts,
                            task_tg_owner=d.get("task_tg_owner") or "",
                            tg_allowed=_tg_allowed,
                        )
                        if _engine_res.action == "rewind":
                            # Send/stage failed transiently: restore the OLD
                            # cursor (drop the atomic claim) so a later tick
                            # retries.  The engine only returns ``rewind`` for
                            # recoverable transient failures (api_server outbox
                            # ``pending`` awaiting server-owned readback, or a
                            # stage error); it is the R1 re-implementation of
                            # the pre-R1 ``_kanban_rewind`` on non-ACK'd stage.
                            # ``enqueue`` is idempotent (INSERT OR IGNORE), so
                            # re-claiming and re-staging the same event NEVER
                            # duplicates the outbox row.
                            _rewind_ev_id = _engine_res.event_id or getattr(ev, "id", None)
                            if _rewind_ev_id and platform_str == "api_server":
                                # Per-source-event staging retry budget: a
                                # permanently undeliverable event (missing
                                # registry / stuck outbox row) must not rewind
                                # and re-claim the SAME event every 5s tick
                                # forever (incident 2026-10-08/09, 12,689
                                # identical WARNING lines in ~24h).  After
                                # MAX_API_STAGE_RETRIES consecutive
                                # non-acknowledged results, advance past the
                                # event (the row stays in the outbox for
                                # independent drain) and log the drop once.
                                _stage_key = (sub["task_id"], _rewind_ev_id)
                                _stage_fails = api_stage_counts.get(_stage_key, 0) + 1
                                api_stage_counts[_stage_key] = _stage_fails
                                if _stage_fails >= MAX_API_STAGE_RETRIES:
                                    logger.warning(
                                        "kanban notifier: exact api_server event %s/%s "
                                        "stuck at delivery (%s) after %d ticks; dropping "
                                        "event (cursor advances, row stays in outbox)",
                                        sub["task_id"], _rewind_ev_id,
                                        _engine_res.reason, _stage_fails,
                                    )
                                    api_stage_counts.pop(_stage_key, None)
                                    await _to_thread_process_service(
                                        self._kanban_advance, sub, d["cursor"],
                                        board_slug,
                                    )
                                else:
                                    logger.info(
                                        "kanban notifier: %s/%s rewound by delivery "
                                        "engine (%s); cursor retained (attempt %d/%d)",
                                        sub["task_id"], _rewind_ev_id,
                                        _engine_res.reason,
                                        _stage_fails, MAX_API_STAGE_RETRIES,
                                    )
                                    await _to_thread_process_service(
                                        self._kanban_rewind, sub, d["cursor"],
                                        d.get("old_cursor", 0), board_slug,
                                    )
                                break
                            # Non-api_server / non-traceable rewind: restore the
                            # old cursor and retry next tick.
                            if _engine_res.reason == "wake failed (wake-only)":
                                # wake-only (delivery_mode='wake') push sub: the
                                # wake IS the sole delivery, so consecutive
                                # failures share the SAME MAX_SEND_FAILURES
                                # budget as text sends (pre-R1 contract). Below
                                # cap: bump + rewind so the next tick retries.
                                # At cap: drop the subscription permanently.
                                _wk_fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = _wk_fails
                                if _wk_fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, _wk_fails,
                                    )
                                    await _to_thread_process_service(
                                        self._kanban_unsub, sub, board_slug,
                                    )
                                    sub_fail_counts.pop(sub_key, None)
                                    await _to_thread_process_service(
                                        self._kanban_advance, sub, d["cursor"],
                                        board_slug,
                                    )
                                else:
                                    logger.info(
                                        "kanban notifier: %s/%s rewound by delivery "
                                        "engine (%s); wake retry (attempt %d/%d)",
                                        sub["task_id"], kind, _engine_res.reason,
                                        _wk_fails, MAX_SEND_FAILURES,
                                    )
                                    await _to_thread_process_service(
                                        self._kanban_rewind, sub, d["cursor"],
                                        d.get("old_cursor", 0), board_slug,
                                    )
                                break
                            logger.info(
                                "kanban notifier: %s/%s rewound by delivery engine (%s)",
                                sub["task_id"], kind, _engine_res.reason,
                            )
                            await _to_thread_process_service(
                                self._kanban_rewind, sub, d["cursor"],
                                d.get("old_cursor", 0), board_slug,
                            )
                            break
                        # action == "advance" (or "noop") — cursor handled in the
                        # for-else below (advance unless we broke on rewind).
                    else:
                        # No event in this batch needed a rewind — the engine
                        # delivered (or deliberately silenced) every claimed
                        # event, so the subscription cursor may advance. The
                        # engine's dual-lane dispatch (OWUI outbox + TG) is the
                        # single delivery surface; the wake callback it invoked
                        # already handled both push and non-push wake paths.
                        task_terminal = task and task.status == "archived"
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
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

        Single behavior (R0(в) 10): the notifier suppresses the individual
        descendant per-task terminal pings (replace semantics) and the итог
        is the single channel message.

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
        # A2: per-(root, channel) last delivery error string, persisted as a
        # task comment when the channel is dropped so an operator can triage
        # WHY without grepping logs (analysis A §2.3 / §2.5: previously only
        # "dropping channel …" was logged, the reason stayed invisible).
        drop_reasons: dict[tuple, str] = getattr(
            self, "_kanban_branch_summary_drop_reasons", {}
        )
        self._kanban_branch_summary_drop_reasons = drop_reasons

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
                        _tdq_snapshot: set[str] = set()
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug(
                                "kanban branch summary: cannot open board %s: %s",
                                slug, exc,
                            )
                            continue
                        # t_2dc720a3: cards whose terminal transition is still
                        # awaiting delivery on this board. Read ONCE per pass so
                        # the drain below consumes exactly what this tick saw,
                        # and so a queued card can be logged as the reason the
                        # scan ran early.
                        try:
                            _tdq_snapshot = {
                                str(_row.get("task_id"))
                                for _row in _kb.peek_terminal_delivery_queue(conn)
                                if _row.get("task_id")
                            }
                        except Exception:
                            _tdq_snapshot = set()
                        if _tdq_snapshot:
                            logger.info(
                                "kanban branch summary: board %s has %d pending "
                                "terminal-delivery trigger(s) — scanning now (%s)",
                                slug, len(_tdq_snapshot),
                                ", ".join(sorted(_tdq_snapshot))[:200],
                            )
                        try:
                            subs = _kb.list_notify_subs(
                                conn,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                                # Terminal-card ADDRESS rows (t_099801f5) are the
                                # card's итог ADDRESS, not a second channel — the
                                # OWUI→TG mirror already reaches that chat. The
                                # branch lane enumerates channels only.
                                include_terminal_mirror=False,
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
                                # branch must receive the итог — since replace
                                # semantics (R0(в) 10) are the single behavior, a
                                # node's individual per-node pings are
                                # suppressed, so
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
                            # t_2dc720a3: consume the terminal-delivery
                            # triggers SNAPSHOTTED at the start of this board
                            # pass. Every subscribed branch root was examined in
                            # this pass, so each queued card has been
                            # considered: its итог is planned, its marker was
                            # refreshed, or its branch is still open. A later
                            # close re-enqueues (the trigger is written by every
                            # terminal transition), so clearing here cannot lose
                            # a future report — and leaving rows behind would
                            # make the 1-second probe spin on an open branch.
                            if _tdq_snapshot:
                                try:
                                    _cleared = _kb.clear_terminal_delivery(
                                        conn, sorted(_tdq_snapshot),
                                    )
                                    if _cleared:
                                        logger.info(
                                            "kanban branch summary: %d terminal "
                                            "trigger(s) consumed on board %s "
                                            "(%s)",
                                            _cleared, slug,
                                            ", ".join(sorted(_tdq_snapshot))[:200],
                                        )
                                except Exception as _tdq_exc:
                                    logger.debug(
                                        "kanban branch summary: terminal-trigger "
                                        "drain failed on board %s: %s",
                                        slug, _tdq_exc,
                                    )
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
                            last_error = getattr(self, "_kanban_last_branch_error", None)
                            if not ok and last_error:
                                drop_reasons[sub_key] = last_error
                        except Exception as exc:
                            logger.warning(
                                "kanban branch summary: delivery failed for %s on "
                                "%s/%s: %s",
                                root_id, sub.get("platform"), sub.get("chat_id"), exc,
                            )
                            ok = False
                            drop_reasons[sub_key] = str(exc)[:300]
                        if ok:
                            fail_counts.pop(sub_key, None)
                            drop_reasons.pop(sub_key, None)
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
                                await _to_thread_process_service(
                                    self._persist_branch_drop_reason,
                                    board_slug, root_id,
                                    (sub.get("platform") or "").lower(),
                                    sub.get("chat_id"),
                                    sub.get("thread_id") or "",
                                    drop_reasons.pop(sub_key, None)
                                    or "unknown delivery failure",
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
            # Sleep with cancellation checks — but wake IMMEDIATELY when a
            # terminal-delivery trigger lands in any board's queue
            # (t_2dc720a3). The trigger is written inside the status-change
            # transaction, so this probe is what turns "a card just went
            # terminal" into "the итог goes out now" instead of "…within the
            # next 45-second scan". The probe is read-only and cheap
            # (``has_terminal_delivery_trigger`` never opens the DB writable).
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)
                try:
                    if await asyncio.to_thread(self._terminal_trigger_pending):
                        break
                except Exception:
                    pass

    def _terminal_trigger_pending(self) -> bool:
        """True when ANY board has a queued terminal-delivery trigger.

        Read-only and cheap (``has_terminal_delivery_trigger`` opens the board
        DB ``?mode=ro`` and never runs schema init), so the branch watcher can
        poll it every second between its normal ticks and cut the scan short the
        moment a card goes terminal. Never raises.
        """
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            return False
        try:
            boards = _kb.list_boards(include_archived=False)
        except Exception:
            boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
        seen: set[str] = set()
        for board_meta in boards:
            slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
            db_path = board_meta.get("db_path")
            try:
                key = (
                    str(Path(db_path).expanduser().resolve())
                    if db_path else str(_kb.kanban_db_path(slug).resolve())
                )
            except Exception:
                key = f"slug:{slug}"
            if key in seen:
                continue
            seen.add(key)
            try:
                if _kb.has_terminal_delivery_trigger(board=slug):
                    return True
            except Exception:
                continue
        return False

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
        # SINGLE decision surface (t_5f76c8d0): the review gate reads the
        # reviewer's MACHINE-READABLE verdict, not just its card status, and the
        # same payload compiles the channel text. ``review_passed`` alone would
        # answer only «можно ли отдать ✅»; a reviewer that closed `done` with
        # ``revision_required`` must still reach the owner as a VERDICT (owner
        # rule №4: молчание недопустимо), so the decision also says WHICH text.
        decision = _kb.branch_summary_decision(conn, root_id, st=st)
        if decision is None:
            return None
        if not decision["deliver"]:
            # Branch closed `success` but its own review did not pass
            # (reviewer deliberately withheld) — OR the итог is older than the
            # freshness window (t_fc08695e). Nothing may be planned for the
            # success lane; the verdict (if any) comes from the reviewer card.
            if decision.get("kind") == "stale":
                _fr = decision.get("freshness") or {}
                logger.info(
                    "kanban branch summary: root %s on board %s итог expired "
                    "(%.1f h old > %.1f h limit) — not delivered; this is a "
                    "message about an old card, not a mission result",
                    root_id, board_slug,
                    float(_fr.get("age_hours") or 0.0),
                    float(_fr.get("max_age_hours") or 0.0),
                )
                return None
            logger.info(
                "kanban branch summary: root %s on board %s has no passed "
                "review (%s, verdict=%s); success итог withheld",
                root_id, board_slug, decision["review_reason"],
                decision["verdict_class"],
            )
            return None
        msg = decision["msg"] or _kb.branch_summary_message(outcome, st["failed"])
        if decision["kind"] != "success":
            logger.info(
                "kanban branch summary: root %s on board %s delivers a %s "
                "verdict (review_reason=%s, verdict=%s, refused=%s)",
                root_id, board_slug, decision["kind"],
                decision["review_reason"], decision["verdict_class"],
                ",".join(decision["refused_ids"]) or "-",
            )
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
                "kind": decision["kind"],
                "msg": msg,
                "marker_id": marker.get("_event_id"),
                "fingerprint": st["fingerprint"],
                "terminal": terminal,
            }
        payload = {
            "fingerprint": st["fingerprint"],
            "outcome": outcome,
            # The wording kind is delivered in the marker: ``success`` /
            # ``refusal`` / ``failure``. A `success` branch whose review was
            # rejected is recorded as `refusal` while `outcome` keeps the
            # auditable branch_state value (t_5f76c8d0).
            "kind": decision["kind"],
            "review": {
                "passed": decision["review_passed"],
                "reason": decision["review_reason"],
                "verdict_class": decision["verdict_class"],
            },
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
            "kind": decision["kind"],
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
            from gateway.delivery_engine import (
                DeliveryEngine as _BranchEngine,
                owui_root_final_summary,
            )
            _registry = os.environ.get("HERMES_OPENWEBUI_DB_PATH")
            _state = "unbound"
            marker_id = plan.get("marker_id") or 0
            if (
                _registry
                and (not sub_profile or sub_profile == notifier_profile)
                and int(marker_id or 0) > 0
            ):
                # OWUI primary lane: the baked one-liner goes bare when the
                # root carried no report artifact; when the root DID attach a
                # report file, api_server has no attachment channel, so the
                # report content rides inline in the staged message (R1
                # convention) instead of a bare contentless ping.
                from gateway.delivery_engine import owui_root_artifact_paths
                try:
                    _root_artifacts = owui_root_artifact_paths(plan)
                except Exception:
                    _root_artifacts = []
                if _root_artifacts:
                    try:
                        from gateway.delivery_engine import owui_root_final_summary
                        _msg = owui_root_final_summary(plan, msg, inline_report=True)
                    except Exception:
                        _msg = msg
                else:
                    _msg = msg
                _state, _delivery_id = await asyncio.to_thread(
                    stage_terminal,
                    get_hermes_home() / "delivery-outbox.sqlite",
                    _registry, sub, sub_profile or notifier_profile,
                    board or "default", int(marker_id), _msg,
                )
            # OWUI stage result drives the ack gate, but NEVER starves the
            # complementary OWUI→Telegram mirror (dual-lane rule 1/4/5, same
            # decoupling the [needs-decision] path got in t_637df501).  A
            # terminal итог must reach the owner even while the OWUI readback
            # is pending (slow drain) or the OWUI chat is permanently dead
            # (deleted/renamed session).  Before the fix the ``!= acknowledged``
            # early-return ran BEFORE the TG mirror, so a slow drain (13d,
            # 229 "channel kept pending") or a dead session (56 failed rows,
            # 2 after A1) silently lost the итог on BOTH lanes after
            # MAX_FAILURES.
            _owui_state = _state
            if _state != "acknowledged":
                reason_map = {
                    "unbound": (
                        "no verified-origin binding (subscription has no "
                        "user_id or chat missing from registry)"
                    ),
                    "failed": "outbox enqueue failed",
                    "pending": "outbox item pending (write/readback not yet "
                               "persisted to Open WebUI chat)",
                }
                self._kanban_last_branch_error = (
                    f"api_server stage_terminal -> {_state}: "
                    + reason_map.get(_state, "unknown delivery failure")
                )
                if _state != "pending":
                    logger.warning(
                        "kanban branch summary: api_server delivery for %s state=%s; "
                        "falling back to the OWUI->TG mirror lane",
                        root_id, _state,
                    )
                else:
                    logger.warning(
                        "kanban branch summary: api_server delivery for %s state=%s; "
                        "channel kept pending (TG mirror fired independently)",
                        root_id, _state,
                    )

            # F7: the complementary OWUI→Telegram mirror now lives in the
            # delivery engine (handle_branch_final -> _tg_bound_chat_for ->
            # owui_to_tg[owner]).  The engine is the single dual-lane surface,
            # so the mirror is dispatched there, not here.
            _bs_engine_title = (sub.get("notifier_profile") or sub_profile or "")
            _bs_engine = _BranchEngine()
            _bs_engine._tg_adapter = self._authorization_adapter(
                _Platform.TELEGRAM, sub_profile or None,
            )
            try:
                from hermes_cli.delivery_policy import owui_telegram_users
                _bs_engine.owui_to_tg = dict(owui_telegram_users()) or {}
            except Exception:
                _bs_engine.owui_to_tg = {}
            async def _bs_send_tg(_tg_adapter, _chat_id, _text):
                return await _tg_adapter.send(
                    _chat_id, _text, metadata={},
                )
            # Once-only TG mirror guard (dual-lane: OWUI is authoritative).
            # The [needs-decision] path persists this same
            # (task_id, kind, cause_hash) marker (t_637df501); we key the
            # branch итог on ``branch:{root_id}:final:<marker_id>`` so a slow
            # OWUI drain re-invoking this tick never re-pings the owner's
            # bound TG chat.  ``delivery_dedup`` lives in the live kanban DB
            # (ensure_dedup_table/ensure at engine init) — a missing table
            # fails closed to a send (never silently skips the owner итог).
            _tg_dedup_key = (
                f"branch:{root_id}:final:{int(marker_id or 0)}"
                if plan.get("terminal", True)
                else f"branch:{root_id}:intermediate:{int(marker_id or 0)}"
            )
            _dedup_marked: Optional[str] = None
            try:
                from hermes_cli import kanban_db as _kb_dd
                from gateway.delivery_engine import (
                    dedup_mark as _dd_mark,
                    dedup_seen as _dd_seen,
                )
                _dd_conn = _kb_dd.connect(board=board)
                try:
                    if not _dd_seen(_dd_conn, _tg_dedup_key, "branch_final", "mirror"):
                        _dedup_marked = _tg_dedup_key
                finally:
                    _dd_conn.close()
            except Exception:
                # Fail open to the send: a DB/table problem must not lose the
                # owner's terminal итог (and enqueue dedup still guards the
                # OWUI lane).
                _dedup_marked = _tg_dedup_key
            if _dedup_marked is not None:
                _mirror_res = None
                # t_b154af7d: default is "no failure".  Initialised BEFORE the
                # try so the ``except`` path below cannot fall through with an
                # unbound name (NameError would abort the whole delivery loop
                # and leave the owner without the итог).
                _tg_failed = False
                try:
                    _mirror_res = await _bs_engine.handle_branch_final(
                        plan=plan, sub=sub, msg=msg, adapter=adapter,
                        sub_profile=_bs_engine_title,
                        send_tg=_bs_send_tg,
                        deliver_artifacts=self._deliver_kanban_artifacts,
                        terminal=bool(plan.get("terminal", True)),
                        board_override=board,
                    )
                    # t_b154af7d: the once-only dedup marker is written ONLY
                    # after the TG lane ACCEPTED the message.  Before this the
                    # marker was written unconditionally, so one degraded send
                    # (``send_path_degraded`` / flood wait / adapter hiccup)
                    # was remembered as "already mirrored" and the owner's
                    # terminal итог + report were lost for good — the exact
                    # sno-economics-r3 incident (t_f4e43cb2, t_08a192ec).
                    # ``tg_delivered is False`` ⇒ withhold the marker and let
                    # this channel stay pending, so the next tick retries
                    # (bounded by MAX_FAILURES in the caller).
                    _tg_failed = (
                        _mirror_res is not None
                        and getattr(_mirror_res, "tg_delivered", None) is False
                    )
                    if _tg_failed:
                        _err = (
                            getattr(_mirror_res, "tg_send_error", "")
                            or "unknown TG send failure"
                        )
                        self._kanban_last_branch_error = (
                            f"TG mirror send failed: {_err}"
                        )
                        logger.warning(
                            "kanban branch summary: TG mirror for %s/%s NOT "
                            "dedup'd — send failed (%s); channel stays pending "
                            "for retry",
                            platform_str, root_id, _err,
                        )
                    else:
                        if getattr(_mirror_res, "tg_delivered", None) is True:
                            logger.info(
                                "kanban branch summary: OWUI→TG mirror terminal "
                                "итог for %s delivered to telegram:%s "
                                "(marker %s)",
                                root_id,
                                _bs_engine._tg_bound_chat_for(sub, platform_str)
                                or "?",
                                marker_id,
                            )
                        try:
                            from hermes_cli import kanban_db as _kb_dd2
                            from gateway.delivery_engine import dedup_mark as _dd_mark2
                            _dd_conn2 = _kb_dd2.connect(board=board)
                            try:
                                _dd_mark2(_dd_conn2, _tg_dedup_key, "branch_final", "mirror")
                            finally:
                                _dd_conn2.close()
                        except Exception:
                            pass
                except Exception as _bs_engine_exc:
                    logger.warning(
                        "kanban branch summary: engine mirror for %s/%s failed: %s",
                        platform_str, root_id, _bs_engine_exc,
                    )
                    self._kanban_last_branch_error = (
                        f"TG mirror raised: {_bs_engine_exc}"
                    )
                    # t_b154af7d: an engine-level exception leaves the TG lane
                    # in an UNKNOWN state — the send may or may not have been
                    # accepted.  Withholding the once-only marker (and keeping
                    # the channel pending) means at worst a duplicate итог on a
                    # later tick; writing it would mean a permanent loss of the
                    # owner's terminal report.  The retry is bounded by
                    # MAX_FAILURES in the caller.
                    _tg_failed = True
                if _tg_failed:
                    # Keep the OWUI channel pending as well: the owner's
                    # terminal итог has not reached BOTH lanes yet, and the
                    # retry budget is shared (MAX_FAILURES).
                    return False
            else:
                logger.info(
                    "kanban branch summary: TG mirror for %s/%s dedup'd "
                    "(already mirrored for marker %s)",
                    platform_str, root_id, marker_id,
                )
            if plan.get("terminal", True) and (
                sub.get("delivery_mode") in ("notify+wake", "wake")
            ):
                try:
                    await self._wake_branch_summary_sub(
                        plan, sub, sub_profile, adapter, plat,
                    )
                except Exception as _bs_wake_exc:
                    logger.warning(
                        "kanban branch summary: terminal wake for %s on %s/%s "
                        "failed: %s",
                        root_id, platform_str, sub.get("chat_id"), _bs_wake_exc,
                    )
            # OWUI ack gate (decoupled from the TG mirror above):
            # - acknowledged: OWUI row persisted -> ack the channel, done.
            # - pending: OWUI drain not acked yet -> keep the channel pending
            #   (return False) so a later tick sees acknowledged and acks; the
            #   TG mirror already fired once (dedup-marked) so recurring ticks
            #   never double-ping.  Bounded by MAX_FAILURES in the caller.
            # - failed / unbound (dead session): nothing more can ACK on the
            #   OWUI lane; the terminal итог reached the owner via the TG
            #   mirror, so stop retrying and close the channel (advance).
            if _owui_state == "pending":
                return False
            await _to_thread_process_service(
                self._ack_branch_summary, board, root_id,
                platform_str, sub.get("chat_id"), sub.get("thread_id") or "",
            )
            return True
        # Native-TG lane: the branch итог is handled by the delivery engine —
        # the single place that renders [final] (F1 content: summary+artifacts,
        # bare итог never delivered) and gates the TG mirror on terminality
        # (F6: intermediate closes are OWUI-only).  A non-terminal branch keeps
        # the channel pending so a later terminal close still mirrors.
        if platform_str == "telegram":
            from gateway.delivery_engine import DeliveryEngine as _NativeEngine
            async def _native_send_tg(_tg_adapter, _chat_id, _text):
                return await _tg_adapter.send(
                    _chat_id, _text, metadata=metadata,
                )
            _ne_title = (sub.get("notifier_profile") or sub_profile or "")
            _ne = _NativeEngine()
            _ne._tg_adapter = adapter
            _ne_res = None
            try:
                _ne_res = await _ne.handle_branch_final(
                    plan=plan, sub=sub, msg=msg, adapter=adapter,
                    sub_profile=_ne_title,
                    send_tg=_native_send_tg,
                    deliver_artifacts=self._deliver_kanban_artifacts,
                    terminal=bool(plan.get("terminal", True)),
                    board_override=board,
                )
            except Exception as _ne_exc:
                logger.warning(
                    "kanban branch summary: engine native-TG итог for %s/%s "
                    "failed: %s",
                    platform_str, root_id, _ne_exc,
                )
                self._kanban_last_branch_error = f"native TG итог raised: {_ne_exc}"
                _ne_res = None
                _ne_failed = True
            else:
                # t_b154af7d: a FAILED send on the native TG lane must keep
                # the channel pending so the next tick retries, instead of
                # acking the итог as delivered.
                _ne_failed = getattr(_ne_res, "tg_delivered", None) is False
                if _ne_failed:
                    _err = (
                        getattr(_ne_res, "tg_send_error", "")
                        or "unknown TG send failure"
                    )
                    self._kanban_last_branch_error = f"native TG итог send failed: {_err}"
                    logger.warning(
                        "kanban branch summary: native TG итог for %s on "
                        "telegram:%s NOT delivered (%s); channel stays pending",
                        root_id, sub.get("chat_id"), _err,
                    )
            if _ne_failed:
                return False
            if getattr(_ne_res, "tg_delivered", None) is True:
                logger.info(
                    "kanban branch summary: native TG terminal итог for %s "
                    "delivered to telegram:%s",
                    root_id, sub.get("chat_id"),
                )
            if plan.get("terminal", True) and (
                sub.get("delivery_mode") in ("notify+wake", "wake")
            ):
                try:
                    await self._wake_branch_summary_sub(
                        plan, sub, sub_profile, adapter, plat,
                    )
                except Exception as _bs_wake_exc:
                    logger.warning(
                        "kanban branch summary: terminal wake for %s on %s/%s "
                        "failed: %s",
                        root_id, platform_str, sub.get("chat_id"), _bs_wake_exc,
                    )
            await _to_thread_process_service(
                self._ack_branch_summary, board, root_id,
                platform_str, sub.get("chat_id"), sub.get("thread_id") or "",
            )
            return True

    async def _wake_branch_summary_sub(
        self, plan: dict, sub: dict, sub_profile: str,
        adapter: Any, plat: Any,
    ) -> None:
        """Wake the creator session for a delivered terminal branch итог.

        Only the TERMINAL branch итог may wake the originating agent session
        (replace semantics — R0(в) 10).  The intermediate
        per-child events were claimed+advanced silently by the notifier, so
        this single wake is what lets the creator collect the итог + report
        artifacts.  Push-capable adapters (Telegram) receive a synthetic
        ``handle_message`` turn rebuilding the exact session scope from the
        subscription row; non-push (api_server) needs no self-post here — the
        outbox ``persist_pending`` write IS the message landing in the OWUI
        session, and a second HTTP self-post would be a duplicated turn.
        """
        from gateway.wake import adapter_supports_push as _wake_push_ok
        from gateway.wake import deliver_wake
        if not _wake_push_ok(adapter):
            return  # api_server: outbox persist is the delivery/wake
        board = plan.get("board") or ""
        root_id = plan.get("root_id") or sub.get("task_id") or ""
        title = str(plan.get("title") or "")[:120]
        assignee = ""
        try:
            from hermes_cli import kanban_db as _kb_bs
            conn = _kb_bs.connect(board=board)
            try:
                _t = _kb_bs.get_task(conn, root_id)
                if _t is not None:
                    assignee = str(getattr(_t, "assignee", "") or "")
            finally:
                conn.close()
        except Exception:
            assignee = ""
        _synth = t(
            "gateway.kanban.wake.message",
            task_id=root_id,
            status=t("gateway.kanban.wake.completed"),
            title=title,
            assignee=assignee,
            board=board,
        )
        _synth += "\n\n" + t("gateway.kanban.wake.guidance")
        _chat_type = str(sub.get("chat_type") or "").strip()
        if not _chat_type:
            _delivery_meta = sub.get("delivery_metadata")
            if isinstance(_delivery_meta, dict):
                _chat_type = str(
                    _delivery_meta.get("chat_type") or ""
                ).strip()
        _chat_type = _chat_type or "group"
        from gateway.session import SessionSource
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
        await deliver_wake(
            adapter,
            text=_synth,
            session_id=sub.get("chat_id") or "",
            source=_source,
        )
        logger.info(
            "kanban branch summary: woke agent for terminal итог %s on %s/%s profile=%s",
            root_id, sub.get("platform"), sub.get("chat_id"),
            sub_profile or "default",
        )

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

    def _persist_branch_drop_reason(
        self,
        board: Optional[str],
        root_id: str,
        platform: str,
        chat_id: str,
        thread_id: str,
        reason: str,
    ) -> None:
        """Sync helper (A2): persist WHY a channel was dropped onto the root.

        Writes a durable comment on the branch root so the reason survives log
        rotation and is visible to an operator/worker beside the task — not
        just an ERROR line.  Best-effort: a comment failure must never break
        or re-enter the drop path.
        """
        from hermes_cli import kanban_db as _kb
        try:
            conn = _kb.connect(board=board)
            try:
                _kb.add_comment(
                    conn,
                    root_id,
                    "delivery",
                    ("[delivery-drop] канал не доставил итог ветки: "
                     f"{platform}/{chat_id}"
                     + (f"/{thread_id}" if thread_id else "")
                     + f" — причина: {reason or 'unknown delivery failure'}"),
                )
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "kanban branch summary: failed to persist drop reason for "
                "%s/%s: %s",
                platform, chat_id, exc,
            )

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
    ) -> dict:
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

        Returns ``{"sent": [...], "failed": [...], "skipped": [...]}`` with
        absolute paths (t_e04a8f44): the caller journals what the owner
        ACTUALLY received instead of assuming every discovered report landed.
        """
        from pathlib import Path as _Path

        sent: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []

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
                # F-4.4 (K1 red team H-4): a broken/nonexistent artifact path
                # was silently dropped here, so a promised attachment never
                # reached the channel and nobody could see why.  The file is
                # STILL not delivered (behavior unchanged) but the skip is now
                # observable at warn level.
                logger.warning(
                    "kanban notifier: artifact %s does not exist on disk — "
                    "skipped (was silent)",
                    expanded,
                )
                skipped.append(expanded)
                return
            # Size cap (F-4.3, K1 red team H-3): a >25MB artifact is rejected
            # up front with an explicit warning instead of being handed to the
            # adapter, whose channel can't accept it — the old path sent it to
            # send_document and the delivery was silently lost (or failed).
            # Mirrors the root-lane 25MB ceiling (_OWUI_TG_ARTIFACT_MAX_BYTES,
            # applied in delivery_engine.owui_root_artifact_paths).  The text
            # delivery is never dropped: only this one oversized attachment is
            # skipped, observable in the logs.
            try:
                if os.path.getsize(expanded) > _OWUI_TG_ARTIFACT_MAX_BYTES:
                    logger.warning(
                        "kanban notifier: artifact %s exceeds %d-byte (%d MB) "
                        "cap — skipped (text delivery unaffected)",
                        expanded, _OWUI_TG_ARTIFACT_MAX_BYTES,
                        _OWUI_TG_ARTIFACT_MAX_BYTES // (1024 * 1024),
                    )
                    skipped.append(expanded)
                    return
            except OSError:
                pass
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
            return {"sent": sent, "failed": failed, "skipped": skipped}

        from gateway.platforms.base import BasePlatformAdapter
        _discovered = list(candidates)
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        for _dropped in _discovered:
            if _dropped not in candidates:
                # Unsafe/denied path: never delivered, must be visible.
                skipped.append(_dropped)
        if not candidates:
            return {"sent": sent, "failed": failed, "skipped": skipped}

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
                sent.extend(image_paths)
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )
                failed.extend(image_paths)

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
                sent.append(path)
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )
                failed.append(path)

        return {"sent": sent, "failed": failed, "skipped": skipped}

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
