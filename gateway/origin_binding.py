"""Authenticated exact-origin binding for Open WebUI → api_server sessions.

Server-side seam, not a public API contract: an Open WebUI-signed identity
claim (``X-OpenWebUI-User-Jwt``, minted from the authenticated ``user`` when
``ENABLE_FORWARD_USER_INFO_HEADERS`` + ``FORWARD_USER_INFO_HEADER_JWT_SECRET``
are configured on the WebUI) is verified here against the SAME shared secret
and resolved against the server-owned chat registry. Only a verified pairing
of (authenticated owner, WebUI chat UUID, serving profile) becomes an origin
binding that ``tools.kanban_tools._maybe_auto_subscribe`` may subscribe with.

An API bearer key alone never grants a binding, any client-supplied owner
claim outside this seam is ignored, and every check fails closed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Mapping, Optional

from gateway.session_context import get_session_env

# Signed identity claim minted by Open WebUI (utils/headers.py::_mint_forward_user_jwt).
_JWT_HEADER = "X-OpenWebUI-User-Jwt"
# WebUI conversation id header (FORWARD_SESSION_INFO_HEADER_CHAT_ID upstream default).
_CHAT_HEADER = "X-OpenWebUI-Chat-Id"
_ISSUER = "open-webui"
# WebUI defaults: 300s token lifetime. Verification allows small clock skew.
_EXP_LEEWAY_SECONDS = 30
_MAX_CLAIM_LEN = 4096
_MAX_SUBJECT_LEN = 128

_CHAT_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header read (aiohttp CIMultiDict is already CI)."""
    try:
        value = headers.get(name)
        if value is not None:
            return value
    except AttributeError:
        pass
    lower = name.lower()
    for key in headers:
        if str(key).lower() == lower:
            return headers[key]
    return ""


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _verify_hs256(token: str, secret: str) -> Optional[dict]:
    """Verify an HS256 JWT with stdlib only; return the payload or None.

    Fails closed on any structural, algorithm, signature, issuer, or time
    claim problem. ``exp`` and ``iat`` are REQUIRED - a claim without them
    never verifies.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    if len(token) > _MAX_CLAIM_LEN:
        return None
    head_b64, payload_b64, sig_b64 = token.split(".")
    try:
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{head_b64}.{payload_b64}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, supplied):
            return None
        header = json.loads(_b64url_decode(head_b64))
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            return None
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("iss") != _ISSUER:
        return None
    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    if exp < now - _EXP_LEEWAY_SECONDS:
        return None
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool):
        return None
    if iat > now + _EXP_LEEWAY_SECONDS:
        return None
    return payload


def configured_secret() -> str:
    """The shared WebUI forward secret, or "" when unconfigured.

    Read live (no cache): the secret is instance configuration, and a cache
    would outlive a same-process test or operator rotation.
    """
    return (os.environ.get("FORWARD_USER_INFO_HEADER_JWT_SECRET") or "").strip()


def resolve_request_origin(
    headers: Mapping[str, str], profile: Optional[str] = None
) -> Optional[dict]:
    """Verify the WebUI-signed claim + chat header into an origin binding.

    Called ONLY from the api_server request middleware. Returns the
    request-scoped binding (chat_id/user_id/profile) or None - every
    mismatch (unsigned, foreign secret, expired, missing/foreign chat id)
    fails closed as "unbound".
    """
    secret = configured_secret()
    if not secret:
        return None
    token = _header(headers, _JWT_HEADER).strip()
    if not token:
        return None
    payload = _verify_hs256(token, secret)
    if payload is None:
        return None
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        return None
    if len(subject) > _MAX_SUBJECT_LEN:
        return None
    raw_chat = _header(headers, _CHAT_HEADER).strip()
    match = _CHAT_UUID_RE.fullmatch(raw_chat)
    if not match:
        return None
    chat_id = "owui-" + match.group(1)
    origin: dict[str, Any] = {
        "platform": "api_server",
        "chat_id": chat_id,
        "session_id": chat_id,
        "thread_id": None,
        "user_id": subject,
    }
    if profile:
        origin["profile"] = profile
    return origin


def resolve_origin_binding() -> Optional[dict]:
    """Re-verify the session origin against the server-owned chat registry.

    The HTTP boundary verified authenticity of the identity claim; this
    second server-side check confirms that THIS chat still exists and is
    owned by the authenticated subject BEFORE a subscription row is written.
    Reads the request-scoped ``HERMES_SESSION_ORIGIN`` context value (set
    exclusively by the gateway's verified middleware path) - never client
    parameters. Returns the subscribe payload or None (fail closed).
    """
    raw = get_session_env("HERMES_SESSION_ORIGIN", "")
    if not raw:
        return None
    try:
        origin = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(origin, dict) or origin.get("platform") != "api_server":
        return None
    chat_id = origin.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.startswith("owui-"):
        return None
    if not _CHAT_UUID_RE.fullmatch(chat_id[len("owui-"):]):
        return None
    if origin.get("session_id") not in (None, chat_id):
        return None
    if origin.get("thread_id") is not None:
        return None
    user_id = origin.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    if len(user_id) > _MAX_SUBJECT_LEN:
        return None
    profile = origin.get("profile")
    if profile is not None and not isinstance(profile, str):
        return None
    registry = os.environ.get("HERMES_OPENWEBUI_DB_PATH")
    if not registry:
        return None
    effective_profile = profile
    if not effective_profile:
        try:
            from hermes_cli.profiles import get_active_profile_name

            effective_profile = get_active_profile_name() or "default"
        except Exception:
            effective_profile = "default"
    from gateway.openwebui_origin import resolve_origin

    resolved = resolve_origin(registry, effective_profile, {
        "platform": "api_server",
        "chat_id": chat_id,
        "session_id": chat_id,
        "thread_id": None,
        "user_id": user_id,
    })
    if resolved is None or resolved.get("owner_id") != user_id:
        return None
    return {
        "chat_id": chat_id,
        "session_id": chat_id,
        "user_id": resolved["owner_id"],
        "profile": effective_profile,
    }
