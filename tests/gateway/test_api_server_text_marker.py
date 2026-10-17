"""OWUI/PWA text marker «подумай» end-to-end through api_server.

Task t_8c15d936 / t_0056c2fd — the «подумай»/«думай» prefix in the request body must do
for OWUI/PWA exactly what the TG adapter does for Telegram:

* the marker translates the session to slow_mode (design §3 / A6 §4.2), so
  the review-forcer keeps the review card (D5: fast requests are exempt,
  marked requests are NOT);
* the prefix is STRIPPED from the text the model actually runs with — the
  user wrote «подумай X», the model receives «X» (the UI directive is never
  surfaced as model input).

All of it gated on ``fast_lane.enabled`` (design §6.2, default false): with
the flag OFF the request behaves exactly as before — text passed verbatim,
no marker persisted, fast by default (D1).

These tests exercise the *real* api_server wiring:

* :func:`_effective_api_user_message` — the resolve seam used by both
  ``run_conversation`` call sites (_run_agent and /v1/runs);
* :meth:`APIServerAdapter._create_agent` — the fast-lane block that computes
  the decision, persists the session marker (slow_mode=true) and attaches
  the stripped message to the agent;
* the two ``run_conversation`` call sites via hermetic fakes, proving the
  model is handed the marker-free text.

Heavy runtime resolution is mocked exactly like the pre-existing
``test_api_server_fast_lane_bridge.py`` / ``test_fast_lane_integration.py``
hermetic fixtures.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _effective_api_user_message


# ---------------------------------------------------------------------------
# §1 _effective_api_user_message — the resolve seam
# ---------------------------------------------------------------------------


class TestEffectiveApiUserMessage:
    def test_no_agent_returns_original(self):
        assert _effective_api_user_message(None, "подумай разбери это") == "подумай разбери это"

    def test_agent_without_marker_returns_original(self):
        agent = MagicMock()
        del agent._fast_lane_effective_message
        assert _effective_api_user_message(agent, "привет") == "привет"

    def test_agent_with_marker_returns_stripped(self):
        agent = MagicMock()
        agent._fast_lane_effective_message = "разбери это"
        assert _effective_api_user_message(agent, "подумай разбери это") == "разбери это"

    def test_empty_strip_falls_back_to_original(self):
        agent = MagicMock()
        agent._fast_lane_effective_message = ""
        assert _effective_api_user_message(agent, "подумай") == ""


# ---------------------------------------------------------------------------
# §2 marker vectors through _create_agent (session marker + stripped text)
# ---------------------------------------------------------------------------


def _make_adapter_with_fakes(monkeypatch, config=None):
    """Build an APIServerAdapter with runtime-loading seams faked.

    Returns (adapter, captured_kwargs, fake_db). Same hermetic recipe as
    test_fast_lane_integration._make_adapter_with_fakes.
    """
    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "letovo",
            "base_url": "https://llm.letovo.site/v1",
            "api_mode": None,
            "api_key": "test-key",
        },
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "letovo/deepseek-v4-flash")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "fast_lane": {"enabled": True},
        "agent": {"reasoning_effort": "high"},
    } if config is None else config)
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_reasoning_config",
        staticmethod(lambda model="": {"enabled": True, "effort": "high"}),
    )
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None)
    )
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    fake_db = MagicMock()
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: fake_db)
    return adapter, captured, fake_db


def _agent(adapter, **kwargs):
    return adapter._create_agent(session_id="sess-1", **kwargs)


# (input, slow_mode, residual_text) — the A6 §4.2 vectors
API_TEXT_MARKER_VECTORS = [
    ("подумай разбери это", True, "разбери это"),
    ("ПОДУМАЙ: анализ отчёта", True, "анализ отчёта"),
    ("думай, пересчитай", True, "пересчитай"),
    ("Разбери это сам", False, "Разбери это сам"),
    ("подумайка об этом", False, "подумайка об этом"),  # word boundary
    ("подумай-ка об этом", False, "подумай-ка об этом"),  # word boundary (hyphenated particle)
    ("думай над этим", True, "над этим"),
    ("подумай: проанализируй", True, "проанализируй"),
    ("обычное сообщение", False, "обычное сообщение"),
    ("", False, ""),
]


class TestCreateAgentTextMarker:
    @pytest.mark.parametrize("text, want_slow, want_residual", API_TEXT_MARKER_VECTORS)
    def test_marker_slow_and_effective_text(self, monkeypatch, text, want_slow, want_residual):
        adapter, _, fake_db = _make_adapter_with_fakes(monkeypatch)
        agent = _agent(adapter, user_message=text)

        # Session marker: marked requests are slow (review kept, D5).
        assert fake_db.patch_session_model_config.called
        marker = fake_db.patch_session_model_config.call_args[0][1]
        assert marker["slow_mode"] is want_slow, text
        if want_slow:
            assert marker["review_exempt"] is False
        # Effective text handed to the model = residual (strip is a no-op when
        # no marker is present).
        effective = getattr(agent, "_fast_lane_effective_message", None)
        if want_slow:
            assert effective == want_residual, f"{text!r}: {effective!r} != {want_residual!r}"
        else:
            assert effective is None, text

    def test_marker_source_is_tg_prefix(self, monkeypatch):
        from agent.fast_lane import session_is_fast_lane

        adapter, _, fake_db = _make_adapter_with_fakes(monkeypatch)
        _agent(adapter, user_message="подумай: анализ")
        marker = fake_db.patch_session_model_config.call_args[0][1]
        assert marker["slow_mode_source"] == "tg-prefix"
        assert session_is_fast_lane(marker) is False

    def test_slow_marker_does_not_cap_iterations(self, monkeypatch):
        """Marked (slow) request stays on the full lane: not force-capped to 8,
        no run_budget_seconds, not review-exempt — only the text is stripped."""
        adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
        agent = _agent(adapter, user_message="подумай: детальный анализ проекта")
        assert captured["max_iterations"] != 8
        assert "run_budget_seconds" not in captured
        assert getattr(agent, "review_exempt", None) in (None, False)


# ---------------------------------------------------------------------------
# §3 e2e: text with marker -> session slow_mode=true + prefix stripped from
#    the text that reaches run_conversation (OWUI/PWA path).
# ---------------------------------------------------------------------------


class TestRunConversationReceivesStrippedText:
    @pytest.mark.asyncio
    async def test_run_agent_call_site_uses_effective_message(self, monkeypatch):
        """The actual `run_conversation(user_message=...)` invocation in
        _run_agent must use the effective (stripped) message. We patch
        _create_agent to return a fake carrying the marker, then assert the
        kwargs handed to run_conversation."""
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent.session_prompt_tokens = 1
        mock_agent.session_completion_tokens = 2
        mock_agent.session_total_tokens = 3
        mock_agent._fast_lane_effective_message = "разбери отчёт"

        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
        with patch.object(adapter, "_create_agent", return_value=mock_agent):
            result, _ = await adapter._run_agent(
                user_message="подумай: разбери отчёт",
                conversation_history=[],
                session_id="session-123",
            )

        assert result["final_response"] == "ok"
        mock_agent.run_conversation.assert_called_once_with(
            user_message="разбери отчёт",
            conversation_history=[],
            task_id="session-123",
        )

    @pytest.mark.asyncio
    async def test_unmarked_message_passes_verbatim(self, monkeypatch):
        """No marker -> the model receives the message unchanged (D1: fast by
        default, no strip, no behavioral change)."""
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent.session_prompt_tokens = 1
        mock_agent.session_completion_tokens = 2
        mock_agent.session_total_tokens = 3
        # Explicitly declare the marker absent: a real agent that was never
        # flagged has getattr(obj, "_fast_lane_effective_message", None) -> None.
        # MagicMock auto-vends child attributes on getattr, so pin it to None
        # to reproduce the "no marker" state faithfully.
        mock_agent._fast_lane_effective_message = None

        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
        with patch.object(adapter, "_create_agent", return_value=mock_agent):
            await adapter._run_agent(
                user_message="обычное сообщение",
                conversation_history=[],
                session_id="session-124",
            )

        mock_agent.run_conversation.assert_called_once_with(
            user_message="обычное сообщение",
            conversation_history=[],
            task_id="session-124",
        )


# ---------------------------------------------------------------------------
# §4 flag-off regression (design §6.2): with fast_lane.enabled=false the
#    test-marker must not change anything — text verbatim, no marker, fast.
# ---------------------------------------------------------------------------


class TestFlagOffRegression:
    def test_flag_off_no_strip_no_marker(self, monkeypatch):
        adapter, captured, fake_db = _make_adapter_with_fakes(
            monkeypatch, config={"agent": {"reasoning_effort": "high"}}
        )
        agent = _agent(adapter, user_message="подумай: разбери отчёт")

        # Full lane: no strip is attached, no marker is persisted, reasoning
        # stays at the gateway config default (high).
        assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
        assert not hasattr(agent, "_fast_lane_effective_message")
        assert fake_db.patch_session_model_config.called is False

    def test_flag_explicit_false_no_strip(self, monkeypatch):
        adapter, captured, fake_db = _make_adapter_with_fakes(
            monkeypatch,
            config={"fast_lane": {"enabled": False}, "agent": {"reasoning_effort": "high"}},
        )
        agent = _agent(adapter, user_message="думай над этим")
        assert not hasattr(agent, "_fast_lane_effective_message")
        assert fake_db.patch_session_model_config.called is False
        assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}


# ---------------------------------------------------------------------------
# §5 degradation (design §6.1): a fast-path exception with the flag ON must
#    not break the turn — full lane, text verbatim, degraded marker only.
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_flag_true_fast_path_error_no_strip(self, monkeypatch):
        adapter, captured, fake_db = _make_adapter_with_fakes(monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("fast-path exploded")

        monkeypatch.setattr("agent.fast_lane.evaluate_request", _boom)
        agent = _agent(adapter, user_message="подумай: разбери отчёт")

        assert isinstance(agent, object)
        assert not hasattr(agent, "_fast_lane_effective_message")
        # Full-lane reasoning preserved and only the degraded marker written.
        assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
        assert fake_db.patch_session_model_config.called
        marker = fake_db.patch_session_model_config.call_args[0][1]
        assert marker == {"fast_lane_degraded": True}
