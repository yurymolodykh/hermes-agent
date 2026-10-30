"""t_e305207e — messenger (gateway/run.py) acceptance at the PROFILE level.

Why this file exists
--------------------
``tests/gateway/test_fast_lane_session_sticky_wiring.py`` drives the messenger
selector with ``_resolve_fast_lane_execute_profile`` and asserts it returns
``None`` for a sticky session. ``None`` means "keep the current request
profile" — it is NOT proof that the turn executes on ``01-orchestrator``, which
is what the card's acceptance criterion literally requires:

    "Either the messenger path routes marker-less follow-ups in a slow session
     to ``01-orchestrator`` ..."

Nothing in the messenger half of that suite asserted the CONCRETE profile the
executor scope receives, so a regression that resolved ``None`` to the wrong
home (or skipped the scope entirely) would have passed. These tests assert the
scope by value, on the real ``_run_agent`` control flow, through a real
``SessionDB`` written by the real production marker writer.

Kept in a separate module on purpose: the sibling file already carries a
documented concurrency hotspot (see the card comment on t_e305207e).
"""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import gateway.run as gateway_run


_CFG_ON = {
    "fast_lane": {
        "enabled": True,
        "route_to_profile": True,
        "route_platforms": ["yadisk_bridge"],
    },
}
_CFG_OFF = {"fast_lane": {"enabled": False}}


class _FakePlatform:
    def __init__(self, value: str):
        self.value = value


def _messenger_source(platform: str = "yadisk_bridge"):
    from gateway.session import SessionSource

    return SessionSource(
        platform=_FakePlatform(platform),
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )


def _real_session_db(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-sticky", source="yadisk_bridge")
    db.create_session("sess-clean", source="yadisk_bridge")
    return db


def _write_marker(db, session_id: str, *, slow: bool) -> None:
    """Write through the REAL production writer, not a hand-built dict."""
    from agent.fast_lane import FastLaneDecision, patch_session_fast_lane_marker

    patch_session_fast_lane_marker(
        db, session_id, FastLaneDecision(slow_mode=slow), preserve_sticky=True
    )


@contextmanager
def _capture_sync_scope(scopes: list):
    """Record the profile home handed to the (sync) runtime scope switch."""

    @contextmanager
    def _recorder(profile_home):
        scopes.append(Path(str(profile_home)).name)
        yield

    yield _recorder


async def _drive(runner, message, session_id):
    """Run the real ``_run_agent`` and return (executor scopes, inner calls)."""
    scopes: list = []
    inner: list = []

    async def _inner(*args, **kwargs):
        inner.append(kwargs.get("session_id") or (args[4] if len(args) > 4 else None))
        return {"final_response": "stub"}

    def _home_for_source(self, source):
        # The messenger has no /p/<profile>/ prefix here: the origin home is
        # the active profile, which is 01-orchestrator in the live gateway.
        return Path("C:/fake/profiles/01-orchestrator")

    with _capture_sync_scope(scopes) as recorder, patch.object(
        gateway_run, "_profile_runtime_scope", recorder
    ), patch.object(
        gateway_run.GatewayRunner, "_resolve_profile_home_for_source", _home_for_source
    ), patch.object(
        gateway_run.GatewayRunner, "_run_agent_inner", _inner
    ), patch.object(
        gateway_run.GatewayRunner, "_get_proxy_url", lambda self: None
    ):
        await runner._run_agent(
            message=message,
            context_prompt="",
            history=[],
            source=_messenger_source(),
            session_id=session_id,
            run_generation=1,
        )
    return scopes, inner


def _bare_runner(db):
    from gateway.run import GatewayRunner

    obj = object.__new__(GatewayRunner)
    obj.config = type("C", (), {"multiplex_profiles": True})()
    obj._session_db = db
    return obj


class TestMessengerStickyExecutorProfile:
    """The card's acceptance criterion, asserted on the concrete profile."""

    @pytest.mark.asyncio
    async def test_markerless_turn_in_slow_session_executes_on_01(self, tmp_path):
        """AC: marker-less follow-up in a slowed session runs on 01-orchestrator."""
        db = _real_session_db(tmp_path)
        try:
            _write_marker(db, "sess-sticky", slow=True)
            runner = _bare_runner(db)
            with patch("gateway.run._load_gateway_config", return_value=_CFG_ON):
                scopes, inner = await _drive(runner, "а что дальше?", "sess-sticky")
        finally:
            db.close()

        assert inner, "the turn never reached _run_agent_inner"
        assert scopes, "no executor profile scope was entered"
        assert scopes == ["01-orchestrator"], (
            "a sticky session did not execute on 01-orchestrator: "
            f"scopes={scopes}"
        )

    @pytest.mark.asyncio
    async def test_clean_session_executes_on_11(self, tmp_path):
        """Regression: an unmarked session still executes on 11-fast-answers."""
        db = _real_session_db(tmp_path)
        try:
            runner = _bare_runner(db)
            with patch("gateway.run._load_gateway_config", return_value=_CFG_ON):
                scopes, inner = await _drive(runner, "2+2?", "sess-clean")
        finally:
            db.close()

        assert inner, "the turn never reached _run_agent_inner"
        assert scopes == ["11-fast-answers"], (
            f"a clean session did not execute on the fast profile: scopes={scopes}"
        )

    @pytest.mark.asyncio
    async def test_sticky_write_then_markerless_turn_is_01(self, tmp_path):
        """End-to-end: a marker turn makes the NEXT marker-less turn go to 01.

        Uses only production writers/readers on a real store — no seeded rows.
        """
        from agent.fast_lane import (
            decide_slow_mode,
            patch_session_fast_lane_marker,
            session_slow_mode,
        )

        db = _real_session_db(tmp_path)
        try:
            # Turn 1: the user writes "подумай" -> slow decision -> marker.
            first = decide_slow_mode("подумай: разбери архитектуру")
            assert first.slow_mode is True
            patch_session_fast_lane_marker(
                db, "sess-sticky", first, preserve_sticky=True
            )
            assert session_slow_mode(db, "sess-sticky") is True

            # Turn 2: marker-less message, sticky must keep it on 01.
            runner = _bare_runner(db)
            with patch("gateway.run._load_gateway_config", return_value=_CFG_ON):
                scopes, _ = await _drive(runner, "продолжай", "sess-sticky")
        finally:
            db.close()

        assert scopes == ["01-orchestrator"], (
            f"sticky did not survive into the next turn: scopes={scopes}"
        )

    @pytest.mark.asyncio
    async def test_routing_disabled_keeps_the_single_origin_scope(self, tmp_path):
        """With fast-lane off, the messenger keeps exactly today's scope."""
        db = _real_session_db(tmp_path)
        try:
            _write_marker(db, "sess-sticky", slow=True)
            runner = _bare_runner(db)
            with patch("gateway.run._load_gateway_config", return_value=_CFG_OFF):
                scopes, inner = await _drive(runner, "2+2?", "sess-sticky")
        finally:
            db.close()

        assert inner, "the turn never reached _run_agent_inner"
        assert scopes == ["01-orchestrator"], (
            "flag off must not change the messenger execution scope: "
            f"scopes={scopes}"
        )
