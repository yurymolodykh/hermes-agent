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
from typing import Any, Mapping, Sequence


_MAX_RESUME_RESULT_BYTES = 256 * 1024


class InvalidResume(ValueError):
    """A trusted resume value is malformed or unsupported."""


class ResumeConflict(RuntimeError):
    """The durable transcript no longer matches the validated resume."""


@dataclass(frozen=True)
class ValidatedToolResume:
    """Capability already authenticated and schema-validated by an adapter."""

    session_id: str
    kind: str
    tool_name: str
    tool_call_id: str
    result: str | Mapping[str, Any]
    token_fingerprint: str

    def __post_init__(self) -> None:
        for field_name in (
            "session_id", "kind", "tool_name", "tool_call_id", "token_fingerprint"
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidResume(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        if self.kind != "question_set" or self.tool_name != "clarify":
            raise InvalidResume("unsupported suspended tool resume kind")
        if isinstance(self.result, str):
            result = self.result
        elif isinstance(self.result, Mapping):
            try:
                result = json.dumps(
                    dict(self.result), ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise InvalidResume("resume result must be JSON-serializable") from exc
        else:
            raise InvalidResume("resume result must be a string or mapping")
        if not result:
            raise InvalidResume("resume result must not be empty")
        if len(result.encode("utf-8")) > _MAX_RESUME_RESULT_BYTES:
            raise InvalidResume("resume result is too large")
        object.__setattr__(self, "result", result)


def reconcile_validated_tool_resume(
    history: Sequence[Mapping[str, Any]], resume: ValidatedToolResume
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind a validated resume to a fresh durable tool-call tail."""
    if not isinstance(resume, ValidatedToolResume):
        raise InvalidResume("resume must be a ValidatedToolResume")
    rows = [dict(row) for row in history]
    assistant_index = -1
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].get("role") == "assistant" and rows[index].get("tool_calls"):
            assistant_index = index
            break
    if assistant_index < 0:
        raise ResumeConflict("no suspended assistant tool-call tail")
    suffix = rows[assistant_index + 1 :]
    if any(row.get("role") != "tool" for row in suffix):
        raise ResumeConflict("a newer turn exists after the suspended tool call")

    calls = rows[assistant_index].get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise ResumeConflict("malformed assistant tool-call tail")
    call_ids: list[str] = []
    call_names: dict[str, str] = {}
    for call in calls:
        if not isinstance(call, Mapping):
            raise ResumeConflict("malformed assistant tool call")
        call_id = call.get("id")
        function = call.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ResumeConflict("malformed assistant tool call")
        if call_id in call_names:
            raise ResumeConflict("duplicate assistant tool call id")
        call_ids.append(call_id)
        call_names[call_id] = name
    if resume.tool_call_id not in call_names:
        raise ResumeConflict("resume tool call is not present in the durable tail")
    if call_names[resume.tool_call_id] != resume.tool_name:
        raise ResumeConflict("resume tool name does not match the durable tail")

    result_by_id: dict[str, str] = {}
    for expected_id, row in zip(call_ids, suffix):
        actual_id = row.get("tool_call_id")
        if actual_id != expected_id or actual_id in result_by_id:
            raise ResumeConflict("tool results are not an ordered prefix of the call block")
        content = row.get("content")
        if not isinstance(content, str):
            raise ResumeConflict("durable tool result content is malformed")
        result_by_id[actual_id] = content
    if len(suffix) > len(call_ids):
        raise ResumeConflict("too many durable tool results")

    target_index = call_ids.index(resume.tool_call_id)
    if target_index < len(suffix):
        if result_by_id[resume.tool_call_id] != resume.result:
            raise ResumeConflict("tool call already has a different result")
        if len(suffix) != len(call_ids):
            raise ResumeConflict("partially persisted resume batch")
        return rows, []
    if target_index != len(suffix):
        raise ResumeConflict("resume target is not the next unclosed tool call")

    appended = [{
        "role": "tool", "tool_call_id": resume.tool_call_id,
        "tool_name": resume.tool_name, "content": resume.result,
    }]
    cancellation = json.dumps(
        {"cancelled": True, "reason": "not_executed_after_tool_suspension"},
        sort_keys=True, separators=(",", ":"),
    )
    for call_id in call_ids[target_index + 1 :]:
        appended.append({
            "role": "tool", "tool_call_id": call_id,
            "tool_name": call_names[call_id], "content": cancellation,
            "effect_disposition": "not_executed",
        })
    return rows + appended, appended


def completed_validated_tool_resume(
    history: Sequence[Mapping[str, Any]], resume: ValidatedToolResume
) -> str | None:
    """Return an already-persisted final answer for an exact completed resume tail."""
    if not isinstance(resume, ValidatedToolResume):
        raise InvalidResume("resume must be a ValidatedToolResume")
    rows = [dict(row) for row in history]
    assistant_index = -1
    calls: list[Mapping[str, Any]] = []
    for index in range(len(rows) - 1, -1, -1):
        candidate = rows[index]
        candidate_calls = candidate.get("tool_calls")
        if candidate.get("role") != "assistant" or not isinstance(candidate_calls, list):
            continue
        for call in candidate_calls:
            function = call.get("function") if isinstance(call, Mapping) else None
            if (
                isinstance(call, Mapping)
                and call.get("id") == resume.tool_call_id
                and isinstance(function, Mapping)
                and function.get("name") == resume.tool_name
            ):
                assistant_index = index
                calls = candidate_calls
                break
        if assistant_index >= 0:
            break
    if assistant_index < 0:
        return None
    suffix = rows[assistant_index + 1 :]
    if len(suffix) != len(calls) + 1:
        return None
    for call, result_row in zip(calls, suffix[:-1]):
        if not isinstance(call, Mapping):
            return None
        function = call.get("function")
        expected_name = function.get("name") if isinstance(function, Mapping) else None
        if (
            result_row.get("role") != "tool"
            or result_row.get("tool_call_id") != call.get("id")
            or result_row.get("tool_name") != expected_name
        ):
            return None
        if call.get("id") == resume.tool_call_id and result_row.get("content") != resume.result:
            return None
    final_row = suffix[-1]
    final_content = final_row.get("content")
    if (
        final_row.get("role") != "assistant"
        or final_row.get("tool_calls")
        or not isinstance(final_content, str)
        or not final_content
    ):
        return None
    return final_content


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


__all__ = [
    "ToolSuspension", "ValidatedToolResume", "InvalidResume", "ResumeConflict",
    "reconcile_validated_tool_resume", "public_suspension_projection",
    "SUSPENSION_PRIVATE_PAYLOAD_KEYS",
]
