"""Owner scope for session history: who may see which ``sessions.user_id`` row.

One switch, one rule, one place. ``acl.enabled`` (config.yaml, default ``false``) decides whether
session history is scoped to its owner at all:

* ``false`` — every read path keeps its exact previous behaviour: the listing, the HTTP API and
  ``session_search`` add no owner predicate, so a single-user installation (and every CLI / desktop
  / TUI surface of the host owner) is untouched.
* ``true`` — a read path resolves the subject of the CALLER and reduces it to the stored owner
  stamps that subject may hold (:func:`owner_stamps`); the store then filters rows by exact match
  against that set. A caller whose subject cannot be established gets an EMPTY scope, which the
  store turns into ``1 = 0`` (fail closed), never into "unrestricted".

Two caller surfaces, two sources of truth:

* HTTP (``/api/sessions*``) — the profile-scoped Bearer key authenticates the PROFILE, not a person
  inside it, so the identity must arrive from a channel the deployment declares trusted
  (``acl.identity_trust``). With the default ``untrusted`` the subject is never read: an unsigned
  header is a self-declaration, and honouring it would let anyone holding the shared key read
  anyone's history.
* The agent's own tools (``session_search``) — ``HERMES_SESSION_ACTOR_ID`` is who asked THIS turn.
  It is not ``HERMES_SESSION_USER_ID``: that one is the session ROUTE and is deliberately empty in
  a shared chat (Telegram observe mode). ``get_session_env`` re-reads the actor through the turn
  event, so a turn MERGED from two senders (``MessageEvent.absorb_turn_input``) reports no actor —
  and a merged turn has no single owner, so it sees no history.

Known limitation, stated rather than papered over: a group chat has no per-member history check.
The profile decides that a group chat is served (:func:`is_shared_chat`), and a member's own
sessions remain visible to the bot that serves the group; there is no channel-membership lookup
here (no live platform API in this path), so membership is NOT verified.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

ACL_SECTION = "acl"
#: Identity header the deployment may configure as trusted (``acl.identity_header``).
DEFAULT_IDENTITY_HEADER = "X-Hermes-User-Id"
#: ``acl.identity_trust`` values: only ``trusted-proxy`` admits a header identity.
TRUST_TRUSTED_PROXY = "trusted-proxy"


def _cfg(*keys: str, default: Any = None) -> Any:
    """``config.yaml`` value at *keys*, *default* on any failure (config is never fatal here)."""
    try:
        from hermes_cli.config import cfg_get, load_config
        return cfg_get(load_config(), *keys, default=default)
    except Exception:  # a broken config must not break a read path
        logger.debug("acl scope: config unavailable for %s", ".".join(keys), exc_info=True)
        return default


def acl_enabled() -> bool:
    """Whether session history is owner-scoped. Absent/false = the legacy unscoped behaviour."""
    return bool(_cfg(ACL_SECTION, "enabled", default=False))


def identity_trust() -> str:
    """How a header identity is trusted; only ``trusted-proxy`` (a verifying proxy in front) admits one."""
    return str(_cfg(ACL_SECTION, "identity_trust", default="untrusted") or "untrusted").strip().lower()


def identity_header() -> str:
    """Header carrying the caller's owner stamp when ``identity_trust`` declares one trusted."""
    return str(_cfg(ACL_SECTION, "identity_header", default=DEFAULT_IDENTITY_HEADER) or DEFAULT_IDENTITY_HEADER)


def owner_stamps(identity: Optional[str]) -> list[str]:
    """The stored ``sessions.user_id`` values that name *identity* — exact stamps, never prefixes.

    A gateway stores an owner as the sending user's platform id (``111`` for Telegram user 111,
    ``tg:111`` in a deployment that prefixes the transport) and, for a signed-in surface, the
    ``auth:v1:[provider, issuer, user]`` subject :func:`gateway.session_identity.authenticated_subject`
    mints. The same person therefore owns rows under more than one stamp, and the mapping is taken
    from the identity formats themselves rather than guessed:

    * the identity as given, and — in multi-user mode — the subject a *login* resolves to
      (``hermes_acl_identity.resolve_subject``: ``anna`` → ``person/anna``), so a proxy that names
      a person by their login scopes exactly as a dashboard sign-in does;
    * for an ``auth:v1:[provider, issuer, user]`` subject, the login label
      ``<provider>:<user>`` (what ``hermes serve`` stamps on a desktop row) and the bare ``user``
      (the platform id a messaging row keeps);
    * every stamp the bindings registry records for the SAME PERSON (``hermes_acl_bindings``):
      the owner's ``uid:<n>``, ``cron-owner`` and Telegram id live on their own record, which is
      what keeps the sessions created before step 3 visible to the person they belong to.

    Anything else contributes only itself. No wildcard, no prefix match: an unrecognised stamp
    simply does not match, which fails closed.
    """
    text = (identity or "").strip()
    if not text:
        return []
    stamps: list[str] = [text]
    stamps.extend(_identity_subject_stamps(text))
    try:
        from gateway.session_identity import parse_authenticated_subject
        parts = parse_authenticated_subject(text)
    except Exception:  # a non-gateway caller (CLI tool) has no session_identity
        parts = None
    if parts is not None:
        provider, _issuer, user = (part.strip() for part in parts)
        if provider and user:
            stamps.append(f"{provider}:{user}")
        if user:
            stamps.append(user)
    for stamp in list(stamps):
        stamps.extend(_binding_stamps(stamp))
    # A gateway stores a messaging row under the BARE platform id (``241213869``) while the
    # registry names the same person ``tg:241213869``; the two forms must scope the same rows or the
    # owner's own Telegram chats stay hidden from them. Deriving the bare id ONLY from a stamp the
    # registry itself recorded keeps this exact (no prefix match) and, just as importantly, leaves
    # an identity the registry does not name holding nothing but itself — an unknown stamp must not
    # conjure a second one from its own form.
    for stamp in list(stamps):
        for recorded in _binding_stamps(stamp):
            bare = _bare_platform_id(recorded)
            if bare:
                stamps.append(bare)
    seen: list[str] = []
    for stamp in stamps:
        if stamp and stamp not in seen:
            seen.append(stamp)
    return seen


def _bare_platform_id(stamp: str) -> str:
    """The bare id a ``tg:<id>`` stamp also owns (``tg:125805788`` → ``125805788``); ``''`` otherwise.

    Only the Telegram prefix is folded: it is the form this install's registry records
    (``telegram_subject``) while the gateway stamps the raw id on the row. A sticker id is emitted
    only when the remainder is entirely digits, so an ``auth:v1:...`` or ``uid:1001`` stamp never
    contributes a second, unrelated stamp.
    """
    text = (stamp or "").strip()
    prefix, _, rest = text.partition(":")
    if prefix.strip().lower() != "tg":
        return ""
    candidate = rest.strip()
    return candidate if candidate.isdigit() else ""


def _identity_subject_stamps(identity: str) -> list[str]:
    """The stamps a multi-user login resolves to (``anna`` → ``person/anna``); ``[]`` otherwise.

    Only consulted while ``acl.multi_user`` is on: with the flag off a login string is opaque, and
    turning it into a subject would change a scope nobody asked to change.
    """
    try:
        from hermes_acl_identity import multi_user_enabled, resolve_subject

        if not multi_user_enabled():
            return []
        subject = resolve_subject(identity, fallback="")
    except Exception:  # health: allow BLE001 -- a stripped build has no identity module; the
        # legacy stamp list is then the whole answer, which is the pre-step-3 behaviour.
        logger.debug("acl scope: login→subject resolution unavailable", exc_info=True)
        return []
    return [subject] if subject and subject != identity else []


def _binding_stamps(identity: str) -> list[str]:
    """Every stamp of *identity*'s person per the bindings registry.

    Propagates :class:`hermes_acl_bindings.BindingsError` while ``acl.enabled`` so the caller denies
    (see :func:`request_owner_scope`): a registry that cannot be validated must stop the rights
    check, not shrink the answer to one stamp and quietly hide the owner's history. With ACL off a
    broken file changes nothing, so the registry reports absence instead.
    """
    try:
        from hermes_acl_bindings import person_stamps
        return list(person_stamps(identity))
    except ImportError:  # a stripped-down build without the registry module
        logger.debug("acl scope: hermes_acl_bindings unavailable", exc_info=True)
        return []


def merged_scope(stamps: Sequence[str]) -> list[str]:
    """Dedupe an ordered owner-stamp collection, dropping empties (order preserved)."""
    out: list[str] = []
    for stamp in stamps:
        text = (stamp or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def caller_subject() -> str:
    """The subject of the running turn: ``HERMES_SESSION_ACTOR_ID`` (who asked), never the route id.

    ``get_session_env`` is the one reader that also consults the turn event, so a turn into which a
    second sender's input was folded reports an EMPTY actor here — exactly the case that must see
    nothing rather than "the first sender's" history.
    """
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env("HERMES_SESSION_ACTOR_ID", "") or "").strip()
    except Exception:  # CLI / script context without the gateway modules
        import os
        return str(os.getenv("HERMES_SESSION_ACTOR_ID", "") or "").strip()


def caller_owner_scope() -> Optional[list[str]]:
    """Owner scope for a tool read: ``None`` when unscoped, else the stamps (possibly empty).

    ``None`` (no filter at all) only while ``acl.enabled`` is false. With it on, the subject must be
    established: an empty subject yields ``[]``, which the store renders as ``1 = 0``. A bindings
    registry that cannot be validated also yields ``[]`` — the rights check refuses instead of
    falling back to the narrower stamp set.
    """
    if not acl_enabled():
        return None
    try:
        return owner_stamps(caller_subject())
    except _bindings_errors() as exc:
        logger.warning("acl scope: bindings registry unusable — DENYING session history (%s)", exc)
        return []


def request_owner_scope(request: Any) -> Optional[list[str]]:
    """Owner scope for one HTTP request: ``None`` when unscoped, else stamps (possibly empty).

    The caller is resolved from the STRONGEST identity the request already carries, in this order:

    * a verified native grant (``request.state.native_http_principal``) — the Desktop's own
      private one-use ticket;
    * a verified dashboard login (``request.state.session``) — the cookie/bearer session the
      auth gate attached, whose subject is the person who signed in;
    * ``acl.identity_trust``'s identity header — read ONLY when a trusted proxy is declared,
      because the profile key alone authenticates a profile and an unsigned header under a shared
      key is a self-declaration.

    Under the default trust level with no verified identity this returns ``[]`` (fail closed) as
    soon as scoping is on, which is deliberate: an installation that enables ``acl.enabled``
    without declaring how identities are trusted must not serve anyone's history, and must say so.
    A broken bindings registry is the same class of failure and returns ``[]`` as well.
    """
    if not acl_enabled():
        return None
    subject = request_identity_subject(request)
    if subject is not None:
        try:
            return owner_stamps(subject)
        except _bindings_errors() as exc:
            logger.warning("acl scope: bindings registry unusable — DENYING this request (%s)", exc)
            return []
    if identity_trust() != TRUST_TRUSTED_PROXY:
        logger.warning(
            "acl.enabled is true but acl.identity_trust is %r and the request carries no verified "
            "dashboard identity; refusing it (session history stays closed). Configure "
            "acl.identity_trust=trusted-proxy behind a proxy that sets %s.",
            identity_trust(), identity_header())
        return []
    headers = getattr(request, "headers", None)
    value = headers.get(identity_header(), "") if headers is not None else ""
    try:
        return owner_stamps(value)
    except _bindings_errors() as exc:
        logger.warning("acl scope: bindings registry unusable — DENYING this request (%s)", exc)
        return []


def request_identity_subject(request: Any) -> Optional[str]:
    """The verified subject a request acts as, or ``None`` when it carries no verified identity.

    Three stitched identities are compared: the native HTTP grant and the dashboard login, whose
    subjects must AGREE when both are present (a mismatch is not "one of them is right") — an
    ambiguous request is refused with ``""``, the fail-closed sentinel every scope resolver turns
    into "no rows". No header is read here: this is the verified half of
    :func:`request_owner_scope`.
    """
    state = getattr(request, "state", None)
    native = getattr(state, "native_http_principal", None)
    session = getattr(state, "session", None)
    subjects: list[str] = []
    if native is not None:
        subject = str((native or {}).get("subject") or "").strip()
        subjects.append(subject)
    if session is not None:
        from gateway.owner_identity import principal_subject
        subjects.append(str(principal_subject({
            "user_id": getattr(session, "user_id", ""),
            "provider": getattr(session, "provider", ""),
            "issuer": getattr(session, "issuer", "")}) or "").strip())
    if not subjects:
        return None
    first = subjects[0]
    if any(other != first for other in subjects[1:]):
        logger.warning("acl scope: native grant and dashboard login name different subjects — DENYING")
        return ""
    return first


def request_is_host_owner(request: Any) -> bool:
    """Whether the request's verified identity is the host owner ([] free access to nothing else)."""
    subject = request_identity_subject(request)
    if not subject:
        return False
    from gateway.owner_identity import is_host_owner
    return is_host_owner(subject)



def _bindings_errors() -> tuple[type[BaseException], ...]:
    """The exception tuple the registry raises for a file it cannot trust.

    Resolved at call time so a build without ``hermes_acl_bindings`` still imports this module; an
    always-match fallback keeps the ``except`` clause valid but never fires for that build.
    """
    try:
        from hermes_acl_bindings import BindingsError
        return (BindingsError,)
    except ImportError:
        return (_NeverRaised,)


class _NeverRaised(Exception):
    """Placeholder so the ``except`` clause stays valid when the registry module is absent."""


def row_outside_scope(session: Any, scope: Sequence[str]) -> bool:
    """Whether a stored session row falls OUTSIDE an owner scope (``True`` = must not be served).

    Exact match against the row's ``user_id`` stamp — the same comparison the SQL listing makes
    (``COALESCE(s.user_id, '') IN (...)``), so the open/delete path and the listing cannot disagree.
    A row carrying NO owner stamp is outside every scope: an owner-scoped surface refuses to
    disclose a transcript nobody's identity claims.
    """
    stored = ""
    try:
        stored = str((session or {}).get("user_id") or "")
    except Exception:  # a non-mapping row is outside any scope
        return True
    return not (stored and stored in set(scope))


def is_shared_chat(chat_type: Optional[str]) -> bool:
    """Whether *chat_type* names a chat several people share (group / forum / channel).

    A membership check does not exist on this path (see the module docstring): this predicate says
    only that the transport delivers a shared chat, never that a given person belongs to it.
    """
    return str(chat_type or "").strip().lower() in {"group", "forum", "channel", "supergroup"}


def describe_scope(scope: Optional[list[str]]) -> str:
    """Short human/audit form of a scope for logs (never a transcript)."""
    if scope is None:
        return "unscoped(acl.disabled)"
    return "scope=" + json.dumps(scope, ensure_ascii=False)
