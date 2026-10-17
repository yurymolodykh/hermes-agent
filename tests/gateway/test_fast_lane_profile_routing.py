"""Fast-lane profile routing: markerless requests execute under 11-fast-answers.

Task t_746614d5 — fast requests (no slow markers) are executed by the
11-fast-answers profile instead of a reduced-diet run of the current profile;
slow requests („подумай“ / „гигантская задача“ / OWUI deepthink) stay on the
full lane (default 01-orchestrator). Design §4.2 Track M.

Covers:

  * :func:`agent.fast_lane.route_profile` — pure selector: fast -> fast
    profile, slow -> slow profile (None = current/default unchanged).
  * :meth:`APIServerAdapter._run_agent` routing seam — when
    ``fast_lane.route_to_profile`` (or the explicit ``fast_lane_route=True``
    override) is active, a markerless request enters the fast profile scope;
    a slow request stays on the origin profile; a fast-profile failure falls
    back to the origin profile (Ж1 „деградация вместо ошибки“, §6.1).

The heavy io (SessionDB, _create_agent, provider creds) is mocked; only the
routing decision + scope + fallback wiring is exercised.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from contextlib import contextmanager

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


@contextmanager
def _scoped(profile):
    yield profile


class TestRouteProfile:
    def test_fast_request_routes_to_fast_profile(self):
        from agent.fast_lane import FastLaneDecision, route_profile

        decision = FastLaneDecision(slow_mode=False)
        assert route_profile(decision) == "11-fast-answers"

    def test_slow_request_routes_to_slow_profile(self):
        from agent.fast_lane import FastLaneDecision, route_profile

        decision = FastLaneDecision(slow_mode=True, source="phrase")
        # slow_profile=None default -> None means "use current/default profile"
        # (in the live gateway the default IS 01-orchestrator, the full lane).
        assert route_profile(decision) is None

    def test_slow_request_explicit_slow_profile(self):
        from agent.fast_lane import FastLaneDecision, route_profile

        decision = FastLaneDecision(slow_mode=True, source="phrase")
        assert (
            route_profile(decision, slow_profile="01-orchestrator")
            == "01-orchestrator"
        )

    def test_slow_request_with_null_slow_profile_returns_none(self):
        from agent.fast_lane import FastLaneDecision, route_profile

        decision = FastLaneDecision(slow_mode=True)
        # slow_profile=None default -> use current request profile unchanged
        assert route_profile(decision, slow_profile=None) is None

    def test_fast_profile_customizable(self):
        from agent.fast_lane import FastLaneDecision, route_profile

        decision = FastLaneDecision(slow_mode=False)
        assert route_profile(decision, fast_profile="my-fast") == "my-fast"


class TestRunAgentProfileRouting:
    @pytest.mark.asyncio
    async def test_fast_request_routes_to_fast_profile(self, monkeypatch):
        """Markerless request with routing on -> fast profile scope entered."""
        adapter = _make_adapter()
        called_profiles: list = []

        @contextmanager
        def fake_scope(profile):
            called_profiles.append(profile)
            yield

        monkeypatch.setattr(adapter, "_profile_scope", staticmethod(fake_scope))
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0
        with patch.object(adapter, "_create_agent", return_value=mock_agent):
            await adapter._run_agent(
                user_message="сколько будет 2+2?",
                conversation_history=[],
                session_id="s-fast",
                fast_lane_route=True,
            )

        # The fast-request turn entered the 11-fast-answers scope.
        assert called_profiles, "no profile scope was entered"
        assert called_profiles[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_slow_request_stays_on_origin_profile(self, monkeypatch):
        """Deepthink / slow-marker request -> origin (default) profile scope."""
        adapter = _make_adapter()
        called_profiles: list = []

        @contextmanager
        def fake_scope(profile):
            called_profiles.append(profile)
            yield

        monkeypatch.setattr(adapter, "_profile_scope", staticmethod(fake_scope))
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0
        with patch.object(adapter, "_create_agent", return_value=mock_agent):
            await adapter._run_agent(
                user_message="гигантская задача: спланируй миграцию базы",
                conversation_history=[],
                session_id="s-slow",
                fast_lane_route=True,
            )

        # slow -> origin (None here because _api_request_profile unset -> default)
        assert called_profiles, "no profile scope was entered"
        assert called_profiles[0] is None or called_profiles[0] == "01-orchestrator"

    @pytest.mark.asyncio
    async def test_routing_disabled_by_default(self, monkeypatch):
        """Without route_to_profile / fast_lane_route, no re-scope happens and
        the request runs under the current profile (Track S unchanged)."""
        adapter = _make_adapter()
        called_profiles: list = []

        @contextmanager
        def fake_scope(profile):
            called_profiles.append(profile)
            yield

        monkeypatch.setattr(adapter, "_profile_scope", staticmethod(fake_scope))
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0
        with patch.object(adapter, "_create_agent", return_value=mock_agent):
            await adapter._run_agent(
                user_message="сколько будет 2+2?",
                conversation_history=[],
                session_id="s-default",
                # fast_lane_route not given -> config gate decides (default off)
            )

        # No fast-profile routing: only the original (unscoped) profile runs.
        assert called_profiles, "no profile scope was entered"
        assert called_profiles[0] is None

    @pytest.mark.asyncio
    async def test_fast_profile_failure_falls_back_to_origin(self, monkeypatch):
        """Ж1: a crash building/running under the fast profile falls back to
        the origin profile and returns the fallback result."""
        adapter = _make_adapter()
        called_profiles: list = []

        @contextmanager
        def fake_scope(profile):
            called_profiles.append(profile)
            yield

        monkeypatch.setattr(adapter, "_profile_scope", staticmethod(fake_scope))

        ok_agent = MagicMock()
        ok_agent.run_conversation.return_value = {"final_response": "fallback ok"}
        ok_agent.session_prompt_tokens = 0
        ok_agent.session_completion_tokens = 0
        ok_agent.session_total_tokens = 0

        def _create_agent_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("11-fast-answers is down")
            return ok_agent

        call_count = 0
        with patch.object(adapter, "_create_agent", side_effect=_create_agent_side_effect):
            result, _ = await adapter._run_agent(
                user_message="сколько будет 2+2?",
                conversation_history=[],
                session_id="s-fall",
                fast_lane_route=True,
            )

        assert result["final_response"] == "fallback ok"
        # First attempt under 11, fallback under origin (None/default).
        assert called_profiles[0] == "11-fast-answers"
        assert len(called_profiles) == 2
