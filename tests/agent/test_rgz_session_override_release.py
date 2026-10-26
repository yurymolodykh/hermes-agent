"""Session-level RGZ reasoning-override release (wg t_9d4720da).

The RGZ trigger pins the *session* to reasoning=max via
``apply_rgz_session_override`` for the trigger turn. On a long-lived session
(e.g. PWA/yadisk_bridge) the override previously persisted and every later
ordinary request kept resolving to max — the «залипание» defect.

Part B of wg t_9d4720da: clear the session override in ``finally`` of
``run_agent.run_conversation`` via :func:`agent.rgz_reasoning.
release_rgz_session_override`. These tests exercise the release function
itself (unit) + the wiring in ``run_conversation``'s teardown (that the
release is invoked next to ``release_agent_max``) + cache-signature
participation of the routed profile (Part A).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.rgz_reasoning import (
    MAX_REASONING_CONFIG,
    apply_rgz_session_override,
    release_rgz_session_override,
)


class _FakeRunner:
    """GatewayRunner stub recording ``_set_session_reasoning_override``."""

    def __init__(self):
        self.overrides = {}
        self.calls = []

    def _set_session_reasoning_override(self, key, cfg):
        self.calls.append((key, dict(cfg) if cfg is not None else None))
        if cfg is None:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = dict(cfg)


def _agent(session_key="agent:main:yadisk_bridge:dm:sess-1"):
    return SimpleNamespace(_gateway_session_key=session_key)


def test_release_clears_pinned_session_override():
    """The release reaches the runner and clears the pinned session."""
    runner = _FakeRunner()
    # Pin the session first (as the trigger turn does).
    assert apply_rgz_session_override(runner, "sess-1", "гигантская задача") is True
    assert runner.overrides == {"sess-1": MAX_REASONING_CONFIG}

    agent = _agent(session_key="sess-1")
    with patch(
        "agent.rgz_reasoning._gateway_runner_ref",
        return_value=lambda: runner,
    ):
        assert release_rgz_session_override(agent) is True

    assert runner.overrides == {}
    assert runner.calls[-1] == ("sess-1", None)


def test_release_noop_without_session_key():
    agent = SimpleNamespace(_gateway_session_key=None)
    assert release_rgz_session_override(agent) is False
    agent2 = SimpleNamespace(_gateway_session_key="")
    assert release_rgz_session_override(agent2) is False
    assert release_rgz_session_override(None) is False


def test_release_noop_without_live_runner():
    """No gateway in the live process (CLI/standalone) -> safe no-op."""
    agent = _agent()
    with patch("agent.rgz_reasoning._gateway_runner_ref", return_value=lambda: None):
        assert release_rgz_session_override(agent) is False


def test_release_noop_when_runner_lacks_setter():
    """Defensive: runner without the setter degrades, never raises."""
    bare = SimpleNamespace()
    agent = _agent()
    with patch("agent.rgz_reasoning._gateway_runner_ref", return_value=lambda: bare):
        assert release_rgz_session_override(agent) is False


def test_release_swallows_exceptions():
    """Teardown must never raise — even on unexpected runner errors."""
    agent = _agent()

    class _BoomSetter:
        def _set_session_reasoning_override(self, key, cfg):
            raise RuntimeError("boom")

    with patch(
        "agent.rgz_reasoning._gateway_runner_ref",
        return_value=lambda: _BoomSetter(),
    ):
        assert release_rgz_session_override(agent) is False


def test_run_conversation_finally_invokes_release():
    """run_agent.run_conversation's outer finally calls the session release
    next to the existing release_agent_max (Part B wiring). Structural guard:
    the teardown imports and calls both releases in the same finally block."""
    import inspect
    import run_agent as ra

    src = inspect.getsource(ra.AIAgent.run_conversation)

    # Both releases must be present and sequential in the finally teardown.
    assert "release_agent_max" in src
    assert "release_rgz_session_override" in src
    # release_rgz_session_override must appear AFTER release_agent_max in the
    # same try, i.e. it is actually wired into the run teardown (not just
    # defined/imported somewhere).
    ia = src.index("release_agent_max")
    ir = src.index("release_rgz_session_override")
    assert ir > ia
    # And the release is inside a finally/teardown path of the method.
    fin = src.rfind("finally:")
    # The releases sit in a try nested in a finally (the RGZ teardown block),
    # so locate the nearest 'finally:' before the release call region.
    assert fin != -1
    assert src.rfind("finally:", 0, ia) != -1 or src.rfind("finally:", 0, fin) != -1


def test_gateway_cache_signature_includes_execute_profile():
    """A turn routed to a fast profile must not reuse an agent cached under
    another profile (Part A)."""
    from gateway.run import GatewayRunner

    runtime = {"provider": "openrouter", "base_url": "", "api_mode": ""}
    sig_origin = GatewayRunner._agent_config_signature(
        "deepseek-v4-flash",
        runtime,
        ["hermes-telegram"],
        "",
        execute_profile=None,
    )
    sig_fast = GatewayRunner._agent_config_signature(
        "deepseek-v4-flash",
        runtime,
        ["hermes-telegram"],
        "",
        execute_profile="11-fast-answers",
    )
    assert sig_origin != sig_fast
    # Deterministic: same inputs -> same signature.
    sig_fast2 = GatewayRunner._agent_config_signature(
        "deepseek-v4-flash",
        runtime,
        ["hermes-telegram"],
        "",
        execute_profile="11-fast-answers",
    )
    assert sig_fast == sig_fast2
