"""TurnRunner seam: fast-lane level vs. an operator session reasoning pin.

t_0563b054. The defect this pins down lives in ``gateway/run.py``:

  * a fresh agent built for a FAST turn had its ``reasoning_config`` replaced
    wholesale by the lane decision (``_fast_decision.reasoning_effort``),
    which demoted a session pin such as the RGZ ``max`` guarantee;
  * a REUSED (cached) agent kept the level it was built with, so the fast
    level never reached later turn's wire at all.

Both are checked here on the REAL ``TurnRunner.run_sync`` path with the real
``agent.fast_lane`` decisions — only ``AIAgent`` (heavy) and the provider
resolution are faked, exactly like ``test_turn_context.py`` does.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


class _CapturingAgent:
    """Stand-in AIAgent that records the reasoning config it was built with."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        self.model = kwargs.get("model")
        self.session_id = kwargs.get("session_id")
        self.tools = []
        self.max_iterations = kwargs.get("max_iterations")
        self.run_budget_seconds = kwargs.get("run_budget_seconds")
        self.reasoning_config = kwargs.get("reasoning_config")
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000
        )
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0

    def run_conversation(self, _message, **_kwargs):
        return {"final_response": "ok", "messages": [], "failed": False}


def _make_ctx(message: str, *, user_config=None) -> TurnContext:
    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="test-chat",
        user_id="test-user",
    )
    return TurnContext(
        source=source,
        message=message,
        history=[],
        session_id="test-session",
        session_key="test-session-key",
        user_config=user_config
        if user_config is not None
        else {"fast_lane": {"enabled": True, "reasoning_effort": "low"}},
        has_attachments=False,
        AIAgent=_CapturingAgent,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )


def _gateway_runner(*, session_override):
    """GatewayRunner stub: everything the turn needs, nothing it does not.

    ``session_override`` is what ``_peek_session_state(session_key)`` returns —
    ``None`` for a session with no operator pin, or a state whose
    ``conversation.reasoning_override`` carries the pinned config.
    """
    gr = MagicMock()
    gr.config = SimpleNamespace(streaming=None)
    gr._provider_routing = {}
    gr._agent_cache_lock = None
    gr._agent_cache = {}
    gr._session_db = None
    gr._prefill_messages = None
    gr._pending_model_notes = {}
    gr._pending_skills_reload_notes = {}
    gr._service_tier = None
    gr._reasoning_config = None
    gr.session_store._entries = {}
    gr._get_system_prompt_for_channel.return_value = None
    gr._resolve_session_agent_runtime.return_value = (
        "deepseek-v4-flash",
        {"provider": "letovo", "base_url": "https://llm.letovo.site/openai"},
    )
    gr._resolve_session_reasoning_config.return_value = (
        dict(session_override.conversation.reasoning_override)
        if session_override is not None
        else {"enabled": True, "effort": "high"}
    )
    gr._resolve_session_service_tier.return_value = None
    gr._resolve_turn_agent_config.return_value = {
        "model": "deepseek-v4-flash",
        "runtime": {},
    }
    gr._agent_config_signature.return_value = ("test-signature",)
    gr._extract_cache_busting_config.return_value = {}
    gr._refresh_fallback_model.return_value = None
    gr._consume_pending_native_image_paths.return_value = []
    gr._consume_pending_turn_sidecar_notes.return_value = []
    gr._is_telegram_topic_lane.return_value = False
    gr._is_discord_auto_thread_lane.return_value = False
    gr._is_relay_discord_channel_lane.return_value = False
    gr._may_have_pending_turn_sidecar_notes.return_value = False
    gr._peek_session_state.return_value = session_override
    gr._has_explicit_model_selection.return_value = False
    return gr


def _pinned_state(effort: str = "max"):
    return SimpleNamespace(
        conversation=SimpleNamespace(
            reasoning_override={"enabled": True, "effort": effort}
        )
    )


def _run(monkeypatch, message, *, session_override=None):
    """Run one real turn; return the reasoning config the agent was built with."""
    from gateway.run import TurnRunner

    # Session-DB writer is a no-op here (no DB in this unit scope) — the
    # marker contract is covered by the fast_lane/api_server suites.
    monkeypatch.setattr(
        "agent.fast_lane.patch_session_fast_lane_marker",
        lambda *_a, **_k: None,
    )
    ctx = _make_ctx(message)
    gr = _gateway_runner(session_override=session_override)
    TurnRunner(gr, ctx).run_sync()
    return _CapturingAgent.captured


def test_plain_fast_turn_runs_at_low(monkeypatch):
    """No operator pin -> the lane level (low) reaches the agent."""
    captured = _run(monkeypatch, "сколько будет 2+2?", session_override=None)
    assert captured["reasoning_config"] == {"enabled": True, "effort": "low"}


def test_session_pin_max_survives_a_fast_turn(monkeypatch):
    """RGZ / /reasoning --session pin is never demoted by the lane heuristic."""
    captured = _run(
        monkeypatch, "сколько будет 2+2?", session_override=_pinned_state("max")
    )
    assert captured["reasoning_config"] == {"enabled": True, "effort": "max"}


def test_session_pin_max_survives_many_fast_turns(monkeypatch):
    """Repeat turns with the same pin stay at max (no drift to the lane level)."""
    for _ in range(3):
        captured = _run(
            monkeypatch, "статус задачи", session_override=_pinned_state("max")
        )
        assert captured["reasoning_config"]["effort"] == "max"


def test_rgz_phrase_pins_this_turn_to_max(monkeypatch):
    """The RGZ trigger turn itself runs at max on the real seam."""
    captured = _run(monkeypatch, "гигантский план миссии", session_override=None)
    assert captured["reasoning_config"] == {"enabled": True, "effort": "max"}


def test_cached_agent_receives_the_lane_level(monkeypatch):
    """On a REUSED agent the lane level is pushed onto the object.

    The cached path never re-runs the constructor, so before t_0563b054 the
    fast level was lost on every later turn of a long-lived session.
    """
    from gateway.run import TurnRunner

    monkeypatch.setattr(
        "agent.fast_lane.patch_session_fast_lane_marker",
        lambda *_a, **_k: None,
    )
    ctx = _make_ctx("сколько будет 2+2?")
    gr = _gateway_runner(session_override=None)

    # Pre-populate the cache with a live agent for this session signature.
    cached = SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "high"},
        max_iterations=None,
        run_budget_seconds=None,
        review_exempt=False,
        run_conversation=lambda *_a, **_k: {
            "final_response": "ok",
            "messages": [],
            "failed": False,
        },
        tools=[],
        context_compressor=SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000
        ),
        session_prompt_tokens=0,
        session_completion_tokens=0,
        _last_activity_ts=0,
    )
    from collections import OrderedDict

    gr._agent_cache = OrderedDict()
    gr._agent_cache_lock = __import__("threading").Lock()
    gr._agent_cache[ctx.session_key] = (cached, ("test-signature",))

    TurnRunner(gr, ctx).run_sync()
    assert cached.reasoning_config == {"enabled": True, "effort": "low"}


def test_cached_agent_keeps_a_session_pin(monkeypatch):
    """...but a pinned session level is NOT overwritten on the cached agent."""
    from collections import OrderedDict
    import threading

    from gateway.run import TurnRunner

    monkeypatch.setattr(
        "agent.fast_lane.patch_session_fast_lane_marker",
        lambda *_a, **_k: None,
    )
    ctx = _make_ctx("сколько будет 2+2?")
    gr = _gateway_runner(session_override=_pinned_state("max"))

    cached = SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "max"},
        max_iterations=None,
        run_budget_seconds=None,
        review_exempt=False,
        run_conversation=lambda *_a, **_k: {
            "final_response": "ok",
            "messages": [],
            "failed": False,
        },
        tools=[],
        context_compressor=SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000
        ),
        session_prompt_tokens=0,
        session_completion_tokens=0,
        _last_activity_ts=0,
    )
    gr._agent_cache = OrderedDict()
    gr._agent_cache_lock = threading.Lock()
    gr._agent_cache[ctx.session_key] = (cached, ("test-signature",))

    TurnRunner(gr, ctx).run_sync()
    assert cached.reasoning_config == {"enabled": True, "effort": "max"}


def test_letovo_wire_never_sees_medium_on_a_fast_turn(monkeypatch):
    """The resolved fast level is clamped to the letovo domain before the wire."""
    captured = _run(monkeypatch, "сколько будет 2+2?", session_override=None)
    effort = captured["reasoning_config"]["effort"]
    assert effort not in ("medium", "minimal")
    assert effort == "low"


# ---------------------------------------------------------------------------
# Session-marker evidence (operator ruling: prove the level was APPLIED by the
# marker, never by counting reasoning tokens — the within-level spread
# `off+low` 33/32/52 and `off+high` 32/50/50 swamps the between-level delta).
# ---------------------------------------------------------------------------


def _sqlite_session_db():
    import json
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
    con.execute(
        "INSERT INTO sessions (id, model_config) VALUES ('sess-rgz', '{}')"
    )
    con.commit()

    class FakeDB:
        def patch_session_model_config(self, sid, patch):
            row = con.execute(
                "SELECT model_config FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if row is None:
                return
            cur = json.loads(row[0]) if row[0] else {}
            for k, v in patch.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            con.execute(
                "UPDATE sessions SET model_config=? WHERE id=?",
                (json.dumps(cur), sid),
            )
            con.commit()

        def get_session_model_config_value(self, sid, key, default=None):
            row = con.execute(
                "SELECT model_config FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if row is None or not row[0]:
                return default
            return (json.loads(row[0]) or {}).get(key, default)

        def ensure_session(self, *_a, **_k):
            return None

    return con, FakeDB()


def test_marker_records_low_for_an_ordinary_turn():
    """The persisted marker carries `low` — the observable that the level landed."""
    from agent.fast_lane import (
        decide_slow_mode,
        patch_session_fast_lane_marker,
        resolve_fast_path,
    )

    _con, db = _sqlite_session_db()
    text = "сколько будет 2+2?"
    d = resolve_fast_path(
        decide_slow_mode(text=text),
        text,
        reasoning_pin="low",
        wire_base_url="https://llm.letovo.site/openai",
        wire_model="deepseek-v4-flash",
    )
    patch_session_fast_lane_marker(db, "sess-rgz", d)
    assert db.get_session_model_config_value("sess-rgz", "reasoning_effort") == "low"
    assert db.get_session_model_config_value("sess-rgz", "fast_lane") is True
    assert db.get_session_model_config_value("sess-rgz", "slow_mode") is False


def test_marker_records_the_slow_branch_on_an_rgz_phrase():
    """An RGZ phrase resolves SLOW and the marker says so (no fast-lane pin)."""
    from agent.fast_lane import (
        decide_slow_mode,
        patch_session_fast_lane_marker,
        resolve_fast_path,
    )

    _con, db = _sqlite_session_db()
    text = "гигантский план миссии"
    d = resolve_fast_path(
        decide_slow_mode(text=text),
        text,
        reasoning_pin="low",
        wire_base_url="https://llm.letovo.site/openai",
        wire_model="deepseek-v4-flash",
    )
    patch_session_fast_lane_marker(db, "sess-rgz", d)
    assert d.slow_mode is True
    assert db.get_session_model_config_value("sess-rgz", "slow_mode") is True
    assert db.get_session_model_config_value("sess-rgz", "review_exempt") is False
    # The slow branch keeps full-lane semantics; the fast pin cannot touch it.
    assert db.get_session_model_config_value("sess-rgz", "reasoning_effort") == "high"
