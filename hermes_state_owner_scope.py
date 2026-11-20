"""Owner-scope SQL arms shared by the session and message stores (МПД Т5 / §2.2 П6).

Two counters grew a ``user_id`` parameter when the dashboard's session surface became
owner-scoped: ``SessionDB.message_count`` (a store-wide message total per person) and
``SessionDB.count_empty_sessions`` / ``delete_empty_sessions`` (the "delete empty" sweep). Both
need the same two rules, and stating them twice is how the two arms drift apart:

* ``None`` means "unscoped" — the legacy single-user call, which must stay byte-identical;
* an EMPTY scope is the fail-closed "identity not established" case and must match NOTHING,
  never everything. ``1 = 0`` states that, where ``IN ()`` would be a syntax error and a
  skipped arm would be a leak.

The stamp column is ``sessions.user_id``; comparison uses ``COALESCE(user_id, '')`` so an unowned
legacy row is matched by an explicit empty stamp rather than silently escaping the filter.
"""
from __future__ import annotations

from typing import Any, Sequence

#: Matches no row. The fail-closed sentinel for "a scope was asked for and nobody is in it".
NOTHING = "1 = 0"


def owner_values(user_id: str | Sequence[str] | None) -> list[str] | None:
    """``None`` for an unscoped call, else the non-empty stamps (possibly an empty list)."""
    if user_id is None:
        return None
    if isinstance(user_id, str):
        return [user_id] if user_id else []
    return [str(owner) for owner in user_id if owner]


def owner_arm(column: str, user_id: str | Sequence[str] | None) -> tuple[str, list[Any]]:
    """A ``(sql, params)`` AND-arm for *column*; ``("", [])`` when the caller is unscoped."""
    owners = owner_values(user_id)
    if owners is None:
        return "", []
    if not owners:
        return NOTHING, []
    placeholders = ", ".join("?" for _ in owners)
    return f"COALESCE({column}, '') IN ({placeholders})", list(owners)


def scoped_where(base: str, column: str, user_id: str | Sequence[str] | None) -> tuple[str, list[Any]]:
    """*base* WHERE clause narrowed by the owner arm (``base`` is assumed already non-empty)."""
    arm, params = owner_arm(column, user_id)
    if not arm:
        return base, params
    return f"{base} AND {arm}", params


def message_count_sql(session_id: str | None, user_id: str | Sequence[str] | None) -> tuple[str, list[Any]]:
    """The counting statement for ``SessionDB.message_count`` after owner scoping.

    A scoped count joins ``sessions`` so the owner stamp of the row's session decides — the same
    column the listing filters on, so a count can never disagree with the rows it counts.
    """
    arm, params = owner_arm("s.user_id", user_id)
    if not arm:
        sql = "SELECT COUNT(*) FROM messages" + (" WHERE session_id = ?" if session_id else "")
        return sql, ([session_id] if session_id else [])
    sql = "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id"
    where = NOTHING if arm == NOTHING else arm
    params = list(params)
    if session_id:
        where += " AND m.session_id = ?"
        params.append(session_id)
    return f"{sql} WHERE {where}", params
