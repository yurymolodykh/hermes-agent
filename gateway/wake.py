"""Wake an existing agent session from a background completion event.

Two delivery strategies, selected by the target adapter's
``supports_async_delivery`` capability flag:

* Push-capable adapters (telegram, discord, plugin platforms, ...): inject a
  synthetic ``MessageEvent(internal=True)`` through ``adapter.handle_message``
  — the pre-existing wake path, preserved exactly.

* Stateless request/response adapters (the API server,
  ``supports_async_delivery = False``): ``handle_message`` would run the wake
  turn under a ``build_session_key()``-derived key
  (``agent:main:api_server:group:<sid>``) that NEVER matches the raw
  ``X-Hermes-Session-Id`` key real gateway/HQ turns run under
  (``_bind_api_server_session``), so the wake lands in a parallel, invisible
  session. Instead we self-POST ``/v1/chat/completions`` on the in-pod API
  server with the raw session id in the ``X-Hermes-Session-Id`` header. This
  runs an agent turn in the same server-side session, but its HTTP response
  goes to the notifier — NOT to the owner's OpenWebUI chat. Callers must not
  treat this as an acknowledgement of user-visible delivery.

Failures RAISE (after bounded retries on transient errors). HTTP 200 with an
incomplete agent body also raises, so callers retain their delivery cursor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A wake self-post runs the entire agent turn synchronously (stream=false);
# generous ceiling so long tool-using turns aren't killed mid-flight.
WAKE_TURN_TIMEOUT_SECONDS = 600.0

# Backoff delays between retries on transient failures (429 concurrency cap,
# connection errors). The API server has no per-session lock — concurrent
# turns on one session are last-writer-wins — but it DOES enforce a global
# max_concurrent_runs cap via HTTP 429, which is worth waiting out.
_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)


def adapter_supports_push(adapter: Any) -> bool:
    """Whether this adapter can push a message to the user after a turn ends.

    Mirrors ``gateway.session_context.async_delivery_supported`` but reads the
    capability off the adapter class (``supports_async_delivery``) instead of
    the request-scoped contextvar — background watchers run outside any bound
    session context. Adapters that don't declare the flag are push-capable.
    """
    return bool(getattr(adapter, "supports_async_delivery", True))


async def deliver_wake(
    adapter: Any,
    *,
    text: str,
    session_id: str = "",
    source: Any = None,
) -> None:
    """Deliver a wake turn to the session behind ``adapter``.

    ``session_id`` is the RAW session id (the ``X-Hermes-Session-Id`` value /
    ``state.db`` key) — required for non-push adapters. ``source`` is the
    ``SessionSource`` used to build the synthetic event — required for
    push-capable adapters.

    Raises on failure (bad arguments, exhausted retries, HTTP error) so the
    caller can rewind/retry instead of treating the wake as delivered.
    """
    if adapter_supports_push(adapter):
        if source is None:
            raise ValueError(
                "deliver_wake: push-capable adapter requires a SessionSource"
            )
        from gateway.platforms.base import MessageEvent, MessageType

        synth_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        await adapter.handle_message(synth_event)
        return

    if not session_id:
        raise ValueError(
            "deliver_wake: non-push adapter (supports_async_delivery=False) "
            "requires the raw session id to self-post the wake turn"
        )
    await _self_post_chat_completion(adapter, text=text, session_id=session_id)


async def _self_post_chat_completion(
    adapter: Any, *, text: str, session_id: str
) -> None:
    """POST the wake text to the in-pod API server as a normal session turn.

    Uses the adapter's own bind host/port/key (``ApiServerAdapter.__init__``).
    Session continuation via ``X-Hermes-Session-Id`` is 403-gated on
    ``API_SERVER_KEY`` being configured, so a missing key is a hard error —
    raise loudly rather than run the wake in a fresh fingerprint-derived
    session nobody is looking at.
    """
    import aiohttp

    host = str(getattr(adapter, "_host", "") or "127.0.0.1")
    if host in ("0.0.0.0", "::", "*"):
        # Wildcard bind address — connect over loopback.
        host = "127.0.0.1"
    port = int(getattr(adapter, "_port", 0) or 8642)
    api_key = str(getattr(adapter, "_api_key", "") or "")
    if not api_key:
        raise RuntimeError(
            "wake self-post requires API_SERVER_KEY: session continuation via "
            "X-Hermes-Session-Id is rejected (403) on an unauthenticated API "
            "server, so the wake cannot reach the target session"
        )

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6 literal
    url = f"http://{host}:{port}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Hermes-Session-Id": session_id,
    }
    payload = {
        "model": str(getattr(adapter, "_model_name", "") or "hermes-agent"),
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }

    last_err: Optional[BaseException] = None
    attempts = 1 + len(_RETRY_DELAYS_SECONDS)
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_RETRY_DELAYS_SECONDS[attempt - 1])
        try:
            timeout = aiohttp.ClientTimeout(total=WAKE_TURN_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 429:
                        # Global concurrency cap (max_concurrent_runs) —
                        # transient; back off and retry.
                        last_err = RuntimeError(
                            f"wake self-post got HTTP 429 (concurrency cap) "
                            f"for session {session_id}"
                        )
                        logger.warning(
                            "%s; attempt %d/%d", last_err, attempt + 1, attempts
                        )
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        # Non-transient (auth/validation) — fail immediately.
                        raise RuntimeError(
                            f"wake self-post failed for session {session_id}: "
                            f"HTTP {resp.status}: {body}"
                        )
                    # HTTP 200 is not proof of a successful agent turn. The API
                    # server returns 200 with hermes.failed=true and
                    # finish_reason=error when a run produced partial text.
                    # Reject malformed responses too; otherwise a proxy error
                    # page could silently consume the notification cursor.
                    try:
                        body = await resp.json()
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        raise RuntimeError(
                            f"wake self-post returned invalid JSON for session {session_id}"
                        ) from exc
                    if not isinstance(body, dict):
                        raise RuntimeError(
                            f"wake self-post returned invalid completion for session {session_id}"
                        )
                    hermes = body.get("hermes") or {}
                    choices = body.get("choices") or []
                    if (not isinstance(hermes, dict) or not isinstance(choices, list)
                            or not choices or not isinstance(choices[0], dict)):
                        raise RuntimeError(
                            f"wake self-post returned invalid completion for session {session_id}"
                        )
                    if (hermes.get("failed") is True
                            or hermes.get("completed") is False
                            or hermes.get("partial") is True
                            or choices[0].get("finish_reason") != "stop"
                            or body.get("object") != "chat.completion"):
                        raise RuntimeError(
                            f"wake self-post agent turn incomplete for session {session_id}"
                        )
                    logger.info(
                        "wake self-post agent turn completed for session %s (attempt %d); "
                        "client display not confirmed",
                        session_id,
                        attempt + 1,
                    )
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            last_err = exc
            logger.warning(
                "wake self-post transient failure for session %s "
                "(attempt %d/%d): %s",
                session_id,
                attempt + 1,
                attempts,
                exc,
            )
            continue
    raise RuntimeError(
        f"wake self-post gave up for session {session_id} after "
        f"{attempts} attempts: {last_err}"
    ) from last_err
