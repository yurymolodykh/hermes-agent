"""Subject scope for a session the gateway authority holds (МПД Т5 / П3, П6).

``AuthorityConnection`` is the socket's only door to a stored conversation: ``session.list``,
``session.active_list``, ``session.resume``/``attach`` and every control verb pass through
``SessionAuthority.authorize``. That gate compared the row's owner with the caller for a ``LOCAL``
source only — a messaging lane (Telegram, Discord, Slack, ...) fell through, so with
``acl.multi_user`` on a second person's socket listed and mounted the first person's chats, and
``attach`` then streamed their live events.

The REST surface already answers this question in one place (``hermes_acl_scope.owner_stamps`` +
``row_outside_scope``). This module is the same comparison for the socket, so the two surfaces
cannot disagree about who owns a row.

With ``acl.multi_user`` off nothing changes: the host has one person and the gateway keeps its
single-user semantics (a native Desktop socket and the local operator surface still share local
sessions). That is the contract step 3 shipped and step 4 must not break.
"""
from __future__ import annotations

from typing import Any


def actor_stamps(actor: Any) -> list[str]:
    """The stored ``sessions.user_id`` stamps *actor* may hold, or ``[]`` when undeterminable.

    Empty means "matches nothing": a subject that cannot be established, or a bindings registry
    that cannot be validated, must not silently widen to the whole store (fail closed, exactly as
    the HTTP scope does).
    """
    from hermes_acl_scope import owner_stamps

    subject = str(getattr(actor, "subject", "") or "").strip()
    if not subject:
        return []
    try:
        return list(owner_stamps(subject))
    except Exception:  # health: allow BLE001 -- an unusable registry is a REFUSAL, never an exception on a read path
        import logging
        logging.getLogger(__name__).warning(
            "session subject scope: owner stamps unavailable for %r — refusing the row", subject,
            exc_info=True)
        return []


def session_stamp(authority: Any, session_id: str, source: Any) -> str:
    """The owner stamp that names *session_id*: the live source's user, else the stored row's.

    A live ``SessionSource`` carries the sending user for a mounted lane; a cold row (or a mount
    that rebuilt its source without one) only has ``sessions.user_id``. Reading both keeps a
    restored conversation under the same owner as the row the listing shows.
    """
    stamp = str(getattr(source, "user_id", "") or "").strip()
    if stamp:
        return stamp
    try:
        row = authority.db.get_session(session_id)
    except Exception:  # health: allow BLE001 -- an unreadable row is treated as unowned (fail closed)
        return ""
    return str((row or {}).get("user_id") or "").strip()


def foreign_session_in_multi_user(authority: Any, actor: Any, session_id: str, source: Any) -> bool:
    """Whether *actor* must be refused *session_id* under ``acl.multi_user``.

    ``False`` — the whole answer — while the flag is off, for the host owner, for a profile the
    actor does not belong to (that is ``authorize``'s own ``profile_mismatch``), and for a row
    inside the actor's own stamps. ``True`` for everything else, including a row no identity
    claims: an owner-scoped surface never discloses a transcript whose owner is unstated.
    """
    from gateway.owner_identity import is_host_owner
    from hermes_acl_identity import multi_user_enabled

    if not multi_user_enabled():
        return False
    if is_host_owner(getattr(actor, "subject", "")):
        # The host owner keeps the store it always had; ``is_host_operator`` is not consulted here so
        # an owner socket that was never granted ``session:operator`` still reads its own rows.
        return False
    from hermes_acl_scope import row_outside_scope

    stamp = session_stamp(authority, session_id, source)
    return row_outside_scope({"user_id": stamp}, actor_stamps(actor))
