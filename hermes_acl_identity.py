"""Who a signed-in caller IS, and whether the credential that named them is still valid.

Step 2 built the registry that says which profiles belong to which person; this module is the
missing bridge between a *login* (what a person typed, what the users file holds) and the
*subject* every owner comparison in Hermes compares against. Until it exists, every dashboard
sign-in is the host owner (:func:`gateway.owner_identity.principal_subject` says so in as many
words), so 25 logins are one human and revoking one of them revokes nothing.

Three questions, and only three:

* **who is this login** — :func:`resolve_subject`: a users-file record with ``role: owner`` is
  still the host owner (their sessions, rooms and history keep matching, which is the whole
  point of step 2's registry), and every other record resolves to ``person/<login>``. A login
  the users file does not know keeps the caller's legacy answer, so an installation that never
  creates a users file behaves exactly as before.
* **is that credential still current** — :func:`credential_valid`: the record must still exist,
  must not be disabled, and the *generation* baked into the ticket must equal the record's
  current ``gen``. That is what makes ``passwd``/``disable``/``revoke``/deletion throw out the
  devices that are already signed in instead of waiting for a 7-day session to lapse.
* **how long may it live** — :func:`session_ttl_seconds` / :func:`refresh_ttl_seconds`: seven
  days once multi-user identity is on. A TTL without a generation is seven days of access for
  somebody who was fired yesterday, which is why the card insists both land together.

**The flag.** ``acl.multi_user`` (``config.yaml``, default ``false``). Step 3 SHIPS this module
with the flag off: while it is false every function here returns the caller's legacy answer and
no session carries a generation, so the live installation moves not one right. Turning it on is
the owner's separate decision (the first enablement of the mode), exactly as the card requires.
``acl.enabled`` stays an independent switch: identity answers *who*, ``acl.enabled`` decides
whether history is scoped at all.

Fail-closed, and additive, in the same sense as the bindings registry: a users file that does
not parse yields no records (``hermes_cli.dashboard_users`` voids the whole file), so nobody
from it resolves to a person and every one of their tickets is refused — never "unknown, so
treat as owner".
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: ``acl.multi_user`` — this module is a no-op until it is true.
FLAG_SECTION = "acl"
FLAG_KEY = "multi_user"

#: Seven days: the card's number. A rolling session that outlives a revocation is the bug.
MULTI_USER_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
#: The refresh token lives no longer than the session it rotates in multi-user mode.
MULTI_USER_REFRESH_TTL_SECONDS = 7 * 24 * 60 * 60

#: The registry's person-slug rule (``hermes_acl_bindings._PERSON_RE``). Kept in sync by
#: :func:`hermes_acl_bindings.person_slug` sharing this constant rather than re-deriving it.
SLUG_MAX = 63
_SLUG_ALLOWED = re.compile(r"[^a-z0-9._-]")

#: The one provider whose logins this module turns into persons. A federated (OAuth) login is
#: already a stable subject minted by its IdP and is not this step's business.
LOGIN_PROVIDERS = frozenset({"basic"})


def _flag_from_user_layer(config_path) -> Optional[bool]:
    """``acl.multi_user`` from ONE config file's user layer, or ``None`` when the file is silent.

    The user layer (not the merged config) is what distinguishes "off" from "unset": the shipped
    ``DEFAULT_CONFIG`` already carries the ``acl`` section, so a merged read answers ``False`` for
    every home that simply never mentioned it.
    """
    from hermes_cli.config_effective import load_user_config_effective

    layer = load_user_config_effective(config_path)
    section = layer.get(FLAG_SECTION) if isinstance(layer, dict) else None
    if not isinstance(section, dict) or FLAG_KEY not in section:
        return None
    return bool(section.get(FLAG_KEY))


def multi_user_enabled() -> bool:
    """Whether multi-user identity is ON (``acl.multi_user``). Absent/false = legacy behaviour.

    The flag is a deployment decision, so a profile home that does not set it inherits the ROOT
    one — like the binding registry it gates (``hermes_acl_bindings.bindings_path``). Reading only
    the process home reported "off" inside every per-profile process (a profile-scoped socket, an
    ``owner_scope``d worker), silently disabling the whole multi-user layer exactly where it has to
    hold: the served profile's own ``config.yaml`` has no ``acl`` section.
    """
    try:
        from hermes_cli.config import get_config_path

        own = get_config_path()
        flag = _flag_from_user_layer(own)
        if flag is not None:
            return flag
        from hermes_constants import get_default_hermes_root

        root_config = get_default_hermes_root() / "config.yaml"
        if root_config != own:
            inherited = _flag_from_user_layer(root_config)
            if inherited is not None:
                return inherited
        from hermes_cli.config import cfg_get, load_config

        return bool(cfg_get(load_config(), FLAG_SECTION, FLAG_KEY, default=False))
    except Exception:  # a broken config must not switch identity on, and must not crash a read
        logger.debug("acl identity: config unavailable for %s.%s", FLAG_SECTION, FLAG_KEY,
                     exc_info=True)
        return False


# ────────────────────────────────────────────────────────────────────────────
# login → subject
# ────────────────────────────────────────────────────────────────────────────


def person_slug(login: Any) -> str:
    """``person/<slug>`` for *login*, or ``""`` when nothing usable is left of it.

    The registry accepts ``^person/[a-z0-9][a-z0-9._-]{0,63}$``; a login that violates it (an
    upper-case e-mail login, a bare digit, an imported name with spaces) is normalised the same
    way every time so a person's subject is stable across restarts and CLI runs, and so the
    record an operator writes by hand is the record the login resolves to.
    """
    text = str(login or "").strip().lower()
    if not text:
        return ""
    text = _SLUG_ALLOWED.sub("-", text).strip("-.")
    text = text[:SLUG_MAX].strip("-.")
    if not text or not text[0].isalnum():
        return ""
    return f"person/{text}"


def person_subject(login: Any) -> str:
    """The subject a non-owner login acts as; ``""`` when the login cannot name anyone."""
    return person_slug(login)


def users_store():
    """The configured users-file store, or ``None`` when no file is configured.

    Imported lazily: this module is read from gateway boot paths where importing the dashboard's
    user-file helpers would drag in half the CLI.
    """
    try:
        from hermes_cli import dashboard_users as du
        from hermes_cli.config import cfg_get, load_config

        value = cfg_get(load_config(), "dashboard", "basic_auth", "users_file", default="")
    except Exception:
        logger.debug("acl identity: cannot resolve dashboard.basic_auth.users_file", exc_info=True)
        return None
    path = du.resolve_users_file_path(value)
    if path is None:
        return None
    return du.UserStore(path)


def record_for(login: Any) -> Optional[Any]:
    """The users-file :class:`~hermes_cli.dashboard_users.UserRecord` for *login*, or ``None``.

    ``None`` covers three different states on purpose (no file, unknown login, unusable file):
    all three mean "this login names nobody here", and :func:`resolve_subject` answers with the
    caller's legacy subject for it.
    """
    text = str(login or "").strip()
    if not text:
        return None
    store = users_store()
    if store is None:
        return None
    try:
        return store.lookup(text)
    except Exception:
        logger.warning("acl identity: users file unusable while resolving %r", text, exc_info=True)
        return None


def is_owner_record(record: Any) -> bool:
    """Whether *record* is the host owner (``role: owner``)."""
    return str(getattr(record, "role", "") or "").strip().lower() == "owner"


def resolve_subject(login: Any, *, fallback: str = "") -> str:
    """The subject *login* acts as, with *fallback* the caller's legacy answer.

    * multi-user OFF (or an unknown/unusable login): *fallback*, unchanged — this is what makes
      the step additive;
    * the host owner's own login: :func:`gateway.owner_identity.host_owner_subject`, so the
      owner's history, rooms and native surfaces keep matching the stamps they already carry;
    * anybody else: ``person/<login>``.

    A login that cannot produce a valid person slug is refused (``""``) rather than silently
    acting as the owner: an unnameable identity must see nothing, not everything.
    """
    if not multi_user_enabled():
        return fallback
    record = record_for(login)
    if record is None:
        return fallback
    if is_owner_record(record):
        from gateway.owner_identity import host_owner_subject

        return host_owner_subject()
    subject = person_subject(getattr(record, "login", "") or login)
    if not subject:
        logger.warning("acl identity: login %r yields no usable person subject — refusing",
                       getattr(record, "login", login))
        return ""
    return subject


def resolve_identity_subject(identity: dict, *, legacy) -> str:
    """Subject for a dashboard ``{user_id, provider, issuer}`` identity.

    Only the password provider's logins are split into persons here; every other credential
    (a federated login, the loopback session token, a server-spawned child) keeps whatever
    *legacy* (the caller's existing rule) says, because a federated subject is already an
    identity its IdP minted and re-labelling it would strand the rows it owns.
    """
    if not multi_user_enabled():
        return legacy
    provider = str(identity.get("provider") or "").strip()
    if provider not in LOGIN_PROVIDERS:
        return legacy
    login = str(identity.get("user_id") or "").strip()
    if not login:
        return legacy
    return resolve_subject(login, fallback=legacy)


# ────────────────────────────────────────────────────────────────────────────
# generation — what makes revocation immediate
# ────────────────────────────────────────────────────────────────────────────


def session_generation(login: Any) -> Optional[int]:
    """The record's current ``gen``, or ``None`` when this login has no users-file record.

    ``None`` is the legacy single-pair case: such a session carries no generation and is never
    revoked by this mechanism (its credential is the deployment's own config).
    """
    record = record_for(login)
    if record is None:
        return None
    try:
        return int(getattr(record, "gen", 1))
    except (TypeError, ValueError):
        logger.warning("acl identity: login %r has a non-integer generation — refusing",
                       getattr(record, "login", login))
        return -1


def credential_valid(login: Any, generation: Any, *, from_file: Optional[bool] = None,
                     reason_out: Optional[list] = None) -> bool:
    """Whether a ticket minted for *login* at *generation* may still be accepted.

    *from_file* says whether the ticket's credential came from a users-file record (rather than
    the deployment's single configured ``username``/``password_hash`` pair); ``None`` means "the
    token did not say", which is read as a users-file credential only when the file has a record
    for that login.

    False (with *reason_out* naming which rule fired, for audit lines) when:

    * the record is gone (deleted account: its sessions die with it);
    * the record is ``disabled``;
    * the ticket's generation is absent or does not equal the record's — the device was signed
      in before a ``passwd``/``disable``/``revoke``;
    * the users file cannot be read: an unreadable source of truth refuses (fail-closed), it does
      not wave the ticket through.

    True while multi-user identity is OFF (callers do not consult this at all then, but a direct
    call must not start refusing sessions), and for a login the file simply does not know when
    the ticket came from the single configured pair — that credential is the deployment's own and
    has no generation to bump.
    """
    if not multi_user_enabled():
        return True
    text = str(login or "").strip()
    if not text:
        _reject(reason_out, "no_login")
        return False
    store = users_store()
    if store is None:
        # No users file: this installation has no per-person accounts, so nothing to revoke.
        return True
    loaded = _load_for(text, reason_out)
    if loaded is None:
        return False
    record = loaded.by_login(text)
    if record is None:
        if from_file is None:
            # A ticket with no source marker: the file's own silence is the answer.
            from_file = bool(loaded.users)
        if not from_file:
            return True
        # The credential came from a record this file no longer holds: deleted, or never theirs.
        _reject(reason_out, "no_such_user")
        return False
    try:
        current = int(getattr(record, "gen", 1))
    except (TypeError, ValueError):
        _reject(reason_out, "bad_generation")
        return False
    if getattr(record, "disabled", False):
        _reject(reason_out, "disabled")
        return False
    return _generation_matches(generation, current, reason_out)


def _generation_matches(generation: Any, current: int, reason_out: Optional[list]) -> bool:
    """Whether the generation a ticket presents is the record's current one."""
    if generation is None:
        _reject(reason_out, "no_generation_on_ticket")
        return False
    try:
        presented = int(generation)
    except (TypeError, ValueError):
        _reject(reason_out, "bad_generation_on_ticket")
        return False
    if presented != current:
        _reject(reason_out, f"generation_superseded:{presented}!={current}")
        return False
    return True


def _reject(reason_out: Optional[list], reason: str) -> None:
    """Record why a credential was refused (audit line), if the caller asked for the reason."""
    if reason_out is not None:
        reason_out.append(reason)


def _load_for(login: str, reason_out: Optional[list]):
    """The users file as loaded for a decision, or ``None`` when it must not be trusted.

    An unreadable or invalid file yields ``None`` AND a reason: a source of truth that cannot be
    read refuses the credential (fail-closed), it never waves it through.
    """
    store = users_store()
    if store is None:
        return None
    try:
        loaded = store.load()
    except Exception:
        logger.warning("acl identity: users file unreadable — REFUSING %r", login, exc_info=True)
        _reject(reason_out, "users_file_unusable")
        return None
    if not loaded.ok:
        _reject(reason_out, "users_file_invalid")
        return None
    return loaded


def revoke_reason(login: Any, generation: Any, *, from_file: Optional[bool] = None) -> str:
    """One-line audit form of :func:`credential_valid`'s verdict (never credential material)."""
    reasons: list = []
    valid = credential_valid(login, generation, from_file=from_file, reason_out=reasons)
    if valid:
        return "accepted"
    return "refused:" + (",".join(reasons) or "unknown")


def credential_marker(login: Any) -> tuple[bool, Optional[int]]:
    """``(from_users_file, generation)`` to bake into a freshly minted ticket for *login*.

    ``(False, None)`` for the deployment's single configured pair — the legacy credential; a
    users-file record contributes its current ``gen`` so every later bump invalidates this ticket.
    """
    record = record_for(login)
    if record is None:
        return False, None
    try:
        return True, int(getattr(record, "gen", 1))
    except (TypeError, ValueError):
        logger.warning("acl identity: login %r has a non-integer generation", login)
        return True, -1


# ────────────────────────────────────────────────────────────────────────────
# lifetimes
# ────────────────────────────────────────────────────────────────────────────


def session_ttl_seconds(configured: Any, *, default_seconds: int) -> int:
    """Effective access-token lifetime: the configured value, else 7 days in multi-user mode.

    An explicit ``dashboard.basic_auth.session_ttl_seconds`` always wins — an operator who wrote
    a number meant it. Without one, multi-user mode uses seven days and single-user keeps the
    provider's own default, so turning the flag off restores the previous lifetime exactly.
    """
    try:
        value = int(configured or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return max(60, value)
    if multi_user_enabled():
        return MULTI_USER_SESSION_TTL_SECONDS
    return max(60, int(default_seconds))


def refresh_ttl_seconds(configured: Any, *, default_seconds: int) -> int:
    """Effective refresh-token lifetime, under the same rule as :func:`session_ttl_seconds`.

    Multi-user mode caps it at seven days: a 30-day refresh token would hand a revoked person a
    new session long after the generation that revoked them, which is the failure the card names.
    """
    try:
        value = int(configured or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return max(60, value)
    if multi_user_enabled():
        return MULTI_USER_REFRESH_TTL_SECONDS
    return max(60, int(default_seconds))


def describe() -> str:
    """Short audit form: is the mode on, and how long a session lives."""
    if not multi_user_enabled():
        return "identity=legacy(multi_user off)"
    return f"identity=multi-user session_ttl={MULTI_USER_SESSION_TTL_SECONDS}s"


__all__ = [
    "FLAG_KEY",
    "FLAG_SECTION",
    "LOGIN_PROVIDERS",
    "MULTI_USER_REFRESH_TTL_SECONDS",
    "MULTI_USER_SESSION_TTL_SECONDS",
    "credential_marker",
    "credential_valid",
    "describe",
    "is_owner_record",
    "multi_user_enabled",
    "person_slug",
    "person_subject",
    "record_for",
    "refresh_ttl_seconds",
    "resolve_identity_subject",
    "resolve_subject",
    "revoke_reason",
    "session_generation",
    "session_ttl_seconds",
    "users_store",
]
