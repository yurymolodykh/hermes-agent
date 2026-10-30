"""Session-sticky slow mode at the gateway seam (t_e0350c47).

The unit contract lives in ``tests/agent/test_fast_lane_session_sticky.py``.
This file proves the two PRODUCTION call sites actually read the persisted
marker and hand it to the decision:

* ``APIServerAdapter._run_agent`` (api_server — OWUI/PWA/API): the sticky read
  happens in the ORIGIN profile scope, BEFORE the Track M executor scope
  switch, so a session slowed on 01 is never judged against the fast profile's
  store.
* ``GatewayRunner._resolve_fast_lane_execute_profile`` (gateway/run.py —
  messenger/PWA bridge): same contract on the sync path, plus the explicit
  ``fast_lane=ROUTE ... target=current`` log line so a sticky hit is visible.

Scenario coverage (card criteria 1-5):

1. marker turn -> slow; next marker-less turn in the SAME session -> slow;
2. a session with no markers still routes to 11-fast-answers (no regression);
3. «гигантская задача» -> sticky; 4. a sibling clean session is unaffected;
5. a broken/missing DB degrades to the old per-request behaviour, never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _StickyDB:
    """SessionDB-shaped fake carrying just the marker reader/writer."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def get_session_model_config_value(self, sid, key, default=None):
        return (self.rows.get(sid) or {}).get(key, default)

    def patch_session_model_config(self, sid, patch):
        cur = dict(self.rows.get(sid) or {})
        for k, v in patch.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        self.rows[sid] = cur


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


@contextmanager
def _recording_scope(sink):
    def _scope(profile):
        @contextmanager
        def _inner():
            sink.append(profile)
            yield

        return _inner()

    return _scope


def _ok_agent():
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "ok"}
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    return agent


async def _run(adapter, message, session_id, db):
    """Drive _run_agent with the DB + scope seams stubbed.

    ``adapter._session_db`` is the documented explicit override ("Explicit
    override (tests / manual wiring) wins"), and it is exactly the handle
    ``_read_sticky_marker`` prefers over the read-only file probe — so the
    test exercises the same precedence the production path uses without
    touching the real ``state.db`` or paying a cold schema init.
    """
    scopes: list = []

    @contextmanager
    def fake_scope(profile):
        scopes.append(profile)
        yield

    adapter._session_db = db

    with patch.object(adapter, "_profile_scope", staticmethod(fake_scope)), \
         patch.object(adapter, "_create_agent", return_value=_ok_agent()):
        await adapter._run_agent(
            user_message=message,
            conversation_history=[],
            session_id=session_id,
            fast_lane_route=True,
        )
    return scopes


# ---------------------------------------------------------------------------
# api_server: sticky decides the executor profile
# ---------------------------------------------------------------------------

class TestApiServerStickyRouting:
    @pytest.mark.asyncio
    async def test_sticky_session_stays_on_origin_profile(self):
        """Criterion 1: session slowed earlier -> marker-less turn stays on 01."""
        db = _StickyDB({"owui-1": {"slow_mode": True, "slow_mode_source": "phrase"}})
        adapter = _make_adapter()
        scopes = await _run(adapter, "сколько будет 2+2?", "owui-1", db)
        assert scopes, "no profile scope entered"
        # NOT routed to 11-fast-answers: the turn runs on the origin profile.
        assert scopes[0] is None or scopes[0] == "01-orchestrator"

    @pytest.mark.asyncio
    async def test_clean_session_still_routes_fast(self):
        """Criterion 2: no marker anywhere -> unchanged fast routing."""
        db = _StickyDB({"owui-2": {}})
        adapter = _make_adapter()
        scopes = await _run(adapter, "сколько будет 2+2?", "owui-2", db)
        assert scopes and scopes[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_unknown_session_still_routes_fast(self):
        """A session row absent from the store has no opinion -> fast."""
        adapter = _make_adapter()
        scopes = await _run(adapter, "сколько будет 2+2?", "owui-absent", _StickyDB())
        assert scopes and scopes[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_sticky_does_not_leak_to_sibling_session(self):
        """Criterion 4: another session in the same profile stays fast."""
        db = _StickyDB({"owui-1": {"slow_mode": True}})
        adapter = _make_adapter()
        scopes = await _run(adapter, "сколько будет 2+2?", "owui-2", db)
        assert scopes and scopes[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_marker_turn_then_markerless_turn_same_session(self):
        """Criterion 1 end-to-end: turn 1 marker, turn 2 marker-less, same session."""
        db = _StickyDB({"owui-1": {}})
        adapter = _make_adapter()

        # Turn 1: the marker turn is slow and persists the sticky marker.
        scopes1 = await _run(adapter, "подумай: разбери архитектуру", "owui-1", db)
        assert scopes1[0] is None or scopes1[0] == "01-orchestrator"

        from agent.fast_lane import decide_slow_mode, resolve_fast_path

        d1 = resolve_fast_path(
            decide_slow_mode(
                "подумай: разбери архитектуру",
                session_slow=db.get_session_model_config_value(
                    "owui-1", "slow_mode", None
                ),
            ),
            "подумай: разбери архитектуру",
        )
        assert d1.slow_mode is True
        db.rows["owui-1"]["slow_mode"] = True  # what the marker write persists

        # Turn 2: no marker, but the session is sticky -> still on the full lane.
        scopes2 = await _run(adapter, "а теперь 2+2?", "owui-1", db)
        assert scopes2[0] is None or scopes2[0] == "01-orchestrator"

    @pytest.mark.asyncio
    async def test_broken_db_degrades_to_previous_behaviour(self):
        """Criterion 5: an exploding store never breaks the turn."""

        class Boom:
            def get_session_model_config_value(self, *a, **k):
                raise RuntimeError("state.db is locked")

        adapter = _make_adapter()
        scopes = await _run(adapter, "сколько будет 2+2?", "owui-boom", Boom())
        # Degraded to the pre-existing per-request decision: marker-less -> fast.
        assert scopes and scopes[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_none_db_degrades_to_previous_behaviour(self):
        adapter = _make_adapter()
        scopes = await _run(adapter, "сколько будет 2+2?", "owui-nodb", None)
        assert scopes and scopes[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_explicit_fast_escape_overrides_sticky(self):
        """§B: a deliberate client fast override still wins over sticky."""
        db = _StickyDB({"owui-1": {"slow_mode": True}})
        adapter = _make_adapter()
        scopes: list = []

        @contextmanager
        def fake_scope(profile):
            scopes.append(profile)
            yield

        adapter._session_db = db

        with patch.object(adapter, "_profile_scope", staticmethod(fake_scope)), \
             patch.object(adapter, "_create_agent", return_value=_ok_agent()):
            await adapter._run_agent(
                user_message="сколько будет 2+2?",
                conversation_history=[],
                session_id="owui-1",
                model_options={"slow_mode": False},
                fast_lane_route=True,
            )
        assert scopes and scopes[0] == "11-fast-answers"


# ---------------------------------------------------------------------------
# api_server: the sticky hit is visible in the ROUTE log line
# ---------------------------------------------------------------------------

class TestStickyRouteLogLine:
    """Card item 3: ``source=session-sticky`` must reach the log line.

    Two shapes have to work: a marker-less turn (source is exactly
    ``session-sticky``) and a turn that ALSO carries its own marker (source is
    the composite ``session-sticky+<marker>``). The log gate used to compare the
    source with ``==``, so the composite shape produced no ROUTE line at all —
    the sticky reason silently disappeared from the logs whenever the follow-up
    message happened to contain «подумай» again.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message,expected_source",
        [
            ("сколько будет 2+2?", "session-sticky"),
            ("подумай: и ещё раз", "session-sticky+tg-prefix"),
        ],
    )
    async def test_route_line_shows_sticky_source(self, caplog, message, expected_source):
        db = _StickyDB({"owui-1": {"slow_mode": True, "slow_mode_source": "phrase"}})
        adapter = _make_adapter()
        with caplog.at_level(logging.INFO, logger="gateway.platforms.api_server"):
            scopes = await _run(adapter, message, "owui-1", db)
        assert scopes and scopes[0] != "11-fast-answers"
        route_lines = [
            r.getMessage() for r in caplog.records if "fast_lane=ROUTE" in r.getMessage()
        ]
        assert route_lines, "no fast_lane=ROUTE line emitted for a sticky turn"
        assert any(
            f"source={expected_source}" in line for line in route_lines
        ), route_lines


# ---------------------------------------------------------------------------
# api_server: the sticky hit is visible in the SLOW/FAST line too (t_4b1086c2)
# ---------------------------------------------------------------------------

class TestStickySlowLogLine:
    """Card t_4b1086c2 ACCEPTANCE: ``fast_lane=SLOW ... source=session-sticky``.

    ``TestStickyRouteLogLine`` above only locks the *ROUTE* line, which is
    emitted by ``_run_agent``. The SLOW/FAST line is emitted one layer deeper,
    by ``_create_agent``, from the decision it recomputes itself — so a ROUTE
    line carrying the sticky source proves nothing about SLOW. Before this
    class nothing in the tree asserted on ``fast_lane=SLOW``/``FAST`` at all
    (``grep -rn 'fast_lane=SLOW' tests/`` returned only a comment), i.e. the
    acceptance string of the card was unverifiable by the suite.

    These tests drive the REAL handler and the REAL ``_create_agent``; only
    ``run_agent.AIAgent`` construction is faked (the same hermetic recipe as
    ``test_api_server_text_marker.py``). That is the only way the SLOW line is
    produced by production code rather than by a stub.
    """

    @pytest.fixture
    def hermetic(self, monkeypatch):
        class FakeAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run_conversation(self, *args, **kwargs):
                return {"final_response": "ok"}

            session_prompt_tokens = 0
            session_completion_tokens = 0
            session_total_tokens = 0

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
        monkeypatch.setattr(
            "gateway.run._resolve_gateway_model",
            lambda: "letovo/deepseek-v4-flash",
        )
        # Flag ON: the SLOW/FAST line is emitted in both flag states, and the
        # sticky source must survive the activated fast-path overrides (the
        # resolve step rebuilds the decision — a naive rebuild would drop
        # ``source`` and print a blank reason).
        monkeypatch.setattr(
            "gateway.run._load_gateway_config",
            lambda: {
                "fast_lane": {"enabled": True},
                "agent": {"reasoning_effort": "high"},
            },
        )
        monkeypatch.setattr(
            "gateway.run.GatewayRunner._load_reasoning_config",
            staticmethod(lambda model="": {"enabled": True, "effort": "high"}),
        )
        monkeypatch.setattr(
            "gateway.run.GatewayRunner._load_fallback_model",
            staticmethod(lambda: None),
        )
        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools", lambda *_: set()
        )

    def _drive(self, caplog, message, rows, session_id="owui-1", route=True):
        """Run the real handler + real _create_agent; return the lane lines."""
        db = _StickyDB(rows)
        adapter = _make_adapter()
        adapter._session_db = db

        @contextmanager
        def fake_scope(profile):
            yield

        with caplog.at_level(logging.INFO, logger="gateway.platforms.api_server"), \
             patch.object(adapter, "_profile_scope", staticmethod(fake_scope)):
            asyncio.run(
                adapter._run_agent(
                    user_message=message,
                    conversation_history=[],
                    session_id=session_id,
                    fast_lane_route=route,
                )
            )
        return [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("fast_lane=")
        ], db

    def test_sticky_markerless_turn_logs_slow_with_sticky_source(
        self, hermetic, caplog
    ):
        """The card's exact acceptance: a sticky session + a marker-less turn."""
        lines, db = self._drive(
            caplog, "сколько будет 2+2?", {"owui-1": {"slow_mode": True}}
        )
        slow_lines = [ln for ln in lines if ln.startswith("fast_lane=SLOW")]
        assert slow_lines, f"no fast_lane=SLOW line emitted; got {lines}"
        assert any("source=session-sticky" in ln for ln in slow_lines), slow_lines
        # The exact token, not just a prefix: session-sticky+... is a different
        # (composite) shape asserted separately below.
        assert any(
            ln.split()[3] == "source=session-sticky" for ln in slow_lines
        ), slow_lines
        # SLOW must stay the slow lane's shape: no fast-path overrides leak in.
        assert not any("review_exempt=True" in ln for ln in slow_lines), slow_lines
        # ...and the marker is still written slow (D5: review card is kept).
        assert db.rows["owui-1"]["review_exempt"] is False

    def test_sticky_turn_with_own_marker_logs_composite_source(
        self, hermetic, caplog
    ):
        lines, _ = self._drive(
            caplog, "подумай: и ещё раз", {"owui-1": {"slow_mode": True}}
        )
        slow_lines = [ln for ln in lines if ln.startswith("fast_lane=SLOW")]
        assert slow_lines, f"no fast_lane=SLOW line emitted; got {lines}"
        assert any(
            "source=session-sticky+tg-prefix" in ln for ln in slow_lines
        ), slow_lines

    @pytest.mark.parametrize(
        "message,preferred",
        [
            ("сколько будет 2+2?", "fast_lane=FAST"),   # clean session -> fast
            ("подумай: разбери", "fast_lane=SLOW"),     # per-request marker only
        ],
    )
    def test_non_sticky_request_logs_non_sticky_source(
        self, hermetic, caplog, message, preferred
    ):
        """Without a sticky marker the source must NOT claim stickiness."""
        lines, _ = self._drive(caplog, message, {"owui-1": {}})
        lane_lines = [ln for ln in lines if ln.startswith(preferred)]
        assert lane_lines, f"no {preferred} line emitted; got {lines}"
        assert not any("session-sticky" in ln for ln in lines), lines

    def test_sticky_source_survives_when_profile_routing_is_off(
        self, hermetic, caplog
    ):
        """route_to_profile=off must not hide the reason a turn stayed slow.

        The ROUTE block is skipped entirely when routing is disabled, so the
        SLOW line is the ONLY evidence of the sticky hit — it must still fire.
        """
        lines, _ = self._drive(
            caplog, "сколько будет 2+2?", {"owui-1": {"slow_mode": True}}, route=False
        )
        assert not any("fast_lane=ROUTE" in ln for ln in lines), lines
        slow_lines = [ln for ln in lines if ln.startswith("fast_lane=SLOW")]
        assert slow_lines, f"no fast_lane=SLOW line emitted; got {lines}"
        assert any("source=session-sticky" in ln for ln in slow_lines), slow_lines


# ---------------------------------------------------------------------------
# fast_lane core: decide_slow_mode stays backward compatible (t_4b1086c2)
# ---------------------------------------------------------------------------

class TestDecideSlowModeBackwardCompatibility:
    """Other callers must keep working without the new ``session_slow`` kwarg.

    ``session_slow`` is optional and keyword-only, so ``gateway/run.py`` and
    any out-of-tree caller that never passes it get exactly the pre-sticky
    decision. These assertions pin that contract at the library seam, not just
    at the api_server call site.
    """

    def test_legacy_call_without_session_slow(self):
        from agent.fast_lane import decide_slow_mode

        decision = decide_slow_mode("сколько будет 2+2?")
        assert decision.slow_mode is False
        assert decision.source == "no-marker"

        marked = decide_slow_mode("подумай: разбери")
        assert marked.slow_mode is True
        assert marked.source == "tg-prefix"

    def test_session_slow_is_keyword_only(self):
        import inspect

        from agent.fast_lane import decide_slow_mode

        params = inspect.signature(decide_slow_mode).parameters
        assert "session_slow" in params
        assert params["session_slow"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["session_slow"].default is None

    def test_none_session_opinion_matches_legacy(self):
        from agent.fast_lane import decide_slow_mode

        for text in ("сколько будет 2+2?", "подумай: разбери"):
            legacy = decide_slow_mode(text)
            explicit_none = decide_slow_mode(text, session_slow=None)
            assert explicit_none.slow_mode == legacy.slow_mode
            assert explicit_none.source == legacy.source

    def test_false_session_opinion_does_not_force_slow(self):
        from agent.fast_lane import decide_slow_mode

        decision = decide_slow_mode("сколько будет 2+2?", session_slow=False)
        assert decision.slow_mode is False
        assert "session-sticky" not in decision.source


# ---------------------------------------------------------------------------
# api_server: the sticky read happens in the ORIGIN scope (ordering guard)
# ---------------------------------------------------------------------------

class TestApiServerStickyReadOrdering:
    @pytest.mark.asyncio
    async def test_sticky_read_precedes_executor_scope_switch(self):
        """The read must resolve against the origin store, not the fast one.

        Regression guard: reading the marker after ``_profile_scope(11-...)``
        would query the FAST profile's state.db, which only ever holds fast
        markers — sticky would be permanently False.
        """
        events: list = []

        class OrderDB(_StickyDB):
            def get_session_model_config_value(self, sid, key, default=None):
                events.append(("read", list(_scope_sink)))
                return True

        _scope_sink: list = []
        db = OrderDB({"owui-1": {"slow_mode": True}})
        adapter = _make_adapter()

        @contextmanager
        def fake_scope(profile):
            _scope_sink.append(profile)
            try:
                yield
            finally:
                _scope_sink.pop()

        async def _fake_db():
            return db

        adapter._session_db = db

        with patch.object(adapter, "_profile_scope", staticmethod(fake_scope)), \
             patch.object(adapter, "_create_agent", return_value=_ok_agent()):
            await adapter._run_agent(
                user_message="2+2?",
                conversation_history=[],
                session_id="owui-1",
                fast_lane_route=True,
            )

        assert events, "the sticky marker was never read"
        # At read time no profile scope had been entered yet.
        assert events[0][1] == [], f"read happened inside scope {events[0][1]}"


# ---------------------------------------------------------------------------
# gateway/run.py (messenger path): the Track M selector honours sticky
# ---------------------------------------------------------------------------
#
# Card t_e305207e. The module docstring above always claimed this seam was
# covered, but only the api_server half had tests. These drive the REAL
# ``GatewayRunner._resolve_fast_lane_execute_profile`` (and ``_run_agent``'s
# sticky read) so the messenger path cannot silently regress to "sticky is
# ignored on messengers".
#
# Two defects this block locks down (both found while wiring t_e305207e):
#   * the read ran INLINE on the event-loop thread (blocking SQLite in a
#     coroutine) and, on the ROUTE path, a SECOND time from the debug block;
#   * the read ran even when fast-lane routing was disabled for the platform.


class _FakePlatform:
    """Plugin-platform stand-in (SessionSource only reads ``.value``)."""

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


def _bare_runner(db=None):
    """Bare GatewayRunner via object.__new__ (AGENTS.md pattern)."""
    from gateway.run import GatewayRunner

    obj = object.__new__(GatewayRunner)
    obj.config = type("C", (), {"multiplex_profiles": False})()
    obj._session_db = db
    return obj


_CFG_ON = {
    "fast_lane": {
        "enabled": True,
        "route_to_profile": True,
        "route_platforms": ["yadisk_bridge"],
    },
    "agent": {"reasoning_effort": "high"},
}
_CFG_OFF = {"fast_lane": {"enabled": False}, "agent": {"reasoning_effort": "high"}}


@pytest.fixture
def cfg_on():
    with patch("gateway.run._load_gateway_config", return_value=_CFG_ON):
        yield


class TestRunPyStickyRouting:
    """Criterion 1/2/4 on the messenger (gateway/run.py) path."""

    def test_sticky_session_stays_on_current_profile(self, cfg_on):
        """A slowed session keeps marker-less follow-ups on the full lane."""
        runner = _bare_runner(_StickyDB({"sess-1": {"slow_mode": True}}))
        assert (
            runner._resolve_fast_lane_execute_profile(
                "а сколько будет 2+2?", _messenger_source(), "sess-1"
            )
            is None
        ), "sticky session was routed to the fast profile"

    def test_clean_session_still_routes_fast(self, cfg_on):
        """No regression: an unmarked session still goes to 11-fast-answers."""
        runner = _bare_runner(_StickyDB({"sess-1": {"slow_mode": True}}))
        assert (
            runner._resolve_fast_lane_execute_profile(
                "сколько будет 2+2?", _messenger_source(), "sess-clean"
            )
            == "11-fast-answers"
        )

    def test_sticky_does_not_leak_to_sibling_session(self, cfg_on):
        runner = _bare_runner(
            _StickyDB({"sess-slow": {"slow_mode": True}, "sess-b": {}})
        )
        assert (
            runner._resolve_fast_lane_execute_profile(
                "2+2?", _messenger_source(), "sess-slow"
            )
            is None
        )
        assert (
            runner._resolve_fast_lane_execute_profile(
                "2+2?", _messenger_source(), "sess-b"
            )
            == "11-fast-answers"
        )

    def test_explicit_trigger_beats_the_fast_profile(self, cfg_on):
        """A marker in the request itself still means slow (unchanged)."""
        runner = _bare_runner(_StickyDB({}))
        assert (
            runner._resolve_fast_lane_execute_profile(
                "подумай: разбери архитектуру", _messenger_source(), "sess-x"
            )
            is None
        )


class TestRunPyStickyDegradation:
    """Criterion 5: a broken store degrades, never raises."""

    def test_broken_db_degrades(self, cfg_on):
        class Boom:
            def get_session_model_config_value(self, *a, **k):
                raise RuntimeError("state.db is locked")

        runner = _bare_runner(Boom())
        assert (
            runner._resolve_fast_lane_execute_profile(
                "2+2?", _messenger_source(), "sess-1"
            )
            == "11-fast-answers"
        )

    def test_none_db_degrades(self, cfg_on):
        runner = _bare_runner(None)
        assert (
            runner._resolve_fast_lane_execute_profile(
                "2+2?", _messenger_source(), "sess-1"
            )
            == "11-fast-answers"
        )


class TestRunPyStickyLogLine:
    """Criterion 4: the sticky hit is visible as ``source=session-sticky``."""

    def test_route_line_shows_sticky_source(self, cfg_on, caplog):
        runner = _bare_runner(_StickyDB({"sess-1": {"slow_mode": True}}))
        with caplog.at_level(logging.INFO, logger="gateway.run"):
            assert (
                runner._resolve_fast_lane_execute_profile(
                    "а сколько будет 2+2?", _messenger_source(), "sess-1"
                )
                is None
            )
        lines = [
            r.getMessage()
            for r in caplog.records
            if "fast_lane=ROUTE" in r.getMessage()
        ]
        assert lines, "no fast_lane=ROUTE line emitted for a sticky messenger turn"
        assert any("source=session-sticky" in line for line in lines), lines

    def test_route_line_shows_composite_source(self, cfg_on, caplog):
        """A follow-up that repeats «подумай» reports the composite source."""
        runner = _bare_runner(_StickyDB({"sess-1": {"slow_mode": True}}))
        with caplog.at_level(logging.INFO, logger="gateway.run"):
            runner._resolve_fast_lane_execute_profile(
                "подумай: и ещё раз", _messenger_source(), "sess-1"
            )
        lines = [
            r.getMessage()
            for r in caplog.records
            if "fast_lane=ROUTE" in r.getMessage()
        ]
        assert any(
            "source=session-sticky+tg-prefix" in line for line in lines
        ), lines


class TestRunAgentStickyRead:
    """The read in ``_run_agent`` must be off-loop, single, and gated."""

    @pytest.mark.asyncio
    async def test_read_runs_off_loop_and_only_once(self, cfg_on):
        """Blocking SQLite must not run on the loop thread, nor twice.

        Pre-fix: ``session_slow_mode`` was called inline (freezing the loop for
        every messenger turn) and then a SECOND time inside the ROUTE debug
        block. Post-fix: one read, on a worker thread, and the selector reuses
        the value instead of re-reading.
        """
        import threading

        from gateway.run import GatewayRunner
        from agent.fast_lane import session_slow_mode as _real_read

        loop_thread = threading.current_thread()
        on_loop: list = []

        def _counting(db, sid):
            on_loop.append(threading.current_thread() is loop_thread)
            return _real_read(db, sid)

        runner = _bare_runner(_StickyDB({"sess-1": {"slow_mode": True}}))

        with patch("agent.fast_lane.session_slow_mode", _counting), patch.object(
            GatewayRunner,
            "_run_agent_inner",
            **{"return_value": {"final_response": "stub"}},
        ):
            await runner._run_agent(
                message="а сколько будет 2+2?",
                context_prompt="",
                history=[],
                source=_messenger_source(),
                session_id="sess-1",
                run_generation=1,
            )

        assert on_loop, "the sticky marker was never read"
        assert len(on_loop) == 1, (
            f"expected exactly ONE sticky read per turn, got {len(on_loop)}"
        )
        assert on_loop == [False], (
            "a sticky read ran on the event-loop thread: "
            f"on_loop={on_loop} (expected [False])"
        )

    @pytest.mark.asyncio
    async def test_marker_never_read_when_routing_disabled(self):
        """With fast-lane off, a messenger turn must not touch state.db."""
        from gateway.run import GatewayRunner

        calls: list = []
        runner = _bare_runner(_StickyDB({"sess-1": {"slow_mode": True}}))

        with patch(
            "agent.fast_lane.session_slow_mode",
            lambda db, sid: calls.append(sid) or True,
        ), patch("gateway.run._load_gateway_config", return_value=_CFG_OFF), patch.object(
            GatewayRunner,
            "_run_agent_inner",
            **{"return_value": {"final_response": "stub"}},
        ):
            await runner._run_agent(
                message="привет",
                context_prompt="",
                history=[],
                source=_messenger_source(),
                session_id="sess-1",
                run_generation=1,
            )

        assert calls == [], f"sticky was read while routing was disabled: {calls}"


# ---------------------------------------------------------------------------
# api_server: the sticky read must stay OFF the cold-start critical path
# ---------------------------------------------------------------------------
#
# Two defects found while wiring this card (t_ca558566), both locked down here
# because they only show up in production-shaped conditions:
#
# 1. The read forced a cold writable ``SessionDB`` construction in front of
#    ``_create_agent``. ``SessionDB.__init__`` runs full schema init (DDL, FTS
#    probes, pragmas) and measured ~3 s on a fresh ``HERMES_HOME`` — paid on
#    the first run of every process for a flag that reads None anyway (a
#    session is only sticky AFTER a marker turn opened the DB). It also put a
#    write-side construction on a read path.
# 2. On ``/v1/runs`` the read sat inside the background run task, so
#    ``await``-ing it suspended the task before ``_create_agent`` and broke the
#    endpoint's "reach agent construction in one uninterrupted slice" contract
#    (3 pre-existing /v1/runs tests failed on it).


class _ConstructTrackingDB(_StickyDB):
    """Records marker reads so a test can assert none happened."""

    def __init__(self, rows=None):
        super().__init__(rows)
        self.reads: list = []

    def get_session_model_config_value(self, sid, key, default=None):
        self.reads.append(sid)
        return super().get_session_model_config_value(sid, key, default)


class TestStickyReadNeverConstructsSessionDB:
    @pytest.mark.asyncio
    async def test_unopened_adapter_does_not_build_a_session_db(self):
        """A cold adapter must probe the file, not build a writable SessionDB."""
        import agent.fast_lane as fl
        from hermes_constants import get_hermes_home

        adapter = _make_adapter()
        assert adapter._session_db is None
        assert adapter._session_dbs == {}

        seen: list = []
        real = fl.session_slow_mode_from_db_file

        def _spy(db_path, session_id, **kw):
            seen.append((str(db_path), session_id))
            return real(db_path, session_id, **kw)

        with patch.object(fl, "session_slow_mode_from_db_file", _spy):
            result = await adapter._read_sticky_marker("cold-session")

        assert result is None                      # no row -> no opinion
        assert seen, "the read-only file probe was never used"
        assert seen[0][0].endswith("state.db")
        # The whole point: nothing was constructed or cached.
        assert adapter._session_db is None
        assert adapter._session_dbs == {}
        assert get_hermes_home() is not None

    @pytest.mark.asyncio
    async def test_open_handle_is_preferred_over_the_file_probe(self):
        """A cached/injected handle wins — no extra file open."""
        import agent.fast_lane as fl

        db = _ConstructTrackingDB({"owui-1": {"slow_mode": True}})
        adapter = _make_adapter()
        adapter._session_db = db

        with patch.object(
            fl, "session_slow_mode_from_db_file", side_effect=AssertionError("file probe used")
        ):
            assert await adapter._read_sticky_marker("owui-1") is True
        assert db.reads == ["owui-1"]

    @pytest.mark.asyncio
    async def test_file_probe_still_reports_stickiness_after_restart(self):
        """Restart safety: stickiness survives because the row is read from disk."""
        import sqlite3 as _sq

        from agent.fast_lane import session_slow_mode_from_db_file

        con = _sq.connect(":memory:")
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
        con.execute(
            "INSERT INTO sessions VALUES ('s1', ?)",
            (json.dumps({"slow_mode": True, "slow_mode_source": "phrase"}),),
        )
        con.commit()

        path = _db_file_from_mem(con)
        try:
            assert session_slow_mode_from_db_file(path, "s1") is True
            assert session_slow_mode_from_db_file(path, "absent") is None
            assert session_slow_mode_from_db_file(path / "nope.db", "s1") is None
        finally:
            con.close()


def _db_file_from_mem(con) -> "object":
    """Materialise an in-memory DB onto a temp file (unique per call)."""
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp(prefix="sticky-probe-")) / "state.db"
    dest = sqlite3.connect(str(path))
    con.backup(dest)
    dest.close()
    return path


class TestRunsStickyReadPlacement:
    """``/v1/runs`` must read the marker BEFORE the background task starts.

    Regression guard for defect 2 above. Behaviour asserted here is the real
    requirement, not the helper name: the sticky value must already be
    resolved when the 202 comes back (the handler ran the read in one
    uninterrupted slice), and the value must reach ``_create_agent``. Moving
    the read back inside ``_run_and_close`` as an ``await`` breaks both — the
    response returns before the read happens, and the task is suspended at its
    first suspension point before agent construction.
    """

    def _drive(self, db, session_id, message):
        from aiohttp.test_utils import TestClient, TestServer

        from tests.gateway.test_api_server_runs import _create_runs_app

        adapter = _make_adapter()
        adapter._session_db = db
        app = _create_runs_app(adapter)

        async def _go():
            seen: dict = {}
            sync_calls: list = []
            real_sync = adapter._read_sticky_marker_sync

            def _spy_sync(sid):
                value = real_sync(sid)
                sync_calls.append((sid, value))
                return value

            awaitable_calls: list = []

            async def _spy_async(sid):
                awaitable_calls.append(sid)
                return await _read_sticky_marker_orig(sid)

            _read_sticky_marker_orig = adapter._read_sticky_marker
            adapter._read_sticky_marker_sync = _spy_sync
            adapter._read_sticky_marker = _spy_async

            async with TestClient(TestServer(app)) as cli:
                with patch.object(adapter, "_create_agent") as mock_create:
                    agent = _ok_agent()

                    def _capture(**kwargs):
                        seen.update(kwargs)
                        return agent

                    mock_create.side_effect = _capture

                    resp = await cli.post(
                        "/v1/runs", json={"input": message, "session_id": session_id}
                    )
                    assert resp.status == 202
                    # The read must already be DONE when the 202 comes back.
                    calls_at_return = list(sync_calls)
                    awaitable_at_return = list(awaitable_calls)

                    import asyncio as _a

                    for _ in range(80):
                        if seen:
                            break
                        await _a.sleep(0.02)
            return seen, calls_at_return, awaitable_at_return

        return asyncio.run(_go())

    def test_sticky_value_is_resolved_before_the_response_returns(self):
        seen, sync_calls, awaitable_calls = self._drive(
            _StickyDB({"owui-1": {"slow_mode": True}}), "owui-1", "продолжай"
        )
        assert sync_calls, "the sticky read did not run in the request handler"
        assert sync_calls[0] == ("owui-1", True)
        assert awaitable_calls == [], (
            "the run task awaited the sticky read: that suspends it before "
            f"_create_agent (calls={awaitable_calls})"
        )
        assert seen.get("fast_lane_session_slow") is True

    def test_clean_session_passes_no_sticky_opinion(self):
        seen, sync_calls, _ = self._drive(
            _StickyDB({"owui-clean": {}}), "owui-clean", "2+2?"
        )
        assert sync_calls and sync_calls[0][1] is None
        assert seen.get("fast_lane_session_slow") is None


# ---------------------------------------------------------------------------
# t_d42f582c: the SAME seam, but against a REAL SessionDB
# ---------------------------------------------------------------------------
#
# Everything above drives the seam through ``_StickyDB``, a dict-shaped fake.
# That fake proves the wiring, but it cannot catch writer/reader drift: the
# "turn 1 writes the marker, turn 2 is marker-less" scenario was reconstructed
# by hand (``db.rows[...] = True``) instead of by the production writer
# ``patch_session_fast_lane_marker``. These tests close that gap on a real
# ``SessionDB`` file: the marker is written by the REAL production writer, read
# back by the REAL production reader, and the resulting executor profile is
# asserted BY VALUE (``01-orchestrator`` / ``11-fast-answers``), not by
# "anything but the fast lane".
#
# Note on ``01-orchestrator``: ``route_profile`` returns ``None`` for a slow
# decision, meaning "keep the current request profile". ``_run_agent`` resolves
# that to ``request_profile`` -- the value the multiplex middleware put in
# ``_api_request_profile`` (the origin profile, ``01-orchestrator`` in the live
# gateway). The tests set that ContextVar explicitly and assert the CONCRETE
# profile the executor scope receives, which is the card's acceptance
# criterion.


def _real_session_db(tmp_path):
    from hermes_state import SessionDB

    return SessionDB(tmp_path / "state.db")


@contextmanager
def _closing(db):
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


async def _drive_real(adapter, message, session_id, db, origin="01-orchestrator"):
    """Drive ``_run_agent`` under an explicit ORIGIN request profile.

    ``adapter._session_db`` is the documented explicit override, so the real
    ``SessionDB`` file is read through the production precedence rule.
    """
    from gateway.platforms.api_server import _api_request_profile

    scopes: list = []

    @contextmanager
    def fake_scope(profile):
        scopes.append(profile)
        yield

    adapter._session_db = db
    token = _api_request_profile.set(origin)
    try:
        with patch.object(adapter, "_profile_scope", staticmethod(fake_scope)), \
             patch.object(adapter, "_create_agent", return_value=_ok_agent()):
            result = await adapter._run_agent(
                user_message=message,
                conversation_history=[],
                session_id=session_id,
                fast_lane_route=True,
            )
    finally:
        _api_request_profile.reset(token)
    return scopes, result


def _seed_real_marker(db, session_id, *, slow: bool):
    """Write the marker via the PRODUCTION writer (no hand-built dict)."""
    from agent.fast_lane import FastLaneDecision, patch_session_fast_lane_marker

    db.create_session(session_id, source="openwebui")
    patch_session_fast_lane_marker(
        db,
        session_id,
        FastLaneDecision(slow_mode=slow, source="phrase" if slow else "no-marker"),
        preserve_sticky=True,
    )


class TestRealSessionDBStickyRouting:
    """Card acceptance on a real store, driven by the real writer/reader."""

    @pytest.mark.asyncio
    async def test_real_writer_makes_a_markerless_turn_route_to_01(self, tmp_path):
        """AC (a): real marker -> marker-less turn runs on 01-orchestrator."""
        with _closing(_real_session_db(tmp_path)) as db:
            _seed_real_marker(db, "owui-sticky", slow=True)
            adapter = _make_adapter()
            scopes, _ = await _drive_real(
                adapter, "продолжай, что дальше?", "owui-sticky", db
            )
        assert scopes, "no executor scope entered"
        assert scopes[0] == "01-orchestrator", (
            f"a sticky session escaped to {scopes[0]!r} on a marker-less turn"
        )

    @pytest.mark.asyncio
    async def test_fresh_real_session_routes_to_11(self, tmp_path):
        """AC (b): a fresh session with no marker still routes to 11-fast-answers."""
        with _closing(_real_session_db(tmp_path)) as db:
            db.create_session("owui-clean", source="openwebui")
            adapter = _make_adapter()
            scopes, _ = await _drive_real(
                adapter, "сколько будет 2+2?", "owui-clean", db
            )
        assert scopes and scopes[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_written_false_marker_does_not_block_the_fast_lane(self, tmp_path):
        """A real ``slow_mode: false`` row is an explicit fast opinion, not sticky."""
        from agent.fast_lane import session_slow_mode

        with _closing(_real_session_db(tmp_path)) as db:
            _seed_real_marker(db, "owui-fast", slow=False)
            assert session_slow_mode(db, "owui-fast") is False
            adapter = _make_adapter()
            scopes, _ = await _drive_real(adapter, "2+2?", "owui-fast", db)
        assert scopes and scopes[0] == "11-fast-answers"

    @pytest.mark.asyncio
    async def test_stickiness_survives_a_restart_of_the_handle(self, tmp_path):
        """Restart safety: a NEW SessionDB on the same file still reports sticky.

        This is the property a cache-only implementation would silently lose,
        and it is why the read path probes the committed row instead of only
        trusting an in-process cache.
        """
        from agent.fast_lane import session_slow_mode

        db_path = tmp_path / "state.db"
        with _closing(_real_session_db(tmp_path)) as first:
            _seed_real_marker(first, "owui-restart", slow=True)
        assert db_path.exists()

        with _closing(_real_session_db(tmp_path)) as second:
            assert session_slow_mode(second, "owui-restart") is True
            adapter = _make_adapter()
            scopes, _ = await _drive_real(adapter, "продолжай", "owui-restart", second)
        assert scopes and scopes[0] == "01-orchestrator"


class TestRealSessionDBDegradation:
    """Card item 2: no DB / no session_id completes on today's routing."""

    @pytest.mark.asyncio
    async def test_no_db_and_no_state_file_keeps_todays_routing(self, tmp_path):
        """Unavailable store -> the request completes and routes fast, no raise."""
        cold_home = tmp_path / "cold-home"
        cold_home.mkdir()
        adapter = _make_adapter()
        scopes: list = []

        @contextmanager
        def fake_scope(profile):
            scopes.append(profile)
            yield

        adapter._session_db = None
        with patch("hermes_constants.get_hermes_home", return_value=cold_home), \
             patch.object(adapter, "_profile_scope", staticmethod(fake_scope)), \
             patch.object(adapter, "_create_agent", return_value=_ok_agent()):
            result = await adapter._run_agent(
                user_message="сколько будет 2+2?",
                conversation_history=[],
                session_id="owui-no-store",
                fast_lane_route=True,
            )

        assert scopes and scopes[0] == "11-fast-answers"
        assert isinstance(result, tuple) and len(result) == 2
        assert result[0].get("final_response") == "ok"

    @pytest.mark.asyncio
    async def test_none_session_id_completes_and_routes_as_today(self, tmp_path):
        """``session_id=None`` -> no session opinion, handler still completes."""
        with _closing(_real_session_db(tmp_path)) as db:
            adapter = _make_adapter()
            scopes, result = await _drive_real(
                adapter, "сколько будет 2+2?", None, db
            )
        assert scopes and scopes[0] == "11-fast-answers"
        assert result[0].get("final_response") == "ok"


# ---------------------------------------------------------------------------
# t_d3387ba5: the messenger SLOW line — the card's acceptance string
# ---------------------------------------------------------------------------
#
# Every fast-lane line asserted above is emitted on the api_server path
# (``fast_lane=SLOW`` / ``fast_lane=ROUTE``). The messenger path emits its OWN
# line from ``TurnRunner.run_sync`` in gateway/run.py:
#
#     fast_lane=SLOW TG session=<key> source=<source> reason=<reason> ...
#
# ``grep -rn "fast_lane=SLOW TG" tests/`` returned NOTHING before this block,
# so the messenger acceptance string ("the log line shows
# ``source=session-sticky`` when the sticky rule fires") was asserted only for
# OWUI/API and only by hand. These tests drive the REAL TurnRunner.run_sync —
# the same seam production uses for every Telegram/plugin-messenger turn — and
# assert on the real ``gateway.run`` logger output.
#
# Two properties beyond "the string appears":
#   * the sticky read must NOT be a second read: exactly one
#     ``get_session_model_config_value`` call per turn;
#   * with the feature off, the messenger turn must not touch state.db at all
#     (the selector is gated by ``_fast_lane_route_config``; the SLOW/FAST
#     block in run_sync is NOT gated and used to read unconditionally).


def _turn_runner(db, user_config, message="а сколько будет 2+2?", session_id="tg-1"):
    """Build a TurnRunner over a MagicMock gateway runner (test_turn_context recipe)."""
    from gateway.run import TurnRunner

    class _Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.tools = []
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0

        def run_conversation(self, _message, **_kwargs):
            return {"final_response": "ok", "messages": [], "failed": False}

    from types import SimpleNamespace

    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = db
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    runner._resolve_session_reasoning_config.return_value = {
        "enabled": True,
        "effort": "high",
    }
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    runner._agent_config_signature.return_value = ("sig",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_telegram_topic_lane.return_value = False
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False
    runner._has_explicit_model_selection.return_value = False

    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext

    ctx = TurnContext(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="c", user_id="u"),
        message=message,
        history=[],
        session_id=session_id,
        session_key=f"{session_id}-key",
        user_config=user_config,
        has_attachments=False,
        AIAgent=_Agent,
        resolve_display_setting=lambda *_a: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    return TurnRunner(runner, ctx)


_FLAG_ON = {"fast_lane": {"enabled": True}, "agent": {"reasoning_effort": "high"}}


class TestMessengerSlowLogLine:
    """The messenger acceptance string: ``fast_lane=SLOW TG source=session-sticky``."""

    def _lane_lines(self, db, user_config, message="а сколько будет 2+2?"):
        records: list = []

        class _Grab(logging.Handler):
            def emit(self, record):  # noqa: D102 — local log capture
                records.append(record.getMessage())

        log = logging.getLogger("gateway.run")
        handler = _Grab()
        prev_level = log.level
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            _turn_runner(db, user_config, message=message).run_sync()
        finally:
            log.removeHandler(handler)
            log.setLevel(prev_level)

        return [m for m in records if m.startswith("fast_lane=")]

    def test_markerless_turn_in_slow_session_logs_sticky_source(self):
        """Card ACCEPTANCE: the messenger line names the sticky rule."""
        db = _StickyDB({"tg-1": {"slow_mode": True, "slow_mode_source": "phrase"}})
        lines = self._lane_lines(db, _FLAG_ON)

        slow = [ln for ln in lines if ln.startswith("fast_lane=SLOW TG")]
        assert slow, f"no 'fast_lane=SLOW TG' line emitted; got {lines}"
        assert any("source=session-sticky" in ln for ln in slow), slow
        # The exact token, not a prefix: session-sticky+<marker> is composite
        # and is asserted separately below.
        assert any(
            tok == "source=session-sticky"
            for ln in slow
            for tok in ln.split()
        ), slow
        # SLOW must keep the slow lane's shape: no fast-path fields leak in.
        assert not any("review_exempt=True" in ln for ln in slow), slow

    def test_sticky_read_happens_exactly_once_per_turn(self):
        """One marker read per turn — not one here plus one in the selector."""
        class _CountingDB(_StickyDB):
            def __init__(self, rows):
                super().__init__(rows)
                self.reads: list = []

            def get_session_model_config_value(self, sid, key, default=None):
                self.reads.append((sid, key))
                return super().get_session_model_config_value(sid, key, default)

        db = _CountingDB({"tg-1": {"slow_mode": True}})
        self._lane_lines(db, _FLAG_ON)
        marker_reads = [r for r in db.reads if r[1] == "slow_mode"]
        assert len(marker_reads) == 1, (
            "the sticky marker was read more than once on one messenger turn: "
            f"{marker_reads}"
        )

    def test_sticky_turn_with_own_marker_logs_composite_source(self):
        db = _StickyDB({"tg-1": {"slow_mode": True}})
        lines = self._lane_lines(db, _FLAG_ON, message="подумай: разбери архитектуру")
        slow = [ln for ln in lines if ln.startswith("fast_lane=SLOW TG")]
        assert slow, f"no 'fast_lane=SLOW TG' line emitted; got {lines}"
        assert any(
            "source=session-sticky+tg-prefix" in ln for ln in slow
        ), slow

    def test_clean_session_keeps_the_fast_lane(self):
        """No regression: an unmarked session still logs FAST / no-marker."""
        db = _StickyDB({"tg-1": {}})
        lines = self._lane_lines(db, _FLAG_ON)
        fast = [ln for ln in lines if ln.startswith("fast_lane=FAST TG")]
        assert fast, f"no 'fast_lane=FAST TG' line emitted; got {lines}"
        assert not any("session-sticky" in ln for ln in lines), lines

    def test_per_request_marker_alone_is_not_reported_as_sticky(self):
        """A first «подумай» turn must not claim the session was already slow."""
        db = _StickyDB({"tg-1": {}})
        lines = self._lane_lines(db, _FLAG_ON, message="подумай: разбери архитектуру")
        slow = [ln for ln in lines if ln.startswith("fast_lane=SLOW TG")]
        assert slow, f"no 'fast_lane=SLOW TG' line emitted; got {lines}"
        assert not any("session-sticky" in ln for ln in lines), lines
        # ...and the turn still latches the session slow for the next one.
        assert db.rows["tg-1"]["slow_mode"] is True

    @pytest.mark.parametrize(
        "user_config",
        [
            {"fast_lane": {"enabled": False}, "agent": {"reasoning_effort": "high"}},
            {"agent": {"reasoning_effort": "high"}},
        ],
        ids=["explicit_false", "section_absent"],
    )
    def test_flag_off_writes_no_marker(self, user_config):
        """Design §6.2: flag off = behaviour as before -> no marker is persisted.

        The read still happens (deliberately): ``fast_lane_enabled`` documents
        that "the marker computation and logging still run so operators can see
        what the decision would be, but the fast-path overrides are inert", and
        the api_server sibling reads unconditionally for the same reason. What
        must NOT happen with the flag off is the WRITE — that is what flips the
        D5 forcer's ``session_is_fast_lane`` and would leak fast-lane state into
        a disabled feature.
        """

        class _CountingDB(_StickyDB):
            def __init__(self, rows):
                super().__init__(rows)
                self.writes: list = []

            def patch_session_model_config(self, sid, patch_):
                self.writes.append((sid, dict(patch_)))
                return super().patch_session_model_config(sid, patch_)

        db = _CountingDB({"tg-1": {"slow_mode": True}})
        lines = self._lane_lines(db, user_config)
        assert lines, "no fast_lane= line emitted at all (decision must still log)"
        assert db.writes == [], f"a marker was written with the feature off: {db.writes}"
        # The pre-existing marker is untouched, so enabling the flag later still
        # honours the stickiness the session had already earned.
        assert db.rows["tg-1"]["slow_mode"] is True


