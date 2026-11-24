"""Kanban notifier: claim terminal task events per subscription and deliver them.

``GatewayKanbanWatchersMixin._kanban_notifier_watcher`` owns the loop and
the GC cadence; the per-tick claim (``_notifier_collect``) and the
per-subscription delivery (``_KanbanNotification``) live here.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from functools import partial
from pathlib import Path
import weakref
from typing import Any, Callable, Optional

from agent.i18n import t

from gateway.kanban_watchers_common import _list_boards, _to_thread_process_service, logger
from gateway.owner_delivery_policy import (
    OwnerDeliveryConfig,
    artifact_paths,
    classify_kanban_event,
    filter_artifacts,
    offlist_artifacts,
    record_delivered,
    record_suppressed,
)
from gateway.wake import session_owned_by_profile


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbn():
    from hermes_cli import kanban_db_notify
    return kanban_db_notify


def _pin_first():
    """Machine-flow board resolution: env pins outrank the enumerated slug.

    The slug here came from ``list_boards()``, not from a user — on a box whose
    env pins ``HERMES_KANBAN_DB`` every board must resolve to the pinned file
    or the notifier reads per-slug DBs nobody writes (see
    ``kanban_db.pin_first_board_resolution``)."""
    from hermes_cli import kanban_db
    return kanban_db.pin_first_board_resolution()

# "status" covers dashboard drag-drop and `_set_status_direct()`.
# ``review_requested`` wakes the origin like a block but is not one;
# the task is not archived so later review cycles keep notifying.
# ``commented`` is here so a handoff note written after a card closed still reaches the
# subscribed chat — deliberately absent from ``_WAKE_KINDS``: a comment is information for
# the human, it must never start a worker turn (that is the OPS-16 bug this guards).
TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status", "archived", "unblocked", "block_loop_detected", "review_requested", "changes_requested", "commented")
# Kinds that hand a decision back to the origin, which must take a turn.
# status/archived/unblocked are bookkeeping.
_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked", "review_requested", "changes_requested", "block_loop_detected")


def diagnostic_event(ev) -> bool:
    """Infrastructure attention is distinct from an explicit owner decision."""
    if ev.kind in {"crashed", "timed_out", "gave_up"}:
        return True
    if ev.kind in {"blocked", "block_loop_detected"}:
        return (ev.payload or {}).get("kind") != "needs_input"
    return ev.kind == "status" and (ev.payload or {}).get("status") in {"blocked", "triage"}


# Failure kinds that are only meaningful while the card still needs someone. On a card that
# already reached a terminal status they are a stale echo of an attempt that outlived its own
# handoff — paging the owner about them is a false alarm on closed work.
_STALE_ON_CLOSED_CARD_KINDS = frozenset({"crashed", "timed_out", "gave_up"})
_CLOSED_CARD_STATUSES = frozenset({"done", "archived"})
# Journal kinds that record the card BECOMING terminal. ``tasks.completed_at`` is not enough:
# a card can be archived without ever completing, and that column then stays NULL.
_CLOSURE_EVENT_KINDS = ("completed", "archived")
# Journal kinds that put the card BACK IN PLAY after a terminal transition. A failure that
# follows one of these belongs to a live attempt, not to the finished handoff.
_REOPEN_EVENT_KINDS = (
    "created", "promoted", "unblocked", "claimed", "spawned", "worker_bound",
    "scheduled", "review_requested", "changes_requested", "respawn_guarded",
)


_CLOSED_ANCESTOR_STATUSES = frozenset({"done", "archived"})
# Bound for the recursive ancestor walk: a malformed (cyclic) link graph must not hang a tick.
_ANCESTOR_WALK_LIMIT = 200


def has_open_ancestor(conn: Any, task_id: str) -> bool:
    """Whether any transitive ancestor of ``task_id`` is still open (not done/archived).

    A card finished under an open parent is an intermediate step of a bigger card, so
    its completion is machine flow for the owner, not a deliverable. Cycle-safe and
    never raises: an unreadable graph counts as "open" (the conservative side — better
    to stay quiet in Telegram than to page the owner for a sub-step).
    """
    try:
        seen: set[str] = {task_id}
        frontier = [task_id]
        while frontier:
            current = frontier.pop()
            rows = conn.execute(
                "SELECT p.id, p.status FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                "WHERE l.child_id = ?",
                (current,),
            ).fetchall()
            for row in rows:
                parent_id = row["id"] if hasattr(row, "keys") else row[0]
                status = row["status"] if hasattr(row, "keys") else row[1]
                if parent_id in seen:
                    continue
                if str(status) not in _CLOSED_ANCESTOR_STATUSES:
                    return True
                if len(seen) >= _ANCESTOR_WALK_LIMIT:
                    return True
                seen.add(parent_id)
                frontier.append(parent_id)
    except Exception:  # health: allow BLE001 -- a graph read must never break delivery
        return True
    return False


def _epochs_for(conn: Any, task_id: str, kinds: tuple) -> list[int]:
    """Every journal stamp of ``kinds`` for ``task_id``, ascending. Never raises."""
    placeholders = ",".join("?" * len(kinds))
    try:
        rows = conn.execute(
            f"SELECT created_at FROM task_events WHERE task_id = ? AND kind IN ({placeholders}) "
            "ORDER BY created_at",
            (task_id, *kinds),
        ).fetchall()
    except Exception:  # a journal read must never break delivery
        return []
    out: list[int] = []
    for row in rows:
        try:
            stamp = row[0] if not hasattr(row, "keys") else row["created_at"]
            out.append(int(stamp))
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    return out


def card_closures(conn: Any, task_id: str, task: Any = None) -> list[int]:
    """Every known moment ``task_id`` became terminal, ascending — [] when unknown.

    The board journal is the primary source (``completed``/``archived`` events), because
    ``tasks.completed_at`` is NULL on cards archived without completing — exactly the two
    cards of the 21.11 incident, archived 00:27:18 with ``completed_at IS NULL``.
    """
    stamps = _epochs_for(conn, task_id, _CLOSURE_EVENT_KINDS)
    if stamps:
        return stamps
    # Legacy rows (pre-journal or hand-migrated archives) may still carry completed_at.
    completed_at = getattr(task, "completed_at", None) if task is not None else None
    try:
        return [int(completed_at)] if completed_at else []
    except (TypeError, ValueError):
        return []


def event_is_stale_for_closed_card(ev, task, closure_ts: Optional[int] = None) -> bool:
    """True when ``ev`` reports a failure booked AFTER the card had already gone terminal.

    The worker hands the card off, its conversation turn keeps running for a while, and
    the leftover attempt is then booked as ``timed_out``/``crashed``/``gave_up`` — after
    the card is ``done``. That event describes an attempt nobody can act on, so it must
    not become a ping or a wake in the owner's chat.

    The evidence is the ORDER OF EVENTS, not the card's status at delivery time. A card
    can be closed long before the notifier reaches its events (processing lag, a gateway
    restart, the nightly archive sweep); suppressing on status alone silently swallowed
    five real crashes on 21.11.2026 (``t_48888b47``/``t_d9ceb3ac``: events at 00:17:49…
    00:19:51, card archived 00:27:18 — 569 s later). So:

    * a failure on a card that is not terminal is real — always delivered;
    * a failure WITHOUT a known closure time cannot be justified as stale — delivered;
    * a failure that happened BEFORE the closure is real — always delivered;
    * only a failure booked after the closure is the stale echo of a finished handoff.

    ``closure_ts`` is the last terminal transition at or before this event (see
    :func:`closure_before_event`); None means "no evidence", which never suppresses.
    """
    if ev.kind not in _STALE_ON_CLOSED_CARD_KINDS:
        return False
    if str(getattr(task, "status", "") or "") not in _CLOSED_CARD_STATUSES:
        return False
    if closure_ts is None:
        return False
    try:
        event_ts = int(getattr(ev, "created_at", 0) or 0)
    except (TypeError, ValueError):
        return False
    return event_ts > int(closure_ts)


def closure_before_event(
    conn: Any, task_id: str, event_ts: int, closures: Optional[list[int]] = None,
    task: Any = None,
) -> Optional[int]:
    """The card's last terminal transition at or before ``event_ts`` — None when there is none.

    Only a closure the failure could actually have outlived counts. A *later* archive (the
    nightly sweep, a day-old ``archived`` event) says nothing about an attempt that had
    already finished, and treating it as the closure would resurrect exactly the false
    alarms OPS-16 removed (``t_6018df99``: ``completed`` 02:05:10, ``timed_out`` 02:07:04,
    armed a day later).

    A failure that follows an event putting the card back in play belongs to a live attempt
    and is delivered: the card was NOT closed at that moment, whatever an older stamp says.
    """
    stamps = closures if closures is not None else card_closures(conn, task_id, task)
    eligible = [ts for ts in stamps if ts <= int(event_ts)]
    if not eligible:
        return None
    closure = max(eligible)
    reopened = [ts for ts in _epochs_for(conn, task_id, _REOPEN_EVENT_KINDS)
                if closure < ts <= int(event_ts)]
    if reopened:
        return None
    return closure


# Consecutive send failures (adapter raised OR reported SendResult(success=False))
# before a sub is dropped as a dead chat. 12 ≈ 60s at the 5s cadence: a transient
# API outage must not permanently unsubscribe a live review-gate channel.
# Subscriptions are removed only when the task reaches the irreversible archived status. ``done`` is
# reversible in review/controller flows, so removing its subscription would silence a later reopen. We used
# to also unsub on any terminal event kind (gave_up / crashed / timed_out / blocked), but that silently
# dropped the user out of the loop whenever the dispatcher respawned the task: a worker that crashes, gets
# reclaimed, runs again, and crashes a second time would only notify on the first crash because the
# subscription was deleted after the first event. Same shape as the reblock-after-unblock cycle that PR
# #22941 fixed for `blocked`. Keeping the subscription alive until the task is archived lets the cursor
# (advanced atomically by claim_unseen_events_for_sub) handle dedup, and any retry-loop event reaches the
# user. Per-subscription send-failure counter. Adapter.send raising means the chat is dead (deleted, bot
# kicked, etc.) — after N consecutive send failures the sub is dropped so we don't spin against a dead chat
# every 5 seconds forever. A genuinely dead chat still drops, just ~60s later — a fine trade for an
# unattended gate where a false drop means silent work pileup.
MAX_SEND_FAILURES = 12

_LOCAL_PATH_RE = re.compile(r"(?<![\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\s,;]+|" r"[A-Za-z]:\\[^\s,;]+)")


def _safe_review_reason(value: Any, limit: int = 160) -> str:
    """Return a mobile-friendly review reason safe for external delivery."""
    from agent.redact import redact_sensitive_text

    reason = redact_sensitive_text("" if value is None else str(value), force=True, redact_url_credentials=True)
    reason = " ".join(_LOCAL_PATH_RE.sub("[local path]", reason).split())
    if len(reason) > limit:
        reason = reason[: limit - 1].rstrip() + "…"
    return reason


def _wake_scope_id(adapter: Any, sub: dict) -> Optional[str]:
    """Return the tenant scope (Slack workspace) a subscription's wake keys to.

    ``build_session_key()`` includes ``scope_id`` on multi-tenant platforms,
    so the wake must carry the same scope as inbound messages. Persisted
    ``delivery_metadata`` wins (it records the creating scope); the adapter's
    live chat → scope map only covers rows without metadata. ``None`` means
    unscoped, matching an unscoped platform's key.
    """
    delivery_meta = sub.get("delivery_metadata")
    if isinstance(delivery_meta, dict):
        for key in ("scope_id", "guild_id", "slack_team_id", "team_id"):
            value = delivery_meta.get(key)
            if value:
                return str(value)
    resolver = getattr(adapter, "scope_id_for_chat", None)
    if not callable(resolver):
        return None
    try:
        resolved = resolver(str(sub.get("chat_id") or ""))
    except Exception as exc:
        # An adapter-side lookup failure yields no scope, never an error.
        logger.debug("kanban notifier: scope lookup failed for chat %s: %s", sub.get("chat_id"), exc, exc_info=True)
        return None
    return str(resolved) if resolved else None


_ANCHORLESS_WARNED: set[tuple] = set()


def _warn_anchorless_thread_sub_once(sub: dict, platform: str) -> None:
    """A thread-shaped subscription without ``parent_chat_id`` cannot match a channel-level
    ``profile_routes`` entry, so the fail-closed route gate skips it on every tick. Say so ONCE per
    row at WARNING — a subscription that can never deliver was invisible below DEBUG (#110919)."""
    metadata = sub.get("delivery_metadata") or {}
    thread_like = bool(sub.get("thread_id")) or (sub.get("chat_type") or metadata.get("chat_type")) in {
        "thread", "forum", "forum_post", "forum-post", "topic"}
    if not thread_like or metadata.get("parent_chat_id"):
        return
    key = (sub.get("task_id"), platform, sub.get("chat_id"), sub.get("thread_id") or "")
    if key in _ANCHORLESS_WARNED:
        return
    _ANCHORLESS_WARNED.add(key)
    logger.warning(
        "kanban notifier: subscription for %s on %s thread %s has no parent_chat_id anchor and matched no "
        "profile route; it will not be delivered. Re-subscribe with `hermes kanban notify-subscribe ... "
        "--parent-chat-id <channel id> [--guild-id <guild id>]`.",
        sub.get("task_id"), platform, sub.get("chat_id"),
    )


_UNROUTABLE_WARNED: set[tuple] = set()


def _warn_unroutable_sub_once(sub: dict, platform: Any, message: str, *extra_args: Any) -> None:
    """A routed subscription the credential gate fail-closes is a permanent dead-end: delivery
    rewinds every tick with only a DEBUG line. Say so ONCE per row at WARNING, mirroring
    ``_warn_anchorless_thread_sub_once`` (#115460)."""
    key = (sub.get("task_id"), platform, sub.get("chat_id"), sub.get("thread_id") or "")
    if key in _UNROUTABLE_WARNED:
        return
    _UNROUTABLE_WARNED.add(key)
    logger.warning(message, sub.get("task_id"), getattr(platform, "value", platform),
                   sub.get("chat_id"), *extra_args)


def _platform_names(mapping: Any) -> set[str]:
    """Lower-cased platform names of an adapters mapping (Platform enums or strings)."""
    return {getattr(platform, "value", str(platform)).lower() for platform in mapping}


def _adapter_for_subscription(runner: Any, platform: Any, sub: dict, owner_profile: Optional[str]) -> Any:
    """Resolve a durable route without turning a missing secondary bot into primary authority."""
    adapter = runner._authorization_adapter(platform, owner_profile)
    config = getattr(runner, "config", None)
    if not getattr(config, "multiplex_profiles", False):
        return adapter
    primary = runner.adapters.get(platform)
    if adapter is not None and adapter is not primary:
        return adapter
    profile = owner_profile or getattr(runner, "_kanban_notifier_profile", None)
    primary_profile = getattr(runner, "_primary_profile_name", None) or runner._active_profile_name()
    profile = profile or primary_profile
    # A profile holding its OWN adapter for this platform is an independent credential boundary —
    # ``_authorization_adapter`` already answered for it, so the primary never stands in. Adapters
    # on OTHER platforms do not gate this one: the primary bot is the only credential serving the
    # pinned chat, for inbound turns and for these notifications alike (#115460).
    own_adapters = (getattr(runner, "_profile_adapters", {}) or {}).get(profile) or {}
    if getattr(platform, "value", str(platform)).lower() in _platform_names(own_adapters):
        return None
    metadata = sub.get("delivery_metadata") or {}
    guild = metadata.get("scope_id") or metadata.get("guild_id")
    parent = metadata.get("parent_chat_id")
    chat, thread = sub.get("chat_id"), sub.get("thread_id") or None
    user_id = sub.get("user_id") or None
    thread_like = bool(thread) or (sub.get("chat_type") or metadata.get("chat_type")) in {
        "thread", "forum", "forum_post", "forum-post", "topic",
    }
    # Preserve canonical route order, including equal-specificity ties. An older
    # row missing an anchor must not skip a potentially winning route. Reuse the
    # route's matcher (including platform-specific identity aliases), not a second
    # hand-maintained equality implementation.
    for route in getattr(config, "profile_routes", None) or []:
        if route.matches(platform.value, guild_id=guild, chat_id=chat,
                         thread_id=thread, parent_chat_id=parent, user_id=user_id):
            if route.profile != profile:
                _warn_unroutable_sub_once(
                    sub, platform,
                    "kanban notifier: subscription for %s on %s chat %s is stamped with profile %s but a "
                    "profile_routes entry pins that chat to profile %s; it will not be delivered. "
                    "Re-subscribe with `hermes kanban notify-subscribe ... --notifier-profile %s`.",
                    profile, route.profile, route.profile)
                return None
            from gateway.run import _multiplex_profile_homes
            served = {name for name, _home in _multiplex_profile_homes(config)}
            return primary if profile in served else None
        if route.matches(platform.value, guild_id=guild or route.guild_id, chat_id=chat,
                         thread_id=thread, parent_chat_id=parent or (route.chat_id if thread_like else None),
                         user_id=user_id or route.user_id):
            return None
    # A stateless (api_server) subscription carries a RAW session id, not a routable chat, so no
    # profile_routes entry can anchor it — and a platform-wide api_server route would deny the
    # default profile's own api_server destinations. The shared listener mirrors /p/<profile>/ for
    # every served profile, so the owner's own session store is the proof: authorize exactly the
    # session that lives in the served profile's state.db, never the platform. The default profile
    # keeps the historical fallthrough below (no store read).
    if profile != primary_profile and getattr(platform, "value", platform) == "api_server" \
            and session_owned_by_profile(config, profile, chat):
        return primary
    return primary if profile == primary_profile else None


def _policy_artifact_payload(event_payload: Any, extensions) -> Optional[dict]:
    """The payload handed to the shared delivery path under the owner policy.

    Every artifact the card names rides out: a producer's ``.py`` script, log or JSON archive is a
    result of the mission, not a lesser file, so the owner's *deliverable* extension list no longer
    narrows this payload. It still annotates the delivery journal (:func:`_journal_offlist`), so a
    future tightening has the evidence instead of a silent disappearance.

    ``None`` is returned only for a payload that carries NO artifact at all — the caller then skips
    artifact delivery (the text ping already went) without pretending a file was sent.
    """
    raw = (event_payload or {}).get("artifacts") if isinstance(event_payload, dict) else None
    kept = artifact_paths(raw if isinstance(raw, (list, tuple)) else ())
    return {"artifacts": kept} if kept else None


def _journal_offlist(task_id: str, platform: str, chat_id: str, event_payload: Any,
                     extensions) -> None:
    """Record, per artifact the owner's list would have hidden, that it rode out anyway.

    The DELIVERY journal, not the suppression one — the file IS delivered — so the row names the
    extension gate as the would-be reason. Without it a later "why did this .py never arrive?" has
    no trail, and the "how much did we hide" figure stays honest.
    """
    raw = (event_payload or {}).get("artifacts") if isinstance(event_payload, dict) else None
    for path in offlist_artifacts(raw if isinstance(raw, (list, tuple)) else (), extensions):
        record_delivered(
            task_id=task_id,
            event_kind="completed",
            platform=platform,
            chat_id=chat_id,
            reason=f"delivered_despite_extension_list:{Path(path).suffix.lower() or '<none>'}",
        )


def _owner_delivery_config() -> OwnerDeliveryConfig:
    """``kanban.owner_delivery_policy`` from the active profile's effective config.

    Read once per tick (not at import) so flipping ``enabled`` is a live rollback: the
    next tick delivers the old way. An unreadable config keeps the shipped defaults.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    except Exception as exc:  # health: allow BLE001 -- config read is optional; defaults apply
        logger.debug("kanban notifier: cannot read owner delivery policy (%s); using defaults", exc)
        kanban_cfg = {}
    return OwnerDeliveryConfig.from_config(kanban_cfg)


# --- Collection (runs in a worker thread) ---


class _Collector:
    """One tick's claim state: which profiles/platforms this gateway serves and the GC gate."""

    def __init__(self, runner: Any, kb: Any, *, notifier_profile: Optional[str], gc_due: bool, gc_retention_days: int) -> None:
        self.runner = runner
        self.kb = kb
        self.notifier_profile = notifier_profile
        self.gc_due = gc_due
        self.gc_retention_days = gc_retention_days
        self.deliveries: list[dict] = []
        # Owner delivery policy (default: Telegram gets decisions and final deliverables only).
        # Read per tick and matched against each subscription's platform, so desktop/local,
        # api_server and cron keep the pre-policy behaviour byte for byte.
        self.owner_policy = _owner_delivery_config()
        self.include_unowned = runner._owns_kanban_dispatcher_lock()
        self.profile_adapters = getattr(runner, "_profile_adapters", {})
        self.notifier_profiles = {notifier_profile}
        self.notifier_profiles.update(str(p).strip() for p in self.profile_adapters if str(p).strip())
        config = getattr(runner, "config", None)
        if getattr(config, "multiplex_profiles", False):
            self.notifier_profiles.update(
                route.profile for route in config.profile_routes
                if route.enabled and route.platform in _platform_names(runner.adapters)
            )
        # Include every platform any secondary profile has live. This is only a
        # coarse pre-filter; exact destination authorization runs before claim
        # and again at delivery, rewinding if the route or adapter changed.
        self.active_platforms = _platform_names(runner.adapters).union(
            *(_platform_names(m) for m in self.profile_adapters.values()))

    def collect(self) -> list[dict]:
        if not self.active_platforms:
            logger.debug("kanban notifier: no connected adapters; skipping tick")
            return self.deliveries
        # Poll each resolved DB path once: several slugs can map to one DB when
        # HERMES_KANBAN_DB pins the board path. The whole tick resolves pin-first:
        # on a dispatcher-pinned box each enumerated slug must map to the pinned
        # DB, never to that slug's own (empty) physical file.
        kb = self.kb
        with _pin_first():
            seen_db_paths: set[str] = set()
            for board_meta in _list_boards(kb):
                slug = board_meta.get("slug") or kb.DEFAULT_BOARD
                db_path = board_meta.get("db_path")
                try:
                    resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(kb.kanban_db_path(slug).resolve())
                except Exception:
                    resolved_db_path = f"slug:{slug}"
                if resolved_db_path in seen_db_paths:
                    logger.debug("kanban notifier: skipping duplicate board slug %s for DB %s", slug, resolved_db_path)
                    continue
                seen_db_paths.add(resolved_db_path)
                self.collect_board(slug)
        return self.deliveries

    def _board_has_subs(self, slug: str) -> bool:
        """Cheap read-only probe before the writable connect() (schema init, WAL
        sidecars, checkpoints); a probe failure falls back to the writable open."""
        try:
            count = _kbn().count_notify_subs(
                board=slug, notifier_profiles=self.notifier_profiles, include_unowned=self.include_unowned)
        except Exception as exc:
            logger.debug("kanban notifier: read-only subscription probe failed "
                         "for board %s (%s); falling back to writable open", slug, exc)
            return True
        if count == 0:
            logger.debug("kanban notifier: board %s has no subscriptions owned by %s; skipping open",
                         slug, sorted(self.notifier_profiles))
        return count != 0

    def _gc_stale_subs(self, conn: Any, slug: str) -> None:
        """Best-effort stale-sub sweep: a failed sweep never blocks delivery; the next hourly gate retries."""
        try:
            _purged = _kbn().purge_stale_done_notify_subs(conn, max_age_days=self.gc_retention_days)
            if _purged:
                logger.info("kanban notifier: purged %d stale done/blocked-task subscription(s) on board %s (retention %dd)",
                            _purged, slug, self.gc_retention_days)
        except Exception as _gc_exc:
            logger.debug("kanban notifier: stale-sub GC failed for board %s: %s", slug, _gc_exc)

    def _claim_for_sub(self, conn: Any, slug: str, sub: dict) -> Optional[dict]:
        """Claim one subscription's unseen events; None when skipped or nothing new."""
        owner_profile = sub.get("notifier_profile") or None
        platform = (sub.get("platform") or "").lower()
        if platform not in self.active_platforms:
            logger.debug("kanban notifier: subscription for %s on %s skipped; adapter not connected",
                         sub.get("task_id"), platform or "<missing>")
            return None
        from gateway.config import Platform
        if _adapter_for_subscription(self.runner, Platform(platform), sub, owner_profile or self.notifier_profile) is None:
            _warn_anchorless_thread_sub_once(sub, platform)
            return None
        old_cursor, cursor, events = _kbn().claim_unseen_events_for_sub(
            conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
            thread_id=sub.get("thread_id") or "", kinds=TERMINAL_KINDS,
        )
        if not events:
            return None
        task = self.kb.get_task(conn, sub["task_id"])
        # A failure booked on a card that is already terminal is a stale echo of an attempt
        # that outlived its own handoff — dropping it here keeps the cursor moving while
        # neither pinging nor waking the owner about closed work. Whether it IS stale is
        # decided per event: the card's closure time against the event's OWN timestamp, never
        # the status at delivery time (that status silently swallowed five real crashes when
        # the notifier ran 569 s after the archive).
        closures: Optional[list[int]] = None
        if any(ev.kind in _STALE_ON_CLOSED_CARD_KINDS for ev in events):
            closures = card_closures(conn, sub["task_id"], task)

        def _is_stale(ev) -> bool:
            closure_ts = closure_before_event(
                conn, sub["task_id"], int(getattr(ev, "created_at", 0) or 0),
                closures=closures, task=task,
            )
            return event_is_stale_for_closed_card(ev, task, closure_ts)

        kept = [ev for ev in events if not _is_stale(ev)]
        if len(kept) != len(events):
            logger.info(
                "kanban notifier: dropped %d stale failure event(s) for card %s — booked "
                "after it closed (closures %s)",
                len(events) - len(kept), sub["task_id"], closures,
            )
        if not kept:
            return None
        events = kept
        events, categories = self._apply_owner_policy(conn, sub, platform, events)
        if not events:
            return None
        logger.debug("kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                     len(events), sub["task_id"], slug, old_cursor, cursor)
        return {"sub": sub, "old_cursor": old_cursor, "cursor": cursor, "events": events,
                "task": task, "board": slug, "categories": categories,
                "owner_policy": self.owner_policy}

    def _apply_owner_policy(self, conn: Any, sub: dict, platform: str, events: list):
        """Keep only owner-facing events for a covered platform; journal every drop.

        The claim already advanced the cursor, so a dropped event never comes back — the
        suppression journal is what keeps that from being a silent loss. Every other
        platform (desktop ``local``, api_server, cron) returns the events untouched.
        """
        if not self.owner_policy.covers(platform):
            return events, {}
        task_id = sub["task_id"]
        task = self.kb.get_task(conn, task_id)
        open_ancestor = has_open_ancestor(conn, task_id)
        categories: dict = {}
        kept: list = []
        for ev in events:
            category, reason = classify_kanban_event(
                kind=ev.kind,
                payload=getattr(ev, "payload", None),
                task=task,
                has_open_ancestor=open_ancestor,
                extensions=self.owner_policy.artifact_extensions,
            )
            if category is None:
                record_suppressed(
                    task_id=task_id,
                    event_kind=ev.kind,
                    platform=platform,
                    chat_id=sub.get("chat_id") or "",
                    reason=reason,
                )
                continue
            categories[ev.id] = (category, reason)
            kept.append(ev)
        if len(kept) != len(events):
            logger.debug(
                "kanban notifier: owner policy suppressed %d/%d event(s) for %s on %s",
                len(events) - len(kept), len(events), task_id, platform,
            )
        return kept, categories

    def collect_board(self, slug: str) -> None:
        """Claim events on one board, appending delivery dicts to ``deliveries``."""
        if not self._board_has_subs(slug):
            return
        try:
            conn = _kbc().connect(board=slug)
        except Exception as exc:
            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
            return
        try:
            if self.gc_due:
                self._gc_stale_subs(conn, slug)
            # No explicit init_db(): connect() already runs the migration once per
            # process, and init_db() would re-run it on a second connection racing
            # the first.
            subs = _kbn().list_notify_subs(conn, notifier_profiles=self.notifier_profiles, include_unowned=self.include_unowned)
            if not subs:
                logger.debug("kanban notifier: board %s has no subscriptions", slug)
            for sub in subs:
                try:
                    claimed = self._claim_for_sub(conn, slug, sub)
                    if claimed is not None:
                        self.deliveries.append(claimed)
                except Exception as sub_exc:
                    # One bad subscription must not block the rest of the tick.
                    logger.warning("kanban notifier: subscription for %s on board %s failed: %s",
                                   sub.get("task_id"), slug, sub_exc)
        finally:
            conn.close()


def _notifier_collect(runner: Any, kb: Any, *, notifier_profile: Optional[str], gc_due: bool, gc_retention_days: int) -> list[dict]:
    """Claim unseen terminal events for every owned subscription on every board.

    Each gateway polls only subscriptions owned by profiles whose adapters it
    hosts; legacy rows without a profile stamp are visible only to the process
    holding the singleton dispatcher lock.
    """
    return _Collector(
        runner, kb, notifier_profile=notifier_profile, gc_due=gc_due, gc_retention_days=gc_retention_days,
    ).collect()


# --- Per-event message formatting: kind -> (msg, wake_handoff, wake_review_detail) ---
# ``None`` for handoff / review_detail leaves the accumulated wake value untouched.


def _payload(ev: Any, key: str) -> Any:
    """Shared "payload present and truthy" read."""
    return ev.payload.get(key) if ev.payload and ev.payload.get(key) else None


def _clip(ev: Any, key: str, msg_key: str, limit: int) -> str:
    """Catalog message ``msg_key`` (``{value}`` placeholder) rendered with the truncated payload
    value, or ``""`` when absent."""
    value = _payload(ev, key)
    return t(msg_key, value=str(value)[:limit]) if value else ""


_NL = "\n{}"


def _first_line(text: str, limit: int) -> str:
    lines = text.strip().splitlines()
    return lines[0][:limit] if lines else text[:limit]


def _fmt_completed(ev, n) -> tuple:
    # Prefer the run summary from the event payload; fall back to task.result for legacy rows.
    wake_handoff = None
    payload_summary = _payload(ev, "summary")
    if payload_summary:
        wake_handoff = _first_line(str(payload_summary), 200)
    elif n.task and n.task.result:
        wake_handoff = _first_line(n.task.result, 160)
    handoff = f"\n{wake_handoff}" if wake_handoff is not None else ""
    return t("gateway.kanban.ping.completed", head=n.head, title=n.title, handoff=handoff), wake_handoff, None


def _fmt_review_requested(ev, n) -> tuple:
    # Implementation done; task moved to the review lane. Carry the handoff
    # into the wake turn like ``completed`` so the reviewer needn't re-read the board.
    handoff = ""
    wake_handoff = None
    summary = _payload(ev, "summary")
    if summary:
        summary = str(summary)
        handoff = f"\n{summary[:200]}"
        wake_handoff = _first_line(summary, 200)
    return t("gateway.kanban.ping.review_requested", head=n.head, title=n.title, handoff=handoff), wake_handoff, None


def _fmt_changes_requested(ev, n) -> tuple:
    payload = ev.payload or {}
    reason = _safe_review_reason(payload.get("reason"))
    reviewer = _safe_review_reason(payload.get("reviewer"), 48)
    implementer = _safe_review_reason(payload.get("implementer"), 48)
    reason_text = reason or t("gateway.kanban.ping.changes_default_reason")
    provenance = t("gateway.kanban.ping.reviewer_suffix", reviewer=reviewer) if reviewer else ""
    if implementer:
        provenance += t("gateway.kanban.ping.implementer_suffix", implementer=implementer)
    msg = t("gateway.kanban.ping.changes_requested",
            board_tag=n.board_tag, task_id=n.task_id, reason=reason_text, provenance=provenance)
    return msg, None, reason_text


def _fmt_block_loop_detected(ev, n) -> tuple:
    """Re-blocked for the same cause past the limit and routed to `triage`.

    It emits no blocked/status event, so ping loudly here. A repeated-block
    circuit breaker establishes that orchestration attention is needed; it
    does NOT establish that a human decision or owner input exists. Use
    neutral orchestration wording unless the block was typed as a genuine
    owner-input request (`needs_input`, the only kind that carries a concrete
    question for the owner).
    """
    kind = _payload(ev, "kind")
    decision = kind == "needs_input"
    msg = t(
        "gateway.kanban.ping.triage", head=n.head,
        why=t("gateway.kanban.ping.triage_decision" if decision else "gateway.kanban.ping.triage_attention"),
        recurrences=_clip(ev, "recurrences", "gateway.kanban.ping.triage_recurrences", 200),
        reason=_clip(ev, "reason", "gateway.kanban.ping.reason_suffix", 160),
    )
    return msg, None, None


def _fmt_gave_up(ev, n) -> tuple:
    # The dispatcher auto-blocked the task after ``failures`` consecutive non-success attempts
    # (spawn failure, crash, or timeout alike): it is now Blocked and waiting for a human.
    failures = _payload(ev, "failures")
    count = (t("gateway.kanban.ping.failed_n_times", count=int(failures)) if failures
             else t("gateway.kanban.ping.kept_failing"))
    last = _clip(ev, "error", "gateway.kanban.ping.last_error", 160)
    return t("gateway.kanban.ping.gave_up", head=n.head, count=count, last=last, task_id=n.task_id), None, None


def _fmt_timed_out(ev, n) -> tuple:
    limit = int(_payload(ev, "limit_seconds") or 0)
    minutes = max(1, round(limit / 60)) if limit else 0
    span = t("gateway.kanban.ping.limit_minutes", minutes=minutes) if minutes else t("gateway.kanban.ping.limit_generic")
    return t("gateway.kanban.ping.timed_out", head=n.head, span=span), None, None


# archived / unblocked are claimed (so the cursor advances past them) but
# intentionally silent (no formatter), and excluded from _WAKE_KINDS so they
# never wake the creator.
_EVENT_FORMATTERS: dict[str, Callable[[Any, _KanbanNotification], tuple]] = {
    "completed": _fmt_completed,
    "blocked": lambda ev, n: (
        t("gateway.kanban.ping.blocked", head=n.head, reason=_clip(ev, "reason", "gateway.kanban.ping.reason_suffix", 160)),
        None, None,
    ),
    "gave_up": _fmt_gave_up,
    "crashed": lambda ev, n: (t("gateway.kanban.ping.crashed", head=n.head), None, None),
    "timed_out": _fmt_timed_out,
    "status": lambda ev, n: (t("gateway.kanban.ping.status", head=n.head, status=_payload(ev, "status") or ""), None, None),
    "review_requested": _fmt_review_requested,
    "changes_requested": _fmt_changes_requested,
    "block_loop_detected": _fmt_block_loop_detected,
    "commented": lambda ev, n: (
        t("gateway.kanban.ping.commented", head=n.head,
          author=_clip(ev, "author", "gateway.kanban.ping.comment_author", 60),
          preview=_clip(ev, "preview", "gateway.kanban.ping.comment_body", 600)),
        None, None,
    ),
}


# --- Delivery of one claimed batch (one subscription, N events) ---


class _KanbanNotification:
    """Deliver one subscription's claimed events, then settle the cursor.

    Both legs of notify+wake must succeed before settling. Sent pings have a
    separate durable checkpoint so wake retries do not resend them. Admission
    is at-least-once queueing, not an execution or final-response receipt.
    """

    def __init__(self, runner: Any, d: dict, *, platform_cls: Any, sub_fail_counts: dict) -> None:
        self.runner = runner
        self.d = d
        self.platform_cls = platform_cls
        self.sub_fail_counts = sub_fail_counts
        self.sub = sub = d["sub"]
        self.task = task = d["task"]
        self.board_slug = d.get("board")
        self.platform_str = (sub["platform"] or "").lower()
        self.task_id = sub["task_id"]
        self.sub_profile = sub.get("notifier_profile") or ""
        self.title = (task.title if task else sub["task_id"])[:120]
        self.board_tag = t("gateway.kanban.ping.board_tag", board=self.board_slug) if self.board_slug else ""
        # Attribute the ping to the worker that did the work.
        tag = t("gateway.kanban.ping.assignee_tag", assignee=task.assignee) if task and task.assignee else ""
        self.head = t("gateway.kanban.ping.head", board_tag=self.board_tag, assignee_tag=tag, task_id=self.task_id)
        # The wake self-post path needs the key even when every event was skipped.
        self.sub_key = (sub["task_id"], sub["platform"], sub["chat_id"], sub.get("thread_id") or "")
        mode = sub.get("delivery_mode") or "notify"
        self.wake_agent = mode in ("notify+wake", "wake")
        self.send_passive = mode != "wake"
        # Worker handoff carried into the synthetic wake turn so the woken
        # creator doesn't re-decompose work already on the board.
        self.wake_handoff = self.wake_review_detail = self.session_key = self.synth = ""
        self.plat: Any = None
        self.adapter: Any = None
        self.is_push_adapter = True
        self.wake_kinds: set = set()
        # Owner delivery policy: set by the collector for covered platforms only.
        # Absent → ``enabled=False`` → every path below behaves exactly as before.
        self.categories: dict = d.get("categories") or {}
        self.owner_policy = d.get("owner_policy") or OwnerDeliveryConfig(enabled=False)
        self.policy_active = self.owner_policy.covers(self.platform_str)

    # -- cursor / subscription ops (blocking, run in a fresh-context thread) --

    async def rewind(self) -> None:
        await _to_thread_process_service(
            self.runner._kanban_rewind, self.sub, self.d["cursor"], self.d.get("old_cursor", 0), self.board_slug,
        )

    async def advance(self) -> None:
        await _to_thread_process_service(self.runner._kanban_advance, self.sub, self.d["cursor"], self.board_slug)

    async def unsub(self) -> None:
        await _to_thread_process_service(self.runner._kanban_unsub, self.sub, self.board_slug)

    def clear_failures(self) -> None:
        self.sub_fail_counts.pop(self.sub_key, None)

    async def delivery_failed(self, fmt: str, prefix: tuple, drop_fmt: str, exc: Exception, exc_info: bool) -> None:
        """Bump the failure counter; drop the sub past the limit, else rewind the claim so the next tick retries."""
        fails = self.sub_fail_counts.get(self.sub_key, 0) + 1
        self.sub_fail_counts[self.sub_key] = fails
        logger.warning(fmt, *prefix, fails, MAX_SEND_FAILURES, exc, exc_info=exc_info)
        if fails >= MAX_SEND_FAILURES:
            logger.warning(drop_fmt, self.task_id, self.platform_str, fails)
            await self.unsub()
            self.clear_failures()
        else:
            await self.rewind()

    async def _wake_failed(self, fmt: str, exc: Exception, *, check: tuple = ()) -> None:
        """Bump the failure counter on a failed wake. ``check`` is the refusal's own diagnosis
        (``field``/``detail``/``reason``), folded into the exception the warning prints so the line
        names the check that refused instead of only "wake failed"."""
        drop_fmt = "kanban notifier: dropping subscription %s on %s after %d consecutive wake failures"
        from gateway.wake import WakeNotAccepted
        show_tb = not isinstance(exc, WakeNotAccepted)  # a refusal is explained, not a crash
        field, detail, reason = check or (None, None, getattr(exc, 'reason', None))
        if field or detail:
            exc = RuntimeError(f'{reason or "refused"} field={field} detail={detail}')
        await self.delivery_failed(fmt, (self.task_id, self.platform_str), drop_fmt, exc,
                                   exc_info=show_tb)

    # -- formatting --

    def _category_for_kind(self, kind: str) -> Optional[str]:
        """Owner category of a claimed event kind (policy platforms only)."""
        for ev in self.d["events"]:
            if ev.kind == kind:
                category = self.event_category(ev)
                if category:
                    return category
        return None

    def event_category(self, ev: Any) -> Optional[str]:
        """``decision`` / ``final`` / ``None`` for ``ev`` as classified at claim time."""
        entry = self.categories.get(getattr(ev, "id", None))
        return entry[0] if entry else None

    def format_event(self, ev: Any) -> Optional[str]:
        """Render one event; accumulates wake handoff/review detail. None → silent kind."""
        formatter = _EVENT_FORMATTERS.get(ev.kind)
        if formatter is None:
            return None
        msg, handoff, review_detail = formatter(ev, self)
        if handoff is not None:
            self.wake_handoff = handoff
        if review_detail is not None:
            self.wake_review_detail = review_detail
        if self.policy_active:
            msg = self._owner_facing_text(ev, msg)
        return msg

    def _owner_facing_text(self, ev: Any, msg: str) -> str:
        """A policy message must carry its own substance: the deliverable's summary or the
        decision's question. A bare "✅" or "статус: blocked" is the noise the policy removes."""
        category = self.event_category(ev)
        payload = getattr(ev, "payload", None) or {}
        if category == "final":
            summary = str(payload.get("summary") or "").strip()
            if not summary and self.task is not None:
                summary = str(getattr(self.task, "result", "") or "").strip()
            if summary and summary[:60] not in msg:
                msg += "\n" + _first_line(summary, 400)
        elif category == "decision":
            reason = str(payload.get("reason") or "").strip()
            if reason and reason[:40] not in msg:
                msg += "\n" + t("gateway.kanban.ping.reason_suffix", reason=reason[:400])
        return msg

    def build_wake_text(self) -> None:
        """Set ``wake_kinds`` / ``session_key`` / ``synth`` for the wake paths."""
        task, sub = self.task, self.sub
        self.wake_kinds = {ev.kind for ev in self.d["events"] if ev.kind in _WAKE_KINDS} if self.wake_agent else set()
        if self.policy_active:
            # Only a decision justifies spending a turn of the creator's session: a final
            # deliverable is delivered passively (text + files), and service events never
            # reached this point at all. Without this, every suppressed-looking event still
            # produced a second message — the agent's own reply to the wake.
            self.wake_kinds = {k for k in self.wake_kinds if self._category_for_kind(k) == "decision"}
        self.wake_diagnostic = all(diagnostic_event(ev) for ev in self.d["events"] if ev.kind in self.wake_kinds)
        if not self.wake_kinds:
            return
        if self.is_push_adapter:
            self.session_key = getattr(task, "session_id", None) or ""
        else:
            # Non-push wakes target sub["chat_id"] (the raw session id the
            # subscriber registered). task.session_id may be a WORKER session
            # for child tasks; use it only for legacy rows.
            self.session_key = sub["chat_id"] or getattr(task, "session_id", None) or ""
        # i18n keys: gateway.kanban.wake.<kind> for each _WAKE_KINDS entry.
        _parts = [t(f"gateway.kanban.wake.{k}") for k in _WAKE_KINDS if k in self.wake_kinds]
        _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
        synth = t(
            "gateway.kanban.wake.message",
            task_id=sub["task_id"], status=_status, title=self.title,
            assignee=task.assignee if task else "", board=self.board_slug,
        )
        # Label as an automatic notification and carry the handoff so the
        # creator inspects the board instead of re-decomposing.
        if self.wake_handoff:
            synth += "\n" + t("gateway.kanban.wake.handoff", summary=self.wake_handoff)
        if self.wake_review_detail:
            synth += "\n" + t("gateway.kanban.wake.review_detail", reason=self.wake_review_detail)
        self.synth = synth + "\n\n" + t("gateway.kanban.wake.guidance")

    def _log_woke(self) -> None:
        logger.info("kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                    self.task_id, self.platform_str, self.sub["chat_id"], self.sub_profile or "default", self.wake_kinds)

    def _served_wake_profile(self) -> Optional[str]:
        """The subscription's profile when THIS gateway is a multiplexer serving it, else ``None``.

        ``None`` keeps the historical path: a standalone ``hermes -p <name>`` gateway owns its own
        listener and key, so its api_server wakes keep using the HTTP self-post.
        """
        if not self.sub_profile:
            return None
        if not getattr(getattr(self.runner, "config", None), "multiplex_profiles", False):
            return None
        return self.sub_profile

    def _owner_scope(self):
        """Runtime scope of the subscription's profile under multiplex, else a no-op context."""
        runner = self.runner
        served_profile = self._served_wake_profile()
        if not served_profile:
            return contextlib.nullcontext()
        from gateway.run import _async_profile_runtime_scope
        from gateway.session import SessionSource
        source = SessionSource(platform=self.plat, chat_id=self.sub["chat_id"], profile=served_profile)
        return _async_profile_runtime_scope(runner._resolve_profile_home_for_source(source))

    async def wake(self) -> None:
        """Wake the creator session (raises on failure): push adapters get a full SessionSource, non-push a raw self-post."""
        from gateway.wake import deliver_wake
        sub = self.sub
        if not self.is_push_adapter:
            # A served profile's raw-session wake runs in THAT profile's scope, in-process: the
            # shared listener's /p/<profile>/ self-post would need the profile's own
            # API_SERVER_KEY, which a route-only profile legitimately does not have, and an
            # unprefixed self-post would resume the session in the DEFAULT profile's store.
            async with self._owner_scope():
                await deliver_wake(self.adapter, text=self.synth, session_id=self.session_key,
                                   profile=self._served_wake_profile(),
                                   notification_category="diagnostic" if self.wake_diagnostic else "result")
            self._log_woke()
            return
        from gateway.session import SessionSource
        # Rebuild the creator's real session scope from the persisted chat_type:
        # build_session_key() keys DMs differently from group/thread, so a
        # hardcoded "group" mis-routed DM/thread creators into a fresh session.
        # Legacy rows may carry chat_type in delivery_metadata; last resort is
        # "group". A mismatch only degrades to a fresh session.
        # Legacy rows written before the column existed may still carry chat_type in delivery_metadata
        # (#60600 rows) — fall back to that, then to "group" (the historical default that suits the
        # dashboard/group flows). handle_message() get_or_create_session's the target, so a mismatch only
        # ever degrades to a fresh session, never an exception.
        _delivery_meta = sub.get("delivery_metadata") or {}
        _chat_type = str(sub.get("chat_type") or _delivery_meta.get("chat_type") or "").strip()
        _source = SessionSource(
            platform=self.plat, chat_id=sub["chat_id"], chat_type=_chat_type or "group",
            thread_id=sub.get("thread_id") or None, user_id=sub.get("user_id"), user_id_alt=sub.get("user_id_alt"),
            profile=self.sub_profile or None, scope_id=_wake_scope_id(self.adapter, sub),
            parent_chat_id=_delivery_meta.get("parent_chat_id"),
        )
        _source._transport_adapter_ref = weakref.ref(self.adapter)
        from gateway.run import _async_profile_runtime_scope
        if self.sub_profile and getattr(getattr(self.runner, "config", None), "multiplex_profiles", False):
            from hermes_cli.profiles import profile_exists
            if not profile_exists(self.sub_profile):
                raise RuntimeError(f"Kanban wake profile {self.sub_profile!r} no longer exists")
        async with _async_profile_runtime_scope(self.runner._resolve_profile_home_for_source(_source)):
            await deliver_wake(self.adapter, text=self.synth, session_id=self.session_key, source=_source,
                               notification_category="diagnostic" if self.wake_diagnostic else "result")
        self._log_woke()

    async def _send_event(self, ev: Any, msg: str) -> bool:
        """Send one text ping; raises on adapter exception or SendResult(success=False)."""
        from gateway.warning_notifications import present_notification
        sub, adapter = self.sub, self.adapter
        delivery_metadata = sub.get("delivery_metadata")
        metadata: dict[str, Any] = dict(delivery_metadata) if isinstance(delivery_metadata, dict) else {}
        if sub.get("thread_id") and not metadata.get("thread_id"):
            metadata["thread_id"] = sub["thread_id"]
        _send_res = None
        async def send_ping():
            nonlocal _send_res
            _send_res = await adapter.send(sub["chat_id"], msg, metadata=metadata)
        if not await present_notification(send_ping, platform=self.platform_str, diagnostic=diagnostic_event(ev)):
            return False
        # SendResult(success=False) without an exception is a FAILED delivery
        # (else the event is lost); None / non-SendResult keeps the
        # "no exception == delivered" contract.
        if getattr(_send_res, "success", True) is False:
            raise RuntimeError(f"adapter send() reported failure: {getattr(_send_res, 'error', None) or 'unknown error'}")
        logger.debug("kanban notifier: delivered %s event for %s to %s/%s on board %s",
                     ev.kind, self.task_id, self.platform_str, sub["chat_id"], self.board_slug)
        # Upload artifact paths from the handoff payload / legacy result as
        # native files. Both handoff kinds stage files for exactly this: a
        # review-bound card's files exist precisely so the human sees them at
        # handoff time. Retry exposure matches ``completed`` (the sub cursor is
        # rewound only when a send failed).
        if ev.kind in ("completed", "review_requested"):
            event_payload = getattr(ev, "payload", None)
            if self.policy_active:
                # Owner policy: the artifact set is NOT narrowed by extension any more — a card's
                # ``.py``/log/JSON is a result like a PDF. The extension list is journalled when it
                # would have hidden a file, so nothing disappears without a trace.
                _journal_offlist(self.task_id, self.platform_str, sub["chat_id"],
                                 event_payload, self.owner_policy.artifact_extensions)
                event_payload = _policy_artifact_payload(event_payload, self.owner_policy.artifact_extensions)
                if event_payload is None:
                    return True
            try:
                await self.runner._deliver_kanban_artifacts(
                    adapter=adapter, chat_id=sub["chat_id"], metadata=metadata,
                    event_payload=event_payload, task=None if self.policy_active else self.task,
                )
            except Exception as art_exc:
                logger.debug("kanban notifier: artifact delivery for %s failed: %s", self.task_id, art_exc)
        return True

    async def _send_pings(self) -> bool:
        """Send every text ping; False when a send failed (claim already rewound/dropped)."""
        for ev in self.d["events"]:
            msg = self.format_event(ev)
            if msg is None:
                continue
            # Non-push adapters (api_server) always report SendResult(success=False)
            # from send(); treating that as failure would drop the sub forever and
            # make the wake path unreachable. Skip the doomed send; the self-post
            # IS the delivery and resolves the failure counter.
            if not self.is_push_adapter and self.wake_agent:
                logger.debug(
                    "kanban notifier: adapter %s has no push channel; skipping text ping for %s, relying "
                    "on wake self-post instead", self.platform_str, self.task_id,
                )
                continue
            if not self.send_passive:
                # Wake-only: the wake path is the sole delivery and resolves the counter.
                continue
            if ev.id <= self.sub.get("last_ping_event_id", 0):
                continue
            try:
                if await self._send_event(ev, msg) is False:
                    continue
                await _to_thread_process_service(partial(
                    self.runner._kanban_sub_op, self.board_slug, "record_notify_ping", self.sub,
                    event_id=ev.id,
                ))
                self.clear_failures()
            except Exception as exc:
                await self.delivery_failed(
                    "kanban notifier: send failed for %s on %s (attempt %d/%d): %s", (self.task_id, self.platform_str),
                    "kanban notifier: dropping subscription %s on %s after %d consecutive send failures", exc, False,
                )
                return False
        return True

    async def deliver(self) -> None:
        try:
            self.plat = self.platform_cls(self.platform_str)
        except ValueError:
            await self.advance()
            return
        # Recheck the exact route after claiming: config/adapters can change between ticks. The
        # recheck reads the served profile's session store for a stateless destination, so it runs
        # off the event loop (the claim path already collects in a worker thread).
        adapter = await asyncio.to_thread(
            _adapter_for_subscription, self.runner, self.plat, self.sub, self.sub_profile or None)
        if adapter is None:
            logger.debug("kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                         self.platform_str, self.task_id)
            await self.rewind()
            return
        self.adapter = adapter
        from gateway.wake import adapter_supports_push
        self.is_push_adapter = adapter_supports_push(adapter)

        # Pings, artifact uploads (media policy) and the wake text (display.language) all read the
        # SUBSCRIBER profile's config; the notifier thread itself runs in the launch profile's scope.
        async with self._owner_scope():
            if not await self._send_pings():
                return
            # All text pings delivered (or skipped for non-push / wake-only).
            original_events = self.d["events"]
            from gateway.warning_notifications import warning_notifications_enabled
            split = not warning_notifications_enabled(self.platform_str)
            wake_groups = ([original_events] if not split else [
                [ev for ev in original_events if diagnostic_event(ev)],
                [ev for ev in original_events if not diagnostic_event(ev)],
            ])
            wake_payloads = []
            for events in wake_groups:
                if not events:
                    continue
                self.d = {**self.d, "events": events}
                self.wake_handoff = self.wake_review_detail = ""
                for ev in events:
                    self.format_event(ev)
                self.build_wake_text()
                if self.wake_kinds:
                    wake_payloads.append((self.synth, self.wake_diagnostic, self.wake_kinds))
            self.d = {**self.d, "events": original_events}
        is_push = self.is_push_adapter
        from gateway.wake import WakeNotAccepted

        # A requested wake is required even when its passive ping already landed.
        if wake_payloads:
            try:
                for self.synth, self.wake_diagnostic, self.wake_kinds in wake_payloads:
                    await self.wake()
                self.clear_failures()
            except WakeNotAccepted as _wake_exc:
                # Startup / a full queue is not a dead destination: keep the durable subscription
                # and retry. A refusal whose FIELD proves the destination store cannot serve this
                # session at all (an absent or unreadable receipt) never clears — retrying it every
                # tick is what flooded the journal — so it takes the bounded failure path instead.
                from gateway.wake import PERMANENT_REFUSAL_FIELDS
                if getattr(_wake_exc, 'field', None) in PERMANENT_REFUSAL_FIELDS:
                    await self._wake_failed(
                        "kanban notifier: wake refused for %s on %s (attempt %d/%d): %s", _wake_exc,
                        check=(_wake_exc.field, _wake_exc.detail, _wake_exc.reason))
                    return
                logger.info(
                    "kanban notifier: wake deferred for %s on %s (reason=%s field=%s); claim rewound",
                    self.task_id, self.platform_str, getattr(_wake_exc, 'reason', None),
                    getattr(_wake_exc, 'field', None))
                await self.rewind()
                return
            except Exception as _wk_err:
                await self._wake_failed(
                    "kanban notifier: wake-only delivery failed for %s on %s (attempt %d/%d): %s" if is_push
                    else "kanban notifier: wake self-post failed for %s on %s (attempt %d/%d): %s",
                    _wk_err,
                )
                return

        # Delivery complete: advance the cursor (the dedup mechanism).
        await self.advance()
        if not is_push:
            self.clear_failures()
        # Unsubscribe only on archive; ``done`` is reversible.
        if self.task and self.task.status == "archived":
            await self.unsub()
