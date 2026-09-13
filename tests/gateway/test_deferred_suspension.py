"""Gateway projection for deliberately suspended agent turns."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SuspendedReply,
)
from gateway.run import (
    _clarify_send_then_wait_or_suspend,
    _deferred_clarify_suspension,
    _normalize_empty_agent_response,
    _suspended_reply_for_agent_result,
)
from tools.clarify_tool import clarify_tool
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.outcomes = []
        self.sent = []
        self.private_suspensions = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SimpleNamespace(success=True, message_id="sent-1")

    async def on_processing_complete(self, event, outcome):
        self.outcomes.append((outcome, getattr(event, "_hermes_suspension", None)))

    async def on_processing_suspended(self, event, suspension):
        self.private_suspensions.append(dict(suspension))


# Local import keeps the adapter declaration compact and mirrors plugin stubs.
from types import SimpleNamespace


def _event():
    return MessageEvent(
        text="do work",
        message_type=MessageType.TEXT,
        message_id="msg-1",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm"),
    )


def test_suspended_result_is_intentionally_silent_not_empty_response_error():
    result = {
        "suspended": True,
        "suspension": {
            "kind": "question_set",
            "payload": {"question_set_id": "qs-1"},
            "tool_name": "clarify",
            "tool_call_id": "call-1",
        },
        "api_calls": 1,
        "failed": False,
        "interrupted": False,
    }

    assert _normalize_empty_agent_response(result, "") == ""
    assert _suspended_reply_for_agent_result(result) == SuspendedReply(result["suspension"])


def test_suspended_reply_projection_fails_closed_for_malformed_metadata():
    malformed = {"suspended": True, "api_calls": 1}
    assert _suspended_reply_for_agent_result(malformed) is None
    assert "no response was generated" in _normalize_empty_agent_response(
        malformed, ""
    )
    assert _suspended_reply_for_agent_result(
        {"suspended": True, "suspension": "not-a-mapping"}
    ) is None


def test_default_adapter_does_not_opt_in_to_deferred_clarify():
    adapter = SimpleNamespace(supports_deferred_clarify=False)
    assert _deferred_clarify_suspension(
        adapter,
        question="Pick?",
        choices=["A", "B"],
        multi_select=False,
        questions=None,
        clarify_id="cid",
        session_key="sk",
    ) is None


def test_opted_in_single_question_has_stable_canonical_shape():
    suspension = _deferred_clarify_suspension(
        SimpleNamespace(supports_deferred_clarify=True),
        question="Proceed?",
        choices=["Yes", "No"],
        multi_select=False,
        questions=None,
        clarify_id="cid",
        session_key="sk",
    )

    assert suspension.payload["questions"] == [
        {
            "id": "q0",
            "type": "single_select",
            "question": "Proceed?",
            "choices": ["Yes", "No"],
            "multi_select": False,
        }
    ]


def test_opted_in_batch_is_one_atomic_suspension_and_clears_waiter():
    adapter = SimpleNamespace(supports_deferred_clarify=True)
    future = MagicMock()
    future.result.return_value = SimpleNamespace(success=True)
    clarify_mod = MagicMock()
    calls = []

    def deferred_callback(question, choices, multi_select=False, questions=None):
        calls.append(questions)
        suspension = _deferred_clarify_suspension(
            adapter,
            question=question,
            choices=choices,
            multi_select=multi_select,
            questions=questions,
            clarify_id="cid",
            session_key="sk",
        )
        return _clarify_send_then_wait_or_suspend(
            future,
            clarify_id="cid",
            session_key="sk",
            clarify_mod=clarify_mod,
            suspension=suspension,
        )

    result = clarify_tool(
        question="Need both",
        questions=[
            {"id": "target", "question": "Target?", "choices": ["A", "B"]},
            {"id": "notes", "question": "Notes?"},
        ],
        callback=deferred_callback,
    )

    assert len(calls) == 1
    assert result.kind == "question_set"
    assert result.payload["questions"] == [
        {
            "id": "target",
            "type": "single_select",
            "question": "Target?",
            "choices": ["A", "B"],
            "multi_select": False,
        },
        {
            "id": "notes",
            "type": "text",
            "question": "Notes?",
            "choices": None,
            "multi_select": False,
        },
    ]
    clarify_mod.clear_session.assert_called_once_with("sk")
    clarify_mod.wait_for_response.assert_not_called()


def test_ambiguous_send_does_not_transfer_deferred_ownership():
    import concurrent.futures

    future = MagicMock()
    future.result.side_effect = concurrent.futures.TimeoutError()
    clarify_mod = MagicMock()
    clarify_mod.get_clarify_timeout.return_value = 600
    clarify_mod.wait_for_response.return_value = "late answer"
    suspension = _deferred_clarify_suspension(
        SimpleNamespace(supports_deferred_clarify=True),
        question="Proceed?",
        choices=None,
        multi_select=False,
        questions=None,
        clarify_id="cid",
        session_key="sk",
    )

    assert _clarify_send_then_wait_or_suspend(
        future,
        clarify_id="cid",
        session_key="sk",
        clarify_mod=clarify_mod,
        suspension=suspension,
    ) == "late answer"
    clarify_mod.clear_session.assert_not_called()
    clarify_mod.wait_for_response.assert_called_once_with("cid", timeout=600.0)


def test_agent_result_projects_through_base_adapter_without_delivery():
    from agent.suspension import public_suspension_projection

    adapter = _Adapter()
    event = _event()
    suspension = {
        "kind": "question_set",
        "payload": {"question_set_id": "qs-1", "session_key": "sekret"},
        "tool_name": "clarify",
        "tool_call_id": "call-1",
    }
    agent_result = {
        "suspended": True,
        "suspension": suspension,
        "session_id": "20260913_123456_deadbeef",
    }
    projected = _suspended_reply_for_agent_result(agent_result)
    adapter._message_handler = AsyncMock(return_value=projected)
    adapter._active_sessions["test-session"] = __import__("asyncio").Event()

    asyncio.run(adapter._process_message_background(event, "test-session"))

    assert adapter.sent == []
    # The SUSPENDED outcome reports the bounded public shape: the private
    # routing envelope (session_key, tool_call_id) must not reach the event.
    assert adapter.outcomes == [
        (ProcessingOutcome.SUSPENDED, public_suspension_projection(suspension))
    ]
    expected_private = dict(suspension, core_session_id="20260913_123456_deadbeef")
    assert adapter.private_suspensions == [expected_private]


# ---------------------------------------------------------------------------
# P1: fail-open gateway projection. Anything short of an unambiguous,
# schema-valid suspension must project to None (fail open to the normal
# empty-response/error path), never a SuspendedReply.
# ---------------------------------------------------------------------------

def _valid_suspension_dict():
    return {
        "kind": "question_set",
        "payload": {"question_set_id": "qs-1", "session_key": "sekret"},
        "tool_name": "clarify",
        "tool_call_id": "call-1",
    }


def test_suspended_must_be_exactly_true():
    for bad in (True,):  # the valid case flows below
        assert _suspended_reply_for_agent_result(
            {"suspended": bad, "suspension": _valid_suspension_dict()}
        ) == SuspendedReply(_valid_suspension_dict())
    for bad in ("yes", "", 1, "suspended", [], ("suspended",), {"s": 1}):
        assert _suspended_reply_for_agent_result(
            {"suspended": bad, "suspension": _valid_suspension_dict()}
        ) is None


def test_missing_suspension_or_non_dict_rejected():
    for suspended_value in (True,):
        assert _suspended_reply_for_agent_result({"suspended": True}) is None
        assert _suspended_reply_for_agent_result(
            {"suspended": True, "suspension": ["not", "a", "dict"]}
        ) is None
        assert _suspended_reply_for_agent_result(
            {"suspended": True, "suspension": None}
        ) is None
        assert _suspended_reply_for_agent_result(
            {"suspended": suspended_value, "suspension": "json-string"}
        ) is None


def test_empty_or_whitespace_kind_rejected():
    for kind in ("", "   ", None, 0):
        d = _valid_suspension_dict()
        d["kind"] = kind
        assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_extra_unknown_top_level_keys_rejected():
    d = _valid_suspension_dict()
    d["extra"] = "leak"
    assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_missing_required_schema_keys_rejected():
    for missing in ("kind", "payload", "tool_name", "tool_call_id"):
        d = _valid_suspension_dict()
        d.pop(missing)
        assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_non_str_tool_name_or_call_id_rejected():
    for key in ("tool_name", "tool_call_id"):
        d = _valid_suspension_dict()
        d[key] = 123
        assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_non_mapping_payload_rejected():
    for payload in ([], "str", 7, object()):
        d = _valid_suspension_dict()
        d["payload"] = payload
        assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_non_json_serializable_payload_rejected():
    d = _valid_suspension_dict()
    d["payload"] = {"bad": object()}
    assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_nan_payload_rejected():
    d = _valid_suspension_dict()
    d["payload"] = {"nan": float("nan")}
    assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_oversized_payload_rejected():
    d = _valid_suspension_dict()
    d["payload"] = {"blob": "x" * (300 * 1024)}
    assert _suspended_reply_for_agent_result({"suspended": True, "suspension": d}) is None


def test_conflicted_flags_reject_suspension():
    base = {"suspended": True, "suspension": _valid_suspension_dict()}
    for flag in ("failed", "interrupted"):
        malformed = dict(base, **{flag: True})
        assert _suspended_reply_for_agent_result(malformed) is None
    # A non-empty final_response alongside suspension is a conflict too.
    malformed = dict(base, final_response="I actually answered.")
    assert _suspended_reply_for_agent_result(malformed) is None
    # But the same turn with an EMPTY/None final_response is a clean suspend.
    assert _suspended_reply_for_agent_result(dict(base, final_response="")) == SuspendedReply(
        _valid_suspension_dict()
    )


def test_verify_suspended_true_project_still_green():
    # The existing passing projection must keep working after hardening.
    assert _suspended_reply_for_agent_result(
        {"suspended": True, "suspension": _valid_suspension_dict()}
    ) == SuspendedReply(_valid_suspension_dict())


# ---------------------------------------------------------------------------
# P2: the private routing envelope (session_key, tool_call_id) must never
# surface in the observer/public projection.
# ---------------------------------------------------------------------------

def test_public_suspension_projection_drops_routing_secrets():
    from agent.suspension import public_suspension_projection

    private = {
        "kind": "question_set",
        "payload": {
            "question_set_id": "qs-1",
            "session_key": "sekret-routing-token",
            "revision": 1,
            "questions": [{"id": "q0", "question": "Proceed?"}],
        },
        "tool_name": "clarify",
        "tool_call_id": "call-1",
    }
    public = public_suspension_projection(private)

    assert public["kind"] == "question_set"
    assert "session_key" not in public["payload"]
    assert "tool_call_id" not in public
    assert "tool_name" not in public
    # Non-secret metadata survives the sanitization.
    assert public["payload"]["question_set_id"] == "qs-1"
    assert public["payload"]["questions"] == [{"id": "q0", "question": "Proceed?"}]


def test_public_suspension_projection_is_defensive():
    from agent.suspension import public_suspension_projection

    assert public_suspension_projection({"kind": "question_set"}) == {"kind": "question_set"}
    assert public_suspension_projection("not-a-dict") == {}
