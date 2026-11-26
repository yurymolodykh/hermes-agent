"""Which profiles a signed-in person may SEE, USE and CREATE (МПД Т2/Т3/Т4, шаг 5).

The dashboard's profile surface was written for a host with one person: ``GET /api/profiles``
listed every profile directory, ``POST /api/profiles`` created any name, and every
``?profile=`` fan-out (sessions, sidebar, projects tree, pull requests) plus every
``/api/profiles/{name}/…`` route answered for *any* name. So one login could enumerate the whole
roster, create a profile inside somebody else's namespace, and read another person's
orchestrator state.

This module is the ONE policy the router asks, in the same spirit as ``hermes_acl_scope`` for
session rows:

* **visible** — :func:`visible_profiles`: the person's own profiles (their registry record) plus
  the SHARED profiles (registry record ``shared/<name>``). The host OWNER keeps the whole roster,
  unchanged: step 2 bound the owner's legacy stamps to one person and this step must not take the
  admin view away from them.
* **usable** — :func:`allows`: the same set, applied to a single profile name (a ``?profile=``
  selector, a ``/api/profiles/{name}/…`` path).
* **creatable** — :func:`create_allowed`: off by default (the owner's §6 answer is "profiles are
  made by the admin"); with ``acl.profile_self_service: true`` a person may create exactly their
  own orchestrator root ``01-<login>`` and nothing else.

**The switch.** ``acl.multi_user`` — the same flag step 3 uses, because a profile grant is a grant
to a PERSON and there are no persons without it. With the flag off every function here answers
"unchanged", so a single-user installation keeps the exact behaviour it had (the dashboard's
Desktop/TUI surfaces read the roster as before). ``acl.enabled`` stays what it is in step 4: the
session-history switch. A profile nobody may see yet still holds rows whose owner stamp step 4
compares — the two layers are independent on purpose.

**Fail-closed.** A registry that is absent, or present but unreadable/unparseable, grants a
non-owner NOTHING (an empty visible set), never "everything": a broken file must not widen access.
The owner is exempt because :func:`gateway.owner_identity.is_host_owner` answers from the host
identity alone, and a broken registry must not lock the operator out of their own dashboard.

**Personal data in shared profiles** is not enforced here: a shared profile is a *runtime*, not a
container of one person's rows. What keeps personal rows apart inside it is step 4's
``sessions.user_id`` scope — a shared profile's sessions stay visible only to the person whose
stamp they carry. This module only decides which profiles are reachable; it does not, and cannot,
inspect what a shared profile's files contain.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

#: ``acl.profile_self_service`` — may a non-owner create their OWN orchestrator root? Default
#: false: the owner's answer to §6.2 is "the admin makes profiles".
SELF_SERVICE_KEY = "profile_self_service"

#: A person's own orchestrator root: ``01-<login>``.
PERSONAL_ROOT_PREFIX = "01-"

#: The profile-numbering convention of the mission (Т4): 02…15 are shared runtimes. Only used by
#: :func:`shared_candidates` so an operator can populate the registry in one command; the registry
#: remains the authority for what is actually shared.
_SHARED_NUMBER_RANGE = range(2, 16)


def _cfg(*keys: str, default: Any = None) -> Any:
    try:
        from hermes_cli.config import cfg_get, load_config

        return cfg_get(load_config(), *keys, default=default)
    except Exception:  # a broken config must not make the policy unanswerable
        logger.debug("acl profiles: config unavailable for %s", ".".join(keys), exc_info=True)
        return default


def multi_user_enabled() -> bool:
    """Whether profile visibility is scoped at all (``acl.multi_user``, default false)."""
    try:
        from hermes_acl_identity import multi_user_enabled as _on

        return bool(_on())
    except Exception:  # an import failure must never ENABLE the scoping
        logger.debug("acl profiles: multi_user flag unreadable", exc_info=True)
        return False


def self_service_enabled() -> bool:
    """Whether a person may create their own ``01-<login>`` root (``acl.profile_self_service``)."""
    return bool(_cfg("acl", SELF_SERVICE_KEY, default=False))


# ────────────────────────────────────────────────────────────────────────────
# The registry, read the same way every other step reads it
# ────────────────────────────────────────────────────────────────────────────


def _load_registry():
    """The bindings registry, or ``None`` when absent. Raises ``BindingsError`` when broken."""
    import hermes_acl_bindings as hb

    return hb.load_cached()


def _bindings_errors() -> tuple[type[BaseException], ...]:
    """The exception tuple the registry raises (resolved lazily: the module may be absent)."""
    try:
        from hermes_acl_bindings import BindingsError

        return (BindingsError,)
    except ImportError:  # pragma: no cover - the registry ships with this tree
        return (_NeverRaised,)


class _NeverRaised(Exception):
    """Placeholder so the ``except`` clause stays valid in a build without the registry."""


def is_owner(subject: Any) -> bool:
    """Whether *subject* is THIS host's owner (who keeps the full roster)."""
    if not subject:
        return False
    from gateway.owner_identity import is_host_owner

    return bool(is_host_owner(subject))


def login_of(subject: Any) -> str:
    """``person/<login>`` → ``<login>``; anything else → ``""`` (no personal namespace)."""
    text = str(subject or "").strip()
    if not text.startswith("person/"):
        return ""
    return text[len("person/"):]


def personal_root(login: Any) -> str:
    """The orchestrator root a login owns: ``01-<login>`` (``""`` when the login is unusable)."""
    slug = str(login or "").strip()
    if not slug:
        return ""
    from hermes_acl_identity import person_slug

    slug = person_slug(slug)
    if not slug:
        return ""
    return f"{PERSONAL_ROOT_PREFIX}{login_of(slug)}"


def root_login(profile: Any) -> str:
    """The login an ``01-<login>`` name claims, else ``""``."""
    text = str(profile or "").strip()
    if not text.startswith(PERSONAL_ROOT_PREFIX):
        return ""
    return text[len(PERSONAL_ROOT_PREFIX):]


# ────────────────────────────────────────────────────────────────────────────
# What a subject may see
# ────────────────────────────────────────────────────────────────────────────


def registry_slug(subject: Any) -> str:
    """The registry record slug *subject* acts as — the key ``person_of``/``allows`` are written for.

    A verified dashboard login reaches the policy as the SUBJECT ``gateway.session_identity`` mints
    (``auth:v1:[provider, issuer, login]``), while the bindings registry keys a person by its record
    slug (``person/<login>``) and by the stamps it records (``tg:<id>``, ``uid:<n>``). ``person_of``
    is exact-match on purpose — a prefix match would let ``person/yury2`` claim ``person/yury``'s
    profiles — so it answers ``None`` for the minted form and every profile check would then refuse
    the person their own profile (the terminal dial and every ``?profile=`` REST route alike).

    The expansion is the ACL's own (``hermes_acl_scope.owner_stamps``): it already derives the
    login label, the bare platform id and every stamp the SAME person's record holds, and
    :func:`gateway.owner_identity.is_host_owner` remains the separate owner answer. Only stamps the
    registry itself names are folded, so an unknown identity still resolves to ``""`` (fail closed)
    and no second person is ever reached.

    Returns ``""`` when *subject* names nobody in the registry.
    """
    text = str(subject or "").strip()
    if not text:
        return ""
    try:
        registry = _load_registry()
    except _bindings_errors() as exc:
        logger.warning("acl profiles: registry unusable — resolving no person (%s)", exc)
        return ""
    if registry is None:
        return ""
    slug = registry.person_of(text)
    if slug:
        return slug
    # ``person_of`` is exact: ask the ACL's own expansion which stamps this principal also holds.
    try:
        from hermes_acl_scope import owner_stamps

        stamps = owner_stamps(text)
    except Exception:  # health: allow BLE001 -- an unexpanded stamp must not widen access
        logger.debug("acl profiles: owner-stamp expansion unavailable for %r", text, exc_info=True)
        return ""
    for stamp in stamps:
        slug = registry.person_of(stamp)
        if slug:
            return slug
    return ""


def _personal_profiles_for(registry: Any, subject: Any) -> tuple[str, ...]:
    """The personal profiles *subject*'s OWN record holds (``()`` when it names nobody)."""
    slug = registry_slug(subject)
    return tuple(registry.personal_profiles_of(slug)) if slug else ()


def visible_profiles(subject: Any, names: Iterable[str]) -> tuple[str, ...]:
    """The profiles *subject* may see, out of *names*, in the order given.

    * scoping OFF (``acl.multi_user`` false) — *names* unchanged: the legacy single-user roster;
    * the host OWNER — *names* unchanged: the admin view is not narrowed by this step;
    * anybody else — the profiles their registry record holds plus the SHARED ones. An absent
      registry grants the personal set only (which an absent registry says is empty), and a
      broken one grants nothing at all: fail closed, never "unknown, so everything".
    """
    wanted = [str(name) for name in names if name]
    if not multi_user_enabled():
        return tuple(wanted)
    if is_owner(subject):
        return tuple(wanted)
    try:
        registry = _load_registry()
    except _bindings_errors() as exc:
        logger.warning("acl profiles: registry unusable — no profile is visible (%s)", exc)
        return ()
    if registry is None:
        return ()
    allowed = set(_personal_profiles_for(registry, subject)) | set(registry.shared_profiles())
    return tuple(name for name in wanted if name in allowed)


def allows(subject: Any, profile: Any) -> bool:
    """Whether *subject* may use *profile* (see :func:`visible_profiles` for the rule)."""
    name = str(profile or "").strip()
    if not name:
        return False
    if not multi_user_enabled() or is_owner(subject):
        return True
    return name in visible_profiles(subject, (name,))


def writable_profiles(subject: Any) -> Optional[tuple[str, ...]]:
    """Profiles *subject* may MODIFY (``None`` = unscoped: the legacy answer is "all").

    A shared profile is a runtime several people use, so using it never means reconfiguring it:
    its SOUL/description/model change only for the host owner. A non-owner may write exactly the
    profiles of their OWN registry record.
    """
    if not multi_user_enabled() or is_owner(subject):
        return None
    try:
        registry = _load_registry()
    except _bindings_errors() as exc:
        logger.warning("acl profiles: registry unusable — nothing is writable (%s)", exc)
        return ()
    if registry is None:
        return ()
    return _personal_profiles_for(registry, subject)


def request_may_write(request: Any, profile: Any) -> bool:
    """Whether the request's identity may reconfigure *profile*."""
    if not multi_user_enabled():
        return True
    name = str(profile or "").strip()
    if not name:
        return False
    allowed = writable_profiles(request_subject(request))
    return True if allowed is None else name in allowed


def require_writable(request: Any, profile: Any) -> None:
    """Refuse reconfiguring a profile the caller may only USE.

    Two different refusals, because the caller may already know the profile exists:

    * not visible at all — **404**, exactly like a bad name (a 403 would confirm the roster);
    * visible but SHARED — **403**: the caller uses it, so pretending it does not exist would be a
      lie, and what is refused is the write, not the profile.
    """
    if request_may_write(request, profile):
        return
    from fastapi import HTTPException

    logger.info("acl profiles: refusing to modify %r for this request", str(profile))
    if not request_allows(request, profile):
        raise HTTPException(status_code=404, detail=f"Profile '{profile}' does not exist.")
    raise HTTPException(status_code=403, detail="profile_read_only")


def shared_candidates(names: Iterable[str]) -> tuple[str, ...]:
    """Profiles matching the mission's shared numbering (``02-…`` … ``15-…``), in order.

    Only a convenience for ``hermes auth bindings share --defaults``: what is shared is what the
    registry's ``shared/`` record names, never what a directory happens to be called.
    """
    out: list[str] = []
    for name in names:
        text = str(name or "").strip()
        head, _, _rest = text.partition("-")
        if not head.isdigit():
            continue
        if int(head) in _SHARED_NUMBER_RANGE and text not in out:
            out.append(text)
    return tuple(out)


# ────────────────────────────────────────────────────────────────────────────
# HTTP request surface (the only consumer of the policy)
# ────────────────────────────────────────────────────────────────────────────


def request_subject(request: Any) -> Optional[str]:
    """The verified subject a request acts as (``None`` when it carries no verified identity)."""
    if request is None:
        return None
    from hermes_acl_scope import request_identity_subject

    subject = request_identity_subject(request)
    return subject or None


def scope_for_names(subject: Any, names: Iterable[str]) -> Optional[tuple[str, ...]]:
    """The visible subset of *names* for a SUBJECT; ``None`` means "unscoped, all of them"."""
    if not multi_user_enabled():
        return None
    return visible_profiles(subject, names)


def scope_for(request: Any, names: Iterable[str]) -> Optional[tuple[str, ...]]:
    """The visible subset of *names* for this request; ``None`` means "unscoped, all of them"."""
    if not multi_user_enabled():
        return None
    return visible_profiles(request_subject(request), names)


def request_allows(request: Any, profile: Any) -> bool:
    """Whether the request's identity may use *profile*."""
    if not multi_user_enabled():
        return True
    name = str(profile or "").strip()
    if not name:
        return False
    subject = request_subject(request)
    if is_owner(subject):
        return True
    return allows(subject, name)


def request_is_owner(request: Any) -> bool:
    """Whether the request's verified identity is the host owner."""
    return is_owner(request_subject(request))


def require_visible(request: Any, profile: Any) -> None:
    """Refuse a request naming a profile it may not use, with the miss a bad name gets.

    **404**, not 403: the same refusal an unknown profile name produces. A 403 would confirm that
    ``01-bob`` exists, turning the roster into an enumeration oracle — the identical choice
    ``_session_acl.require_row`` makes for a foreign session id.
    """
    if request_allows(request, profile):
        return
    from fastapi import HTTPException

    logger.info("acl profiles: refusing %r for a request that may not use it", str(profile))
    raise HTTPException(status_code=404, detail=f"Profile '{profile}' does not exist.")


def require_owner(request: Any, *, action: str = "owner_only") -> None:
    """Refuse a host-wide profile mutation (import/rename/delete/active) to a non-owner."""
    if not multi_user_enabled() or request_is_owner(request):
        return
    from fastapi import HTTPException

    logger.info("acl profiles: refusing %s for a non-owner", action)
    raise HTTPException(status_code=403, detail=action)


def create_allowed(request: Any, name: Any) -> bool:
    """Whether this request may create the profile *name*.

    * scoping OFF, or the host owner — yes, exactly as before;
    * a non-owner — no by default (§6.2: the admin makes profiles). With
      ``acl.profile_self_service: true`` they may create precisely their own orchestrator root
      ``01-<login>``, provided it is not already somebody else's, and nothing else.
    """
    profile = str(name or "").strip()
    if not multi_user_enabled() or request_is_owner(request):
        return True
    if not profile or not self_service_enabled():
        return False
    subject = request_subject(request)
    login = login_of(subject)
    if not login:
        return False
    root = personal_root(login)
    if not root or profile != root:
        return False
    # A name already bound to another person is never "free" to take.
    try:
        registry = _load_registry()
    except _bindings_errors() as exc:
        logger.warning("acl profiles: registry unusable — refusing creation (%s)", exc)
        return False
    if registry is not None:
        owner_slug = registry.profile_owner(profile)
        person = registry.person_of(registry_slug(subject))
        if owner_slug is not None and owner_slug != person:
            return False
    return True


def require_create(request: Any, name: Any) -> None:
    """Refuse a creation request outside the caller's set, with an explicit 403."""
    if create_allowed(request, name):
        return
    from fastapi import HTTPException

    logger.info("acl profiles: refusing creation of %r for this request", str(name))
    raise HTTPException(status_code=403, detail="profile_not_allowed")


def describe(subject: Any, names: Iterable[str]) -> str:
    """One-line audit form: what this subject may see, never a file path."""
    visible = visible_profiles(subject, names)
    if not multi_user_enabled():
        return "profiles: unscoped (acl off)"
    return f"subject={subject or '(none)'} visible={list(visible)}"


__all__ = [
    "PERSONAL_ROOT_PREFIX",
    "SELF_SERVICE_KEY",
    "allows",
    "create_allowed",
    "describe",
    "is_owner",
    "login_of",
    "multi_user_enabled",
    "personal_root",
    "registry_slug",
    "request_allows",
    "request_is_owner",
    "request_may_write",
    "request_subject",
    "require_create",
    "require_owner",
    "require_visible",
    "require_writable",
    "root_login",
    "scope_for",
    "scope_for_names",
    "self_service_enabled",
    "shared_candidates",
    "visible_profiles",
    "writable_profiles",
]
