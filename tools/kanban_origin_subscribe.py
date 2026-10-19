"""Owner-bound kanban subscription from a VERIFIED Open WebUI origin binding.

Called only from ``tools.kanban_tools._maybe_auto_subscribe`` when the session
platform is ``api_server``. The binding in ``HERMES_SESSION_ORIGIN`` is
produced exclusively by the gateway's origin-binding middleware (WebUI-signed
identity claim) and is re-verified here against the server-owned chat registry
BEFORE any subscription row is written — a client-supplied ``user_id`` (or any
other request parameter) can never reach this code path.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def subscribe_verified_origin(conn: Any, task_id: str) -> bool:
    """Subscribe owner-bound when the session carries a verified OWUI origin.

    Fail closed: returns False for an unbound session, a foreign/expired
    binding, a chat missing from the server registry, or an owner mismatch —
    in every one of those cases no subscription row is written.
    """
    try:
        from gateway import origin_binding

        binding = origin_binding.resolve_origin_binding()
        if binding is None:
            return False
        from gateway.session_context import get_session_env

        chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "") or "dm"
        from hermes_cli import kanban_db as _kb

        _kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="api_server",
            chat_id=binding["chat_id"],
            thread_id=None,
            user_id=binding["user_id"],
            chat_type=chat_type,
            notifier_profile=binding["profile"],
            delivery_mode="notify+wake",
            delivery_metadata=None,
        )
        # F3 slice 3 (t_cdb88a42): the verified-origin subscription also
        # carries the server-side nullable OWUI-owner marker in
        # ``delivery_metadata[owui_user_id_attr]``.  This is the durable
        # attribution the notifier's ``_sub_owui_owner_id`` uses to resolve the
        # owner for PWA/yadisk api_server subscriptions (and a belt-and-braces
        # copy for OWUI-prefixed chats).  It is written ONLY from this
        # server-verified seam — never from client-supplied parameters — and
        # ``set_notify_sub_owui_owner`` clears it (fail-closed) when absent.
        try:
            _kb.set_notify_sub_owui_owner(
                conn,
                task_id=task_id,
                platform="api_server",
                chat_id=binding["chat_id"],
                thread_id=None,
                owui_user_id=binding["user_id"],
            )
        except Exception:
            logger.exception(
                "kanban_create: failed to set owui_user_id_attr marker on "
                "verified-origin api_server subscription"
            )
        return True
    except Exception:
        logger.exception(
            "kanban_create: verified-origin api_server subscription failed"
        )
        return False
