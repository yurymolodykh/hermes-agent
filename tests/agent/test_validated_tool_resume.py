import pytest

from agent.suspension import (
    InvalidResume,
    ResumeConflict,
    ValidatedToolResume,
    reconcile_validated_tool_resume,
)


def _resume(**overrides):
    values = {
        "session_id": "session-1",
        "kind": "question_set",
        "tool_name": "clarify",
        "tool_call_id": "clarify-1",
        "result": {"answers": [{"answer": "A", "question_id": "q1"}]},
        "token_fingerprint": "sha256:deadbeef",
    }
    values.update(overrides)
    return ValidatedToolResume(**values)


def _tail(*, sibling=False):
    calls = [{
        "id": "clarify-1",
        "type": "function",
        "function": {"name": "clarify", "arguments": "{}"},
    }]
    if sibling:
        calls.append({
            "id": "later-1",
            "type": "function",
            "function": {"name": "terminal", "arguments": "{}"},
        })
    return [
        {"role": "user", "content": "help", "_row_id": 1},
        {"role": "assistant", "content": None, "tool_calls": calls, "_row_id": 2},
    ]


def test_validated_resume_is_strict_and_canonical():
    resume = _resume()
    assert resume.result == '{"answers":[{"answer":"A","question_id":"q1"}]}'
    with pytest.raises(InvalidResume):
        _resume(tool_call_id=" ")
    with pytest.raises(InvalidResume):
        _resume(tool_name="terminal")
    with pytest.raises(InvalidResume):
        _resume(result={"bad": float("nan")})


def test_reconcile_appends_only_trusted_result_and_core_cancellations():
    history = _tail(sibling=True)
    reconciled, appended = reconcile_validated_tool_resume(history, _resume())

    assert history[-1].get("content") is None
    assert [row["tool_call_id"] for row in appended] == ["clarify-1", "later-1"]
    assert appended[0]["content"] == _resume().result
    assert appended[1]["effect_disposition"] == "not_executed"
    assert "token" not in repr(appended)
    assert reconciled[:2] == history


def test_reconcile_rejects_changed_or_wrong_tail():
    with pytest.raises(ResumeConflict):
        reconcile_validated_tool_resume(
            _tail() + [{"role": "user", "content": "new work"}], _resume()
        )
    with pytest.raises(ResumeConflict):
        reconcile_validated_tool_resume(_tail(), _resume(tool_call_id="other"))
    with pytest.raises(ResumeConflict):
        reconcile_validated_tool_resume(
            _tail() + [{
                "role": "tool", "tool_call_id": "clarify-1",
                "tool_name": "clarify", "content": "different",
            }],
            _resume(),
        )


def test_reconcile_same_result_is_idempotent():
    resume = _resume()
    first, appended = reconcile_validated_tool_resume(_tail(), resume)
    second, retry_rows = reconcile_validated_tool_resume(first, resume)
    assert appended
    assert retry_rows == []
    assert second == first
