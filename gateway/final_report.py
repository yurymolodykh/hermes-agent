"""Single, directly-callable delivery of a terminal card's final report.

Card t_1fd3f22b (mission «финальный отчёт → Telegram владельца», map
``C:/Users/hermes/durable-results/t_3a77e498/integration_map_final_report_tg.md``):

    «implement the actual delivery path using the existing report generator
     without changing report structure or format»

Design contract (all pieces already existed; this module only COMPOSES them —
the report template/format is untouched, the HTML file is shipped byte-for-byte
as it sits on disk):

=====================================  ==========================================
what                                   existing owner (unchanged)
=====================================  ==========================================
итог text                              ``delivery_engine.owui_root_final_summary``
one-liner                              ``kanban_db.branch_summary_message``
HTML/report artifact paths             ``delivery_engine.owui_root_artifact_paths``
text send                              ``<adapter>.send(chat_id, text, ...)``
file send                              ``kanban_watchers._deliver_kanban_artifacts``
                                       (``send_document``, 25 MB cap, dedup)
owner chat_id                          ``kanban_notify_subs`` (native telegram),
                                       ``kanban_db.resolve_terminal_owner_tg_chat``,
                                       ``delivery_engine.resolve_author_telegram``
audit record                           ``delivery_engine.log_report_delivery``
=====================================  ==========================================

Two rules are load-bearing here:

* **No trigger wiring.** This function is called by whoever owns the terminal
  transition; it never hooks the watcher loop itself (card t_7771f7b5).
* **Never raises.** A Telegram failure/absence degrades into
  ``FinalReportOutcome(delivered=False, reason=...)`` so a caller's terminal
  transition can never be broken by a send (card t_2e53d211 turns that into an
  explicit error log; the outcome already carries the reason).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

logger = logging.getLogger("gateway.final_report")

#: Reason codes returned in ``FinalReportOutcome.reason`` when nothing was sent.
REASON_NO_TASK = "no_card_id"
REASON_NO_OWNER_LANE = "no_owner_tg_lane"
REASON_NO_ADAPTER = "no_adapter"
REASON_CONTENTLESS = "contentless_bare_itog"
REASON_ALREADY_DELIVERED = "already_delivered"


@dataclass
class FinalReportOutcome:
    """Result of one ``send_final_report`` attempt (never an exception)."""

    delivered: bool                 # True only when the adapter accepted the TEXT
    task_id: str
    chat_id_masked: str = ""
    #: ``report_type_of(artifacts)`` — the same buckets the branch lane uses.
    report_type: str = "none"
    #: Exactly the text handed to ``adapter.send`` (unchanged итог composition).
    text: str = ""
    #: Report files that were resolved for upload (absolute paths).
    artifacts: List[str] = field(default_factory=list)
    artifacts_sent: List[str] = field(default_factory=list)
    artifacts_failed: List[str] = field(default_factory=list)
    artifacts_skipped: List[str] = field(default_factory=list)
    #: Why nothing (or not everything) was delivered. Empty on full success.
    reason: str = ""
    #: Where the owner chat_id came from: ``native_telegram_sub`` /
    #: ``terminal_owner`` / ``author_binding`` / ``explicit``.
    chat_source: str = ""
    #: True when a dedup key was supplied and had already been marked.
    already_delivered: bool = False
    #: Raw chat id — kept out of ``repr`` and never persisted; only the journal
    #: helper needs it (it masks the value itself before writing).
    _raw_chat_id: str = field(default="", repr=False, compare=False)


def _mask(chat_id: Any) -> str:
    from gateway.delivery_engine import mask_chat_id

    return mask_chat_id(chat_id)


def _report_type(paths: Any) -> str:
    from gateway.delivery_engine import report_type_of

    return report_type_of(paths)


def resolve_owner_tg_lane(
    conn: Any,
    root_id: str,
    *,
    board: Optional[str] = None,
) -> tuple[Optional[str], str, str]:
    """Resolve the owner's Telegram chat for ``root_id``.

    Returns ``(chat_id | None, thread_id, source)``.  The order is the order
    ``kanban_db.ensure_terminal_delivery_subscription`` uses — it is NOT an
    optimization: on the live board 41 cards carry only a ``telegram``
    subscription (their ``user_id`` is the TG id itself, absent from
    ``owui_telegram_users()``), so both OWUI-side resolvers return ``None``
    for them while the address is in fact known (map §6.4).
    """
    from hermes_cli import kanban_db as kb

    if not root_id:
        return None, "", ""
    # (a) the card's own native Telegram subscription — the channel it was
    #     ordered from when the owner talks to the bot directly.
    try:
        for sub in kb.list_notify_subs(conn, root_id):
            if str(sub.get("platform") or "").lower() != "telegram":
                continue
            chat = str(sub.get("chat_id") or "").strip()
            if chat:
                return chat, str(sub.get("thread_id") or ""), "native_telegram_sub"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("final report: native TG sub lookup for %s failed: %s", root_id, exc)

    # (b) api_server subscription whose verified owner has a server-side
    #     OWUI→TG binding (single source of truth: delivery_policy).
    try:
        owner_chat = kb.resolve_terminal_owner_tg_chat(conn, root_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("final report: terminal owner lookup for %s failed: %s", root_id, exc)
        owner_chat = None
    if owner_chat:
        return str(owner_chat), "", "terminal_owner"

    # (c) last resort: map an api_server subscription's author through the
    #     same helper the delivery engine uses (no second mapping).
    try:
        subs = [
            s for s in kb.list_notify_subs(conn, root_id)
            if str(s.get("platform") or "").lower() == "api_server"
        ]
        if subs:
            from gateway.delivery_engine import resolve_author_telegram
            from hermes_cli.delivery_policy import owui_telegram_users

            task = kb.get_task(conn, root_id)
            for sub in subs:
                chat = resolve_author_telegram(task, sub, owui_telegram_users())
                if chat:
                    return str(chat), str(sub.get("thread_id") or ""), "author_binding"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("final report: author binding lookup for %s failed: %s", root_id, exc)
    return None, "", ""


def _compose(conn: Any, root_id: str, board: Optional[str]) -> tuple[str, List[str]]:
    """Return ``(итог text, absolute artifact paths)`` using the existing helpers."""
    from gateway.delivery_engine import owui_root_artifact_paths, owui_root_final_summary
    from hermes_cli import kanban_db as kb

    msg = "✅ Задача выполнена"
    try:
        st = kb.branch_state(conn, root_id)
        if st:
            # Same verdict wording as the branch-summary watcher: an acceptance
            # refusal reaches the owner as «доработка отклонена», not as the
            # generic failure line (owner-rule 03.11.2026, t_f6790ef5).
            refused_ids = (
                kb.acceptance_refusal_ids(conn, root_id, st["failed"])
                if st["outcome"] == "failure" else []
            )
            msg = kb.branch_summary_message(
                st["outcome"], st["failed"], refused_ids=refused_ids,
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("final report: branch state for %s failed: %s", root_id, exc)

    plan = {"root_id": root_id, "board": board}
    try:
        # inline_report=False: the report rides send_document as a FILE, never
        # duplicated into the message body (F1 terminal-итог contract).
        text = owui_root_final_summary(plan, msg, inline_report=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("final report: итог composition for %s failed: %s", root_id, exc)
        text = msg
    try:
        artifacts = list(owui_root_artifact_paths(plan) or [])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("final report: artifact resolution for %s failed: %s", root_id, exc)
        artifacts = []
    return text, artifacts


async def send_final_report(
    card_or_mission: Any,
    *,
    board: Optional[str] = None,
    adapter: Any = None,
    adapter_resolver: Optional[Callable[[], Any]] = None,
    send_text: Optional[Callable[..., Any]] = None,
    send_document: Optional[Callable[..., Any]] = None,
    metadata: Optional[dict] = None,
    dedup_key: Optional[str] = None,
    trigger: str = "manual_call",
    dry_run: bool = False,
) -> FinalReportOutcome:
    """Deliver the terminal card's final report (text + files) to the owner TG.

    ``card_or_mission`` is a task id (``"t_…"``) or an object exposing ``id``.
    The text goes through ``send_text`` (default ``adapter.send``) and every
    resolved report file through ``send_document`` (default
    ``GatewayKanbanWatchersMixin._deliver_kanban_artifacts`` → ``send_document``,
    i.e. the exact production path: 25 MB cap, unsafe-path filter, content
    dedup, and the file bytes untouched).

    ``dry_run`` composes text+artifacts and returns them without sending.

    Never raises; every failure is a ``FinalReportOutcome`` with ``reason``.
    """
    root_id = str(getattr(card_or_mission, "id", card_or_mission) or "").strip()
    outcome = FinalReportOutcome(delivered=False, task_id=root_id)
    if not root_id:
        outcome.reason = REASON_NO_TASK
        return outcome

    from hermes_cli import kanban_db as kb

    conn = None
    try:
        conn = kb.connect(board=board)
        text, artifacts = _compose(conn, root_id, board)
        outcome.text = text
        outcome.artifacts = artifacts
        outcome.report_type = _report_type(artifacts)

        # Fail-closed: an итог with neither content nor a report file is the
        # contentless ping the owner banned (F1).  The branch lane suppresses
        # it; this entry point must not resurrect it.
        if (
            not artifacts
            and (text or "").strip() == "✅ Задача выполнена"
        ):
            outcome.reason = REASON_CONTENTLESS
            _journal(root_id, outcome, result="skipped", reason=REASON_CONTENTLESS,
                     trigger=trigger, board=board)
            return outcome

        chat_id, thread_id, source = resolve_owner_tg_lane(conn, root_id, board=board)
        outcome.chat_source = source
        outcome._raw_chat_id = str(chat_id or "")
        outcome.chat_id_masked = _mask(chat_id)
        if not chat_id:
            outcome.reason = REASON_NO_OWNER_LANE
            _journal(root_id, outcome, result="failed", reason=REASON_NO_OWNER_LANE,
                     trigger=trigger, board=board)
            return outcome

        if dry_run:
            return outcome

        if dedup_key:
            from gateway.delivery_engine import dedup_mark, dedup_seen, ensure_dedup_table

            ensure_dedup_table(conn)
            if dedup_seen(conn, root_id, "final_report", dedup_key):
                outcome.already_delivered = True
                outcome.reason = REASON_ALREADY_DELIVERED
                return outcome

        if adapter is None and adapter_resolver is not None:
            try:
                adapter = adapter_resolver()
            except Exception as exc:
                logger.warning("final report: adapter resolver failed: %s", exc)
                adapter = None
        if adapter is None:
            outcome.reason = REASON_NO_ADAPTER
            _journal(root_id, outcome, result="failed", reason=REASON_NO_ADAPTER,
                     trigger=trigger, board=board)
            return outcome

        send_meta: dict[str, Any] = dict(metadata or {})
        if thread_id and not send_meta.get("thread_id"):
            send_meta["thread_id"] = thread_id

        # --- text ---------------------------------------------------------
        try:
            if send_text is not None:
                res = await send_text(adapter, chat_id, text)
            else:
                res = await adapter.send(chat_id, text, metadata=send_meta)
        except Exception as exc:
            # Isolated here on purpose: a Telegram exception must never escape
            # into the caller's terminal transition (t_2e53d211).
            logger.warning(
                "final report: text send to telegram:%s for %s raised: %s",
                _mask(chat_id), root_id, exc,
            )
            outcome.reason = str(exc) or exc.__class__.__name__
            _journal(root_id, outcome, result="failed", reason=outcome.reason,
                     trigger=trigger, board=board)
            return outcome

        if getattr(res, "success", True) is False:
            err = getattr(res, "error", None) or "unknown"
            logger.warning(
                "final report: text send to telegram:%s for %s failed: %s",
                _mask(chat_id), root_id, err,
            )
            outcome.reason = str(err)
            _journal(root_id, outcome, result="failed", reason=str(err),
                     trigger=trigger, board=board)
            return outcome

        # --- report files --------------------------------------------------
        if artifacts:
            try:
                if send_document is not None:
                    art_res = await send_document(
                        adapter=adapter, chat_id=chat_id, metadata=send_meta,
                        event_payload={"artifacts": artifacts}, task=None,
                    )
                else:
                    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

                    class _ArtifactLaneShim(GatewayKanbanWatchersMixin):
                        """Carries no gateway state — only the artifact helper.

                        The production lane is reused AS IS (25 MB cap, unsafe
                        path filter, content dedup, ``send_document``) rather
                        than re-implemented here; the helper is bound to a real
                        instance so a future ``self.<attr>`` use fails loudly
                        instead of silently breaking an unbound ``None`` call.
                        """

                    art_res = await _ArtifactLaneShim()._deliver_kanban_artifacts(
                        adapter=adapter,
                        chat_id=chat_id,
                        metadata=send_meta,
                        event_payload={"artifacts": artifacts},
                        task=None,
                    )
            except Exception as exc:
                logger.warning(
                    "final report: artifact send to telegram:%s for %s raised: %s",
                    _mask(chat_id), root_id, exc,
                )
                art_res = {"sent": [], "failed": list(artifacts), "error": str(exc)}
            outcome.artifacts_sent = list((art_res or {}).get("sent") or [])
            outcome.artifacts_failed = list((art_res or {}).get("failed") or [])
            outcome.artifacts_skipped = list((art_res or {}).get("skipped") or [])
            if not outcome.reason:
                outcome.reason = str((art_res or {}).get("error") or "")
            if not outcome.reason and outcome.artifacts_failed:
                # The production artifact lane catches a per-file upload error
                # inside its own loop (observable only as a WARNING + the path
                # landing in ``failed``), so the failure reason has to be
                # surfaced from that list here — otherwise the text-only
                # outcome would look fully successful in the audit trail.
                outcome.reason = "artifact upload failed: %s" % ", ".join(
                    os.path.basename(p) for p in outcome.artifacts_failed
                )

        outcome.delivered = True
        outcome.report_type = _report_type(outcome.artifacts_sent or artifacts)

        if dedup_key:
            try:
                from gateway.delivery_engine import dedup_mark

                dedup_mark(conn, root_id, "final_report", dedup_key)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("final report: dedup mark failed: %s", exc)

        _journal(root_id, outcome, result="delivered", reason=outcome.reason,
                 trigger=trigger, board=board)
        return outcome
    except Exception as exc:  # pragma: no cover - last-resort isolation
        logger.warning("final report: delivery for %s failed: %s", root_id, exc)
        outcome.delivered = False
        outcome.reason = str(exc) or exc.__class__.__name__
        return outcome
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _journal(
    root_id: str,
    outcome: FinalReportOutcome,
    *,
    result: str,
    reason: str = "",
    trigger: str = "manual_call",
    board: Optional[str] = None,
) -> None:
    """One structured audit record per attempt (telegram-report-delivery.log)."""
    try:
        from gateway.delivery_engine import log_report_delivery

        log_report_delivery(
            task_id=root_id,
            chat_id=outcome._raw_chat_id,
            result=result,
            reason=reason,
            report_type=outcome.report_type,
            artifacts=outcome.artifacts_sent,
            artifacts_failed=outcome.artifacts_failed,
            artifacts_skipped=outcome.artifacts_skipped,
            text_len=len(outcome.text or ""),
            trigger=trigger,
            board=board or "",
            # ``artifacts_discovered`` means «найдено на диске, но НЕ ушло» in
            # the journal contract (a failed text send aborts the lane before
            # any upload).  Report it only when the delivery did not happen,
            # otherwise sent files would be counted as never uploaded.
            artifacts_discovered=(
                [] if result == "delivered" else outcome.artifacts
            ),
            extra={"chat_source": outcome.chat_source, "manual": False},
        )
    except Exception as exc:  # pragma: no cover - journal must never break a send
        logger.debug("final report: journal write failed: %s", exc)
