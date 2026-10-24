"""Unit tests: Track M fast-lane profile routing on the gateway/run.py path.

Task t_f4f7bc58 — ordinary PWA request WITHOUT a trigger must NOT go to the
heavy 01-orchestrator full-lane profile: it routes to the fast profile
(11-fast-answers). Requests WITH a trigger („подумай“ / „гигантская задача“)
keep the current heavy behavior (stay on the current profile, full lane).

The seam under test is :meth:`GatewayRunner._resolve_fast_lane_execute_profile`
(gateway/run.py), the Track M selector for the PWA/yadisk_bridge messenger
path. It is the run.py analogue of api_server's ``_run_agent`` Track M block
and is gated on the SAME ``agent.fast_lane`` decision functions, so the
trigger contract (fast_lane markers + RGZ) is identical.

Profile 11 is NOT modified by design (stays medium) — this test only proves
the SELECTION; the executor profile's config is unchanged.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.run import GatewayRunner


def _runner():
    """Bare GatewayRunner via object.__new__ (AGENTS.md pattern)."""
    obj = object.__new__(GatewayRunner)
    obj.config = type("C", (), {"multiplex_profiles": False})()
    return obj


class _FakePlatform:
    """Stand-in for a plugin Platform (e.g. yadisk_bridge).

    The real Platform("yadisk_bridge") pseudo-member only exists at runtime
    after the plugin registers via platform_registry; in a unit test the enum
    rejects the name. SessionSource.platform is only READ for its value here,
    so a lightweight object with .value is sufficient and keeps the test
    hermetic.
    """

    def __init__(self, value: str):
        self.value = value


_SUPPORTED = {
    "yadisk_bridge": _FakePlatform,
    "local": lambda v: Platform(v),
    "telegram": lambda v: Platform(v),
}


def _source(platform: str = "local"):
    factory = _SUPPORTED.get(platform, lambda v: v)
    plat = factory(platform)
    return SessionSource(
        platform=plat,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )


@pytest.fixture
def fl_on():
    """fast_lane enabled + route_to_profile on (production 01 config)."""
    with patch(
        "gateway.run._load_gateway_config",
        return_value={
            "fast_lane": {
                "enabled": True,
                "route_to_profile": True,
                "route_platforms": ["yadisk_bridge"],
            },
            "agent": {"reasoning_effort": "high"},
        },
    ):
        yield


@pytest.fixture
def fl_on_all_platforms():
    """fast_lane enabled + route_to_profile on, NO platform gate."""
    with patch(
        "gateway.run._load_gateway_config",
        return_value={
            "fast_lane": {
                "enabled": True,
                "route_to_profile": True,
                "route_platforms": None,
            },
            "agent": {"reasoning_effort": "high"},
        },
    ):
        yield


@pytest.fixture
def fl_off():
    """fast_lane disabled — routing must be fully inert."""
    with patch(
        "gateway.run._load_gateway_config",
        return_value={
            "fast_lane": {"enabled": False, "route_to_profile": True},
            "agent": {"reasoning_effort": "high"},
        },
    ):
        yield


@pytest.fixture
def fl_no_route():
    """fast_lane enabled but route_to_profile off — no routing."""
    with patch(
        "gateway.run._load_gateway_config",
        return_value={
            "fast_lane": {"enabled": True, "route_to_profile": False},
            "agent": {"reasoning_effort": "high"},
        },
    ):
        yield


# ---------------------------------------------------------------------------
# 1) Fast (no trigger) -> fast profile 11-fast-answers
# ---------------------------------------------------------------------------


def test_markerless_pwa_request_routes_to_fast_profile(fl_on):
    """An ordinary marker-less PWA (yadisk_bridge) request routes to 11-fast-answers."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "сколько будет 2+2?",
            _source(platform="yadisk_bridge"),
        )
        == "11-fast-answers"
    )


def test_markerless_local_request_not_routed_with_platform_gate(fl_on):
    """With route_platforms=[yadisk_bridge], a non-PWA source is NOT routed
    (keeps the current full-lane behavior)."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "сколько будет 2+2?",
            _source(platform="local"),
        )
        is None
    )


def test_markerless_telegram_not_routed_with_platform_gate(fl_on):
    """With the PWA platform gate, a Telegram source keeps the full lane."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "сколько будет 2+2?",
            _source(platform="telegram"),
        )
        is None
    )


def test_markerless_pwa_routes_without_platform_gate(fl_on_all_platforms):
    """With route_platforms unset (None), routing applies to any source —
    PWA included (design parity with api_server)."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "сколько будет 2+2?",
            _source(platform="yadisk_bridge"),
        )
        == "11-fast-answers"
    )
    assert (
        runner._resolve_fast_lane_execute_profile(
            "сколько будет 2+2?",
            _source(platform="telegram"),
        )
        == "11-fast-answers"
    )


def test_markerless_telegram_route_profile_gate_off(fl_no_route):
    """route_to_profile=false -> no routing (release-safe default)."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "сколько будет 2+2?",
            _source(platform="telegram"),
        )
        is None
    )


# ---------------------------------------------------------------------------
# 2) Slow (trigger) -> None, current profile (heavy behavior preserved)
# ---------------------------------------------------------------------------


def test_tg_podumay_trigger_stays_on_current_profile(fl_on):
    """«подумай» prefix is a SLOW marker -> no fast-profile routing (even on PWA)."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "подумай: разбери этот лог",
            _source(platform="yadisk_bridge"),
        )
        is None
    )


def test_giant_task_trigger_stays_on_current_profile(fl_on):
    """«гигантская задача» / RGZ-plan phrase -> no fast-profile routing (even on PWA)."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "Это гигантская задача, нужен декомпоз",
            _source(platform="yadisk_bridge"),
        )
        is None
    )


def test_deepthink_markerless_rg_and_phrase_trigger_stays(fl_on):
    """«гигантский план» -> always-slow, stays on current profile (even on PWA)."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "Составь гигантский план эвакуации",
            _source(platform="yadisk_bridge"),
        )
        is None
    )


# ---------------------------------------------------------------------------
# 3) Feature gates — inert when disabled / no route
# ---------------------------------------------------------------------------


def test_flag_off_never_routes(fl_off):
    """fast_lane.enabled=false -> no routing, current profile unchanged."""
    runner = _runner()
    assert (
        runner._resolve_fast_lane_execute_profile(
            "сколько будет 2+2?",
            _source(),
        )
        is None
    )


def test_source_required():
    """None source/message -> no routing (never raises)."""
    import gateway.run as run_mod

    obj = object.__new__(GatewayRunner)
    obj.config = type("C", (), {"multiplex_profiles": False})()

    # No fast_lane config at all (load returns {}).
    with patch("gateway.run._load_gateway_config", return_value={}):
        assert obj._resolve_fast_lane_execute_profile(None, None) is None
        assert obj._resolve_fast_lane_execute_profile("hi", None) is None
        assert obj._resolve_fast_lane_execute_profile(None, _source()) is None
