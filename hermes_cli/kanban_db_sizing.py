"""Card-sizing advisory on the ``todo -> ready`` promotion.

The sizing rule (``agent.kanban_card_sizing``) used to reach a creator only through
``kanban_create``'s tool response: a card made by ``hermes kanban create`` never saw it,
and the dispatcher never looked at a card's volume at all, so an oversized card entered
work silently. This module closes that hole at the one transition every card must pass:
promotion to ``ready``, whether the dispatcher's tick (``recompute_ready``) or an
operator's ``promote`` (``promote_task``) makes it.

The rule stays advisory: nothing here blocks a promotion or changes a status. One comment
carrying the estimate, the assignee's budget, the acceptance-item count and the remedy is
written, and only once per card (the tag below is the idempotency key) — a second trip
through ``ready`` must not repeat it.

The budget read is the *assignee's* (``agent.max_turns`` of the profile the dispatcher
will spawn), never the creator's, and an unreadable or unlimited budget means silence.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)
# First line of the advisory; also the idempotency key (a card carries at most one).
SIZING_COMMENT_TAG = "**card-sizing**"

_SIZING_COMMENT_SQL = (
    "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ? LIMIT 1"
)


def comment_body(estimate, assignee: str) -> Optional[str]:
    """The advisory text for an over-budget estimate, or None when the card fits.

    Delegates the fit judgement to ``sizing_warning`` so this path and the
    ``kanban_create`` tool can never disagree about what "over budget" means.
    """
    from agent.kanban_card_sizing import sizing_warning

    if sizing_warning(estimate, assignee=assignee) is None:
        return None
    return (
        f"{SIZING_COMMENT_TAG} карточка не влезает в бюджет профиля-исполнителя.\n"
        "\n"
        f"- исполнитель: `{assignee}`\n"
        f"- пунктов приёмки: {estimate.acceptance_items}\n"
        f"- ожидаемо итераций: {estimate.estimated_iterations}\n"
        f"- бюджет профиля (`agent.max_turns`): {estimate.budget}\n"
        f"- превышение: {estimate.overrun}\n"
        "\n"
        "Рекомендация: разделить карточку или вынести часть пунктов приёмки в отдельную\n"
        "карточку с `parents` = эта. Промоушен не заблокирован — правило предупреждающее,\n"
        "решение за оркестратором. Формула оценки: `40 + 30 × пунктов приёмки`."
    )


def promotion_note(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The advisory for ``task_id`` against its assignee's budget, or None.

    Reads the card row itself so callers do not have to thread body/assignee
    through the promotion code; a missing assignee, an unreadable profile or an
    unlimited budget all resolve to None (no warning rather than a guessed cap).
    """
    from agent.kanban_card_sizing import assignee_iteration_budget, estimate_card

    row = conn.execute(
        "SELECT assignee, body FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None or not row["assignee"]:
        return None
    assignee = str(row["assignee"])
    budget = assignee_iteration_budget(assignee)
    if budget is None:
        return None
    return comment_body(estimate_card(row["body"], budget), assignee)


def has_sizing_comment(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when this card already carries the advisory (idempotency check)."""
    return conn.execute(
        _SIZING_COMMENT_SQL, (task_id, SIZING_COMMENT_TAG + "%"),
    ).fetchone() is not None


def note_promotion(conn: sqlite3.Connection, task_id: str, *, author: str) -> bool:
    """Write the sizing advisory for a card just promoted to ``ready``; True if written.

    Advisory only: the promotion has already happened and never rolls back because
    of this. A card that fits, has no acceptance section, no assignee or no known
    budget gets nothing, and a second promotion of the same card gets nothing new.
    """
    try:
        if has_sizing_comment(conn, task_id):
            return False
        body = promotion_note(conn, task_id)
        if body is None:
            return False
        from hermes_cli.kanban_db import add_comment

        add_comment(conn, task_id, author, body)
        return True
    # Advisory bookkeeping must never veto a promotion: a broken estimate, an
    # unreadable profile config or a comment write failure is logged, not raised.
    except Exception:
        logger.exception("card-sizing advisory failed for %s", task_id)
        return False


def note_if_ready(
    conn: sqlite3.Connection, task_id: str, status: str, *, author: str,
) -> bool:
    """Guard for callers that create a card in an arbitrary status.

    A card born ``ready`` crosses no promotion, so the advisory — which hangs off
    promotion — would miss exactly the population it exists for. Any other
    status gets nothing: it will be advised when it is actually promoted.
    """
    if status != "ready":
        return False
    return note_promotion(conn, task_id, author=author)


__all__ = [
    "SIZING_COMMENT_TAG",
    "comment_body",
    "has_sizing_comment",
    "note_if_ready",
    "note_promotion",
    "promotion_note",
]
