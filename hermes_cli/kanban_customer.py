"""Who a kanban card was ordered by, and where that person is reached.

A card's ``created_by`` used to be the *surface* that made it (``"dashboard"``, a profile
name) instead of the person who asked for the work, so the delivery pipeline had nobody to
address: the notifier could only fan out to whatever chat the creating session happened to
be, and a card created in the web interface reached no one at all.

This module is the one policy both surfaces share:

* :func:`creator_label` — what to persist in ``created_by``: the caller's verified subject
  when multi-user identity is on and it resolves, else the legacy surface label. Multi-user
  OFF answers the legacy label byte for byte, so the flag stays additive.
* :func:`delivery_addresses` — the platform chats that subject is provably reachable on,
  taken from the ACL bindings registry (``subjects: ["tg:<id>"]``), never guessed from a
  display name or a header.
* :func:`notify_targets` — the ``add_notify_sub`` kwargs for that person, stamped with
  ``notifier_profile`` of the gateway that serves the transport. The orchestrator profile is
  the *second* address for the same card, never a replacement: its subscription is written
  by the caller's own session path, unchanged.

Nothing here writes: the two call sites decide whether a subscription is allowed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Registry subject stamps that name a reachable chat: ``tg:<telegram user id>``. A stamp with
# no chat behind it (``uid:<n>``, ``cron-owner``) is a scope label, not a destination.
_STAMP_PLATFORMS = (("tg:", "telegram"),)

# Telegram: a user id is also the DM chat id, so the stamp carries the whole address.
_DM_PLATFORM_PREFIX = {"telegram": ""}

LEGACY_DASHBOARD_LABEL = "dashboard"


def _multi_user() -> bool:
    try:
        from hermes_acl_identity import multi_user_enabled

        return bool(multi_user_enabled())
    except Exception:  # health: allow BLE001 -- an unreadable flag must not enable the boundary
        logger.debug("kanban customer: multi_user flag unreadable", exc_info=True)
        return False


def creator_label(surface: str, request: Any = None) -> str:
    """``created_by`` for a card created on *surface* by *request*'s caller.

    Multi-user ON with a verified subject: the subject. Anything else — the flag off, no
    request (CLI/cron/tests), or a request carrying no identity — keeps *surface*, which is
    the exact value the pre-multi-user build wrote.
    """
    if _multi_user():
        try:
            from hermes_acl_results import request_subject

            subject = str(request_subject(request) or "").strip()
        except Exception:  # health: allow BLE001 -- an identity lookup failure is "no identity"
            logger.debug("kanban customer: subject resolution failed", exc_info=True)
            subject = ""
        if subject:
            return subject
    return surface


def _stamp_address(stamp: str) -> Optional[tuple[str, str]]:
    text = str(stamp or "").strip()
    for prefix, platform in _STAMP_PLATFORMS:
        if not text.startswith(prefix):
            continue
        chat_id = text[len(prefix):].strip()
        if chat_id:
            return platform, chat_id
    return None


def delivery_addresses(subject: Any) -> list[tuple[str, str]]:
    """``(platform, chat_id)`` pairs *subject* is reachable on, per the bindings registry.

    Fail closed: an absent/corrupt registry or a subject no record names answers ``[]`` — a
    card with no provable address simply has no customer subscription, which is exactly
    today's behaviour.
    """
    text = str(subject or "").strip()
    if not text:
        return []
    try:
        import hermes_acl_bindings as hb

        registry = hb.load_cached()
    except Exception:  # health: allow BLE001 -- an unusable registry delivers nothing
        logger.warning("kanban customer: bindings registry unusable for %r", text, exc_info=True)
        return []
    if registry is None:
        return []
    try:
        stamps = registry.subjects_of(text) or (text,)
    except Exception:  # health: allow BLE001 -- same refusal as an unusable registry
        logger.warning("kanban customer: subject stamps unavailable for %r", text, exc_info=True)
        return []
    out: list[tuple[str, str]] = []
    for stamp in stamps:
        address = _stamp_address(stamp)
        if address and address not in out:
            out.append(address)
    return out


def notifier_profile_for(platform: str) -> str:
    """The profile whose gateway owns *platform*'s transport.

    A persisted ``profile_routes`` entry for the platform wins; otherwise the configured
    orchestrator profile — the profile whose bot this deployment serves the platform on — and
    ``default`` last. The stamp only says WHICH notifier may deliver the row; the recipient is
    the ``chat_id``/``user_id`` written beside it.
    """
    wanted = str(platform or "").strip().lower()
    try:
        from hermes_cli.config import cfg_get, load_config
    except Exception:  # health: allow BLE001 -- no config module means "no route configured"
        logger.debug("kanban customer: config module unavailable", exc_info=True)
        return "default"
    try:
        config = load_config()
    except Exception:  # health: allow BLE001 -- an unreadable config falls back to default
        logger.debug("kanban customer: config unreadable for route lookup", exc_info=True)
        config = {}
    for route in (config.get("gateway") or {}).get("profile_routes") or []:
        if not isinstance(route, dict) or route.get("enabled") is False:
            continue
        if str(route.get("platform") or "").strip().lower() == wanted and route.get("profile"):
            return str(route["profile"]).strip()
    try:
        orchestrator = str(cfg_get(config, "kanban", "orchestrator_profile", default="") or "").strip()
    except Exception:  # health: allow BLE001 -- optional setting
        orchestrator = ""
    return orchestrator or "default"


def notify_targets(subject: Any, *, platform: str = "") -> list[dict[str, Any]]:
    """``add_notify_sub`` kwargs for every chat *subject* is reachable on.

    ``delivery_mode="notify"``: the person is told the card finished, they are not woken as an
    agent — a customer address is a destination, not a worker slot. The subject itself rides in
    ``delivery_metadata.customer_subject`` so the delivery journal can name who was served
    without inventing a second identity column.
    """
    text = str(subject or "").strip()
    if not text:
        return []
    wanted = str(platform or "").strip().lower()
    targets: list[dict[str, Any]] = []
    for found_platform, chat_id in delivery_addresses(text):
        if wanted and found_platform != wanted:
            continue
        targets.append(dict(
            platform=found_platform,
            chat_id=chat_id,
            user_id=chat_id,
            chat_type="dm",
            notifier_profile=notifier_profile_for(found_platform),
            delivery_mode="notify",
            delivery_metadata={"customer_subject": text},
        ))
    return targets
