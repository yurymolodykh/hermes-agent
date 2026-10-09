"""The memory bridge must only cite a bound user message of this exact turn."""
import hashlib
from types import SimpleNamespace

from agent.background_review import bind_memory_source_turn, build_memory_write_metadata


def _agent():
    return SimpleNamespace(
        session_id="session-1", _parent_session_id=None, platform="cli",
        _current_turn_id="turn-1", _memory_source_turn=None,
    )


def test_memory_source_locator_is_exact_current_user_turn():
    agent = _agent()
    message = "First user request, including üñîcode"
    bind_memory_source_turn(agent, message, 4)
    expected = f"turn:4:sha256:{hashlib.sha256(message.encode('utf-8')).hexdigest()}"
    assert build_memory_write_metadata(agent)["message_id"] == expected
    assert build_memory_write_metadata(agent)["session_id"] == "session-1"
    agent._current_turn_id = "turn-2"
    assert "message_id" not in build_memory_write_metadata(agent)
    agent._current_turn_id = "turn-1"
    agent.session_id = "session-2"
    assert "message_id" not in build_memory_write_metadata(agent)
    agent.session_id = "session-1"
    assert "message_id" not in build_memory_write_metadata(
        agent, execution_context="background_review"
    )
    bind_memory_source_turn(agent, "", 5)
    assert "message_id" not in build_memory_write_metadata(agent)
    bind_memory_source_turn(agent, ["image"], 6)
    assert "message_id" not in build_memory_write_metadata(agent)
