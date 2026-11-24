"""Who a finished card's artifact goes TO, and the journal that says so out loud.

The notifier's artifact path used to be silent in both directions: a file was handed to
whatever ``chat_id`` the subscription named with no relation check to the card, and a
delivery that did not happen left no trace anywhere a human would look. That was harmless
only while every subscription pointed at the orchestrator profile; the moment an employee's
own chat is subscribed (``hermes_cli.kanban_customer``), "nothing arrived" becomes
indistinguishable from "refused on purpose".

This module is the one writer of ``<HERMES_HOME>/logs/notify-delivery.jsonl``: one row per
artifact handoff, carrying the ``task_id``, the recipient, the verdict and a reason. A
refusal is a ROW, never silence.

The relation check itself is :func:`hermes_acl_results.card_verdict` — the same stamps and
the same ACL switch the dashboard's own card access uses. It does NOT close the file by
default: the owner's decision is that a card's materials belong to the team, so a recipient
outside the card's row is delivered to and journalled as ``reason: materials-shared``.
``kanban.shared_card_materials: false`` turns that into a real refusal, journalled with the
reason that caused it — the switch exists so the check can be shown to have both arms,
not because the shipped answer is "hide the file".
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_JOURNAL_NAME = "notify-delivery.jsonl"

#: Verdicts the delivery path understands. ``deliver`` is a handoff that happened;
#: ``refused`` is one that did not, and always carries the reason it did not.
VERDICT_DELIVERED = "delivered"
VERDICT_REFUSED = "refused"


def journal_path() -> Path:
    """``<HERMES_HOME>/logs/notify-delivery.jsonl`` (read at call time, profile-aware)."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / _JOURNAL_NAME


def record_delivery(
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    verdict: str,
    reason: str,
    subject: str = "",
    event_kind: str = "",
    artifacts: int = 0,
    path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Append one jsonl row to the delivery journal; never raises.

    Returns whether the row landed — a journal failure is a logging problem and must never
    turn into a delivery problem, exactly like the owner policy's suppression journal.
    """
    row: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())),
        "task_id": str(task_id or ""),
        "platform": str(platform or ""),
        "chat_id": str(chat_id or ""),
        "subject": str(subject or ""),
        "event_kind": str(event_kind or ""),
        "verdict": str(verdict or ""),
        "reason": str(reason or ""),
        "artifacts": int(artifacts or 0),
    }
    try:
        target = Path(path) if path is not None else journal_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:  # journal unavailable is a logging problem, not a delivery one
        logger.debug("kanban delivery: cannot append journal: %s", exc)
        return False


def materials_shared() -> bool:
    """``kanban.shared_card_materials`` (default True): card materials belong to the team.

    Read per delivery, not at import, so flipping it is a live change. An unreadable config
    keeps the shipped answer — the shared reading is the owner's standing decision, and a
    broken config must not quietly start hiding files.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        value = cfg_get(load_config(), "kanban", "shared_card_materials", default=True)
    except Exception:  # health: allow BLE001 -- an unreadable flag keeps the shipped answer
        logger.debug("kanban delivery: shared_card_materials unreadable", exc_info=True)
        return True
    return bool(value) if isinstance(value, bool) else True


def verdict_for(subject: Any, task: Any) -> tuple[str, str]:
    """``(deliver-or-refuse, reason)`` for handing *task*'s artifacts to *subject*.

    The relation check is the ACL one (:func:`hermes_acl_results.card_verdict`); the shared
    materials switch decides what a mismatch MEANS. Every branch has a reason string, because
    the point of this function is that no handoff is unexplained.
    """
    from hermes_acl_results import card_verdict

    text = str(subject or "").strip()
    relation = card_verdict(text, task)
    if relation == "unscoped":
        return VERDICT_DELIVERED, "acl-off; no relation check"
    if relation == "owner":
        return VERDICT_DELIVERED, "recipient-is-host-owner"
    if relation == "member":
        return VERDICT_DELIVERED, "recipient-on-card"
    if not text:
        return VERDICT_DELIVERED, "recipient-subject-unknown; materials-shared"
    if materials_shared():
        return VERDICT_DELIVERED, "recipient-not-on-card; materials-shared"
    return VERDICT_REFUSED, "recipient-not-on-card; materials-not-shared"
