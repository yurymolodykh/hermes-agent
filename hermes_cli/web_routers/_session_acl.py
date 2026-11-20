"""Owner scope for the session REST surface (МПД Т5 / П6).

The dashboard's session routes were written for a single-user host: the profile API key and the
cookie login authenticate a PROFILE, so ``GET /api/sessions`` and every ``/api/sessions/{id}``
sibling listed and opened every dialog in it. The store already knows the owner of a row
(``sessions.user_id``, plus ``_owner_scope_clause`` from step 0), so the fix is not a new
mechanism but ONE seam every read path goes through:

* :func:`session_scope` — the caller's owner stamps, resolved from the VERIFIED identity the
  request carries (``hermes_acl_scope.request_owner_scope``): ``None`` while ``acl.enabled`` is
  false, then every route keeps its exact previous behaviour; an empty list is the fail-closed
  "identity not established" case and matches nothing.
* :func:`visible_row` / :func:`require_row` — the decision for a row already fetched by id. A
  foreign row answers **404**, never 403: a 403 confirms the id exists and turns the surface into
  an id oracle. The refusal is indistinguishable from a genuine miss, exactly as
  ``gateway/platforms/api_server.py`` does for the same class.
* :func:`scoped_ids` — the filter for bulk selections (delete/prune/import): ids outside the
  scope are dropped rather than reported, so a bulk request cannot count its way into learning
  which ids exist.

Nothing here decides ownership by itself; it only routes the store's own comparison
(``hermes_acl_scope.row_outside_scope``) and the SQL arm (``_owner_scope_clause``) from one place.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from fastapi import HTTPException, Request

NOT_FOUND = "Session not found"


def session_scope(request: Optional[Request]) -> Optional[list[str]]:
    """The caller's owner stamps for this request; ``None`` = unscoped (``acl.enabled`` false)."""
    if request is None:  # a route called without a request (unit tests, internal callers)
        from hermes_acl_scope import acl_enabled
        return None if not acl_enabled() else []
    from hermes_acl_scope import request_owner_scope
    return request_owner_scope(request)


def row_outside(row: Any, scope: Optional[Sequence[str]]) -> bool:
    """Whether a stored session row is OUTSIDE *scope* (``False`` for an unscoped caller)."""
    if scope is None:
        return False
    from hermes_acl_scope import row_outside_scope
    return row_outside_scope(row, scope)


def require_row(row: Any, scope: Optional[Sequence[str]]) -> Any:
    """Return *row* when the caller may see it, else raise the same 404 a missing id gives."""
    if row is None or row_outside(row, scope):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return row


def visible_row(row: Any, scope: Optional[Sequence[str]]) -> bool:
    """``True`` when *row* exists and is inside *scope*."""
    return row is not None and not row_outside(row, scope)


def scoped_ids(ids: Iterable[str], db, scope: Optional[Sequence[str]]) -> list[str]:
    """Keep only the *ids* whose stored row is inside *scope* (unscoped callers keep all)."""
    wanted = [str(value) for value in ids if value]
    if scope is None:
        return wanted
    kept: list[str] = []
    for session_id in wanted:
        if visible_row(db.get_session(session_id), scope):
            kept.append(session_id)
    return kept


def refuse_writes(request: Request) -> None:
    """A NON-owner may not run a store-wide destructive session route.

    ``POST /api/sessions/prune``, ``DELETE /api/sessions/empty``, ``POST /api/sessions/bulk-delete``
    and ``POST /api/sessions/owner-backfill`` act on rows of every owner by construction. Their
    per-row filter is a weaker promise than their wording, so the store-wide ones stop at the
    owner; a non-owner simply does not get that affordance (the sidebar's per-session delete,
    which IS owner-scoped, stays). ``403`` here is right: nothing about a session id leaks.
    """
    from hermes_acl_scope import acl_enabled, request_is_host_owner
    if not acl_enabled():
        return
    if request_is_host_owner(request):
        return
    raise HTTPException(status_code=403, detail="owner_only")
