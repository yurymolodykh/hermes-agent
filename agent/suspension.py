"""Generic, opt-in control value for pausing an agent at a tool boundary.

A tool or platform callback returns :class:`ToolSuspension` instead of a normal
result.  The executor then stops the current turn without fabricating a
``role=tool`` result.  The already-durable assistant tool-call row remains the
recovery boundary; a later process may resume as a fresh turn using the opaque
payload persisted by the integrating platform.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolSuspension:
    """JSON-safe metadata describing an intentional tool-boundary suspension.

    Returning this value is explicitly opt-in.  Ordinary strings, dictionaries,
    exceptions, timeouts, and existing callback results retain their historical
    behavior.
    """

    kind: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("suspension kind must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise TypeError("suspension payload must be a mapping")
        try:
            # Validate AND snapshot now: this object crosses
            # persistence/plugin boundaries holistically, so it must not
            # hold a mutable alias of the caller's payload.  Deep-copy via a
            # strict JSON round-trip (allow_nan=False) so later mutation of
            # the source dict can never change what to_result() projects.
            snapshot = json.loads(
                json.dumps(dict(self.payload), ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise TypeError("suspension payload must be JSON-serializable") from exc
        if not isinstance(snapshot, dict):
            raise TypeError("suspension payload must be a JSON object")
        object.__setattr__(self, "payload", snapshot)

    def to_result(self, *, tool_name: str, tool_call_id: str) -> dict[str, Any]:
        """Build the stable, JSON-safe result exposed by the agent/gateway.

        The internal boundary may carry the full payload (including private
        routing keys such as ``session_key``).  Use
        :func:`public_suspension_projection` to derive the bounded shape that
        is safe to expose to observers, public results, and logs.
        """
        return {
            "kind": self.kind.strip(),
            "payload": self.payload,
            "tool_name": str(tool_name or "tool"),
            "tool_call_id": str(tool_call_id or ""),
        }


# Payload keys that carry private routing metadata and must never reach
# observer plugins, public results, or logs.  ``session_key`` is the
# deferred-clarify routing secret; the payload may also hold other integration
# routing keys a platform wants to keep internal.
SUSPENSION_PRIVATE_PAYLOAD_KEYS: frozenset[str] = frozenset({"session_key"})


def public_suspension_projection(suspension: Any) -> dict[str, Any]:
    """Bounded public shape of a suspension for observers/results/logs.

    Works on either a :class:`ToolSuspension` result dict or a
    :class:`~gateway.platforms.base.SuspendedReply` dict.  Drops the private
    routing envelope (``tool_name``/``tool_call_id`` at the top level and
    ``session_key`` inside ``payload``) so async/generic plugins can never
    observe the routing secret, while preserving the non-secret metadata
    (``kind``, question set) needed for diagnostics.

    Defensive: malformed input yields ``{}`` / ``{"kind": ...}`` rather than
    raising — this projection sits on the untrusted agent-result boundary.
    """
    if not isinstance(suspension, Mapping):
        return {}
    result: dict[str, Any] = {}
    kind = suspension.get("kind")
    if isinstance(kind, str):
        result["kind"] = kind
    payload = suspension.get("payload")
    if isinstance(payload, Mapping):
        cleaned = {
            k: v
            for k, v in dict(payload).items()
            if k not in SUSPENSION_PRIVATE_PAYLOAD_KEYS
        }
        if cleaned:
            result["payload"] = cleaned
    return result


__all__ = ["ToolSuspension", "public_suspension_projection", "SUSPENSION_PRIVATE_PAYLOAD_KEYS"]
