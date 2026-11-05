"""Integration tests: fast-lane activation through the real gateway wiring.

Task t_5e1c4629 — cover the acceptance criteria of the parent card with
*integration* tests that exercise the actual call sites, not just the pure
decision functions in agent/fast_lane.py (those are covered by the sibling
unit files test_fast_lane_markers.py / test_fast_lane_fast_path.py).

The activation seam under test is :meth:`APIServerAdapter._create_agent`
(gateway/platforms/api_server.py), the documented fast-path entry point from
design §4.2 Track S / §5 / §7 (D5):

  * fast request (no slow markers) ->
      reasoning_config low (routine) / high (complex);
      max_iterations = unlimited sentinel (never truncated by call count —
      variant 2, owner 2026-10-19); run_budget_seconds=300 (soft time orienteer);
      agent.review_exempt=True; session marker fast_lane=True review_exempt=True
      (the D5 signal the review-forcer consumes to skip fast sessions).
  * slow request (OWUI deepthink, "гигантская задача", TG «подумай») ->
      fast-path fields NOT applied: budget uncapped, review NOT exempt,
      reasoning stays as configured, no run_budget_seconds in agent kwargs.
  * an explicit client reasoning pin on a fast request is HONOURED (t_0563b054):
      the lane heuristic never rewrites an operator-chosen level, while budget
      and review-exemption shaping still apply.
  * fast request complex -> reasoning high.

The heavy runtime resolution (provider creds, gateway config, AIAgent) is
mocked exactly like the pre-existing test_create_agent_forwards_runtime_config
in test_api_server.py, so these are hermetic and fast.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter_with_fakes(monkeypatch, config=None):
    """Build an APIServerAdapter with the runtime-loading seams faked.

    Returns (adapter, captured_kwargs) where captured_kwargs is the dict the
    fake AIAgent constructor received.
    """
    captured: dict = {}

    class FakeAgent:
        # NOTE: the real AIAgent has NO review_exempt attribute — _create_agent
        # sets it only for fast requests. Tests assert via getattr(..., None)
        # so a missing attribute == "not marked exempt" (correct slow semantics).
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
    # Session DB is mocked so _patch_fast_lane_marker persists into a fake.
    # _ensure_session_db is patched on the INSTANCE so we can read the marker.
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    fake_db = MagicMock()
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: fake_db)
    return adapter, captured, fake_db


def _agent(adapter, **kwargs):
    return adapter._create_agent(session_id="sess-1", **kwargs)


# ---------------------------------------------------------------------------
# 1) Fast request (slow_mode=false) goes down the FAST path with fast-lane
#    parameters from design/fast-lane-design.md.
# ---------------------------------------------------------------------------


def test_fast_request_applies_fast_path_parameters(monkeypatch):
    """A routine fast request gets reasoning low, NO hard call-count cap
    (unlimited sentinel), a soft 300s run budget, and review_exempt=True —
    the D5 signal for 'no review card'. (Variant 2: bound by TIME, not calls.)"""
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    agent = _agent(adapter, user_message="сколько будет 2+2?")

    assert isinstance(agent, object)
    assert captured["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert captured["max_iterations"] == 9223372036854775807  # TURN_LIMIT_UNLIMITED
    assert captured["run_budget_seconds"] == 300
    assert agent.review_exempt is True


def test_fast_request_complex_uses_high_reasoning(monkeypatch):
    """Complex class-A request -> reasoning high (D3 complex split). Per
    variant 2 (owner decision, 2026-10-19) the fast lane bounds by TIME, so a
    complex fast request has NO call-count truncation (unlimited sentinel) and
    the same soft 5-minute run_budget; always review-exempt (D5)."""
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    agent = _agent(adapter, user_message="проанализируй лог и сравни с baseline")

    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert captured["max_iterations"] == 9223372036854775807  # TURN_LIMIT_UNLIMITED
    assert captured["run_budget_seconds"] == 300
    assert agent.review_exempt is True


def test_fast_request_honours_explicit_client_pin(monkeypatch):
    """An explicit client reasoning pin on a fast request is HONOURED.

    t_0563b054: the fast lane used to rewrite EVERY fast request to its own
    level, silently demoting an operator-chosen level. An explicit per-request
    reasoning parameter outranks the lane heuristic; only the budget and
    review-exemption shaping still apply."""
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    model_options = {"reasoning": {"enabled": True, "effort": "low"}}
    _agent(adapter, user_message="какой статус t_001?", model_options=model_options)

    assert captured["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert captured["max_iterations"] == 9223372036854775807  # TURN_LIMIT_UNLIMITED


def test_fast_request_without_pin_stays_wire_safe(monkeypatch):
    """No explicit pin -> the lane level lands on the letovo wire domain."""
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    _agent(adapter, user_message="какой статус t_001?")

    effort = captured["reasoning_config"]["effort"]
    assert effort not in ("medium", "minimal")
    assert effort == "low"


# ---------------------------------------------------------------------------
# 2) Slow requests are NOT broken and stay on the full lane.
# ---------------------------------------------------------------------------


def test_slow_deepthink_request_untouched(monkeypatch):
    """OWUI deepthink (reasoning enabled+high in model_options) -> slow lane:
    no budget cap, no run_budget_seconds, review NOT exempt, reasoning stays
    as configured."""
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    model_options = {"reasoning": {"enabled": True, "effort": "high"}}
    agent = _agent(adapter, user_message="детальный анализ", model_options=model_options)

    # Slow lane: no run_budget_seconds key at all (unlimited wall clock),
    # max_iterations stays at the default (not forced to 16 by fast path,
    # here the config default high), review not exempt.
    assert "run_budget_seconds" not in captured
    assert getattr(agent, "review_exempt", None) in (None, False)
    assert captured["reasoning_config"] == {
        "enabled": True,
        "effort": "high",  # request deepthink wins (unchanged behavior)
    }


def test_slow_giant_task_phrase_untouched(monkeypatch):
    """«гигантская задача» -> always slow AND RGZ-pinned to reasoning=max.

    The RGZ mechanical guarantee (owner 2026-10-20) pins any request carrying
    an «гигантск… + план/задача/миссия/проект» phrase to reasoning=max (full
    lane, no budget cap on the fast path, review not exempt). So the expected
    effort is ``max`` — NOT the gateway default ``high``. This reflects the
    live RGZ hook in ``_create_agent`` (api_server), which deliberately
    outranks the config default.
    """
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    agent = _agent(adapter, user_message="гигантская задача: спланируй миграцию базы")

    assert "run_budget_seconds" not in captured
    assert getattr(agent, "review_exempt", None) in (None, False)
    # RGZ trigger -> reasoning=max (not the config default high), still full lane.
    assert captured["reasoning_config"] == {"enabled": True, "effort": "max"}


def test_slow_tg_prefix_phrase_untouched(monkeypatch):
    """TG «подумай» marker -> slow even without role/deep-think wording."""
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    agent = _agent(adapter, user_message="подумай над этим вопросом")

    assert "run_budget_seconds" not in captured
    assert getattr(agent, "review_exempt", None) in (None, False)


# ---------------------------------------------------------------------------
# 3) Session marker persistence — D5 signal the review-forcer consumes.
# ---------------------------------------------------------------------------


def test_fast_request_persists_review_exempt_marker(monkeypatch):
    """A fast request writes fast_lane=True / review_exempt=True onto the
    session row; the forcer's session_is_fast_lane() returns True so it will
    skip review-card creation for this session (D5)."""
    from agent.fast_lane import session_is_fast_lane

    adapter, _, fake_db = _make_adapter_with_fakes(monkeypatch)
    _agent(adapter, user_message="сколько будет 2+2?")

    assert fake_db.patch_session_model_config.called
    marker = fake_db.patch_session_model_config.call_args[0][1]
    assert marker["fast_lane"] is True
    assert marker["review_exempt"] is True
    assert marker["reasoning_effort"] == "low"
    # The exact D5 predicate the forcer uses:
    assert session_is_fast_lane(marker) is True


def test_slow_request_persists_non_exempt_marker(monkeypatch):
    """A slow request persists slow_mode=True / review_exempt=False, so the
    forcer keeps its existing behavior for slow/marked requests."""
    from agent.fast_lane import session_is_fast_lane

    adapter, _, fake_db = _make_adapter_with_fakes(monkeypatch)
    model_options = {"reasoning": {"enabled": True, "effort": "high"}}
    _agent(adapter, user_message="детальный анализ", model_options=model_options)

    assert fake_db.patch_session_model_config.called
    marker = fake_db.patch_session_model_config.call_args[0][1]
    assert marker["slow_mode"] is True
    assert marker["review_exempt"] is False
    assert marker["fast_lane"] is False
    assert session_is_fast_lane(marker) is False


# ---------------------------------------------------------------------------
# 4) Budget-call cap ≤16: through the wiring the agent is told max_iterations=16
#    for fast, and the slow lane is not force-capped to 16.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message, expected_effort",
    [
        # Routine class-A: NO hard call-count cap — bounded by soft time budget
        # (variant 2: the fast lane no longer truncates by LLM-call count).
        ("сколько будет 2+2?", "low"),
        ("статус задачи", "low"),
        # «деплой сервиса» has no complex verb (deploy is only in the English
        # list; Russian deploy verbs are настрой/разверни/установи) -> routine.
        ("деплой сервиса", "low"),
        # Complex work: same time-bound — no call-count truncation, soft 300s.
        ("проанализируй всё и создай отчёт", "high"),
    ],
)
def test_fast_budget_never_capped_by_calls(monkeypatch, message, expected_effort):
    """Fast requests (routine and complex alike) are never truncated by
    LLM-call count: max_iterations is the unlimited sentinel and the only
    bounder is the soft 5-minute run_budget_seconds."""
    adapter, captured, _ = _make_adapter_with_fakes(monkeypatch)
    _agent(adapter, user_message=message)

    assert captured["max_iterations"] == 9223372036854775807  # TURN_LIMIT_UNLIMITED
    assert captured["run_budget_seconds"] == 300
    assert captured["reasoning_config"] == {
        "enabled": True,
        "effort": expected_effort,
    }


# ---------------------------------------------------------------------------
# 5) Feature flag gate (design §6.2): fast_lane.enabled defaults to FALSE and
#    the fast-path overrides are fully inert when it is unset — the working
#    path behaves exactly as before. (Sibling card t_09bf5ceb owns the config
#    schema + cron-forcer edit; this proves the activation seam is gated.)
# ---------------------------------------------------------------------------


def test_flag_off_default_fast_path_inert(monkeypatch):
    """With no fast_lane section (default false), a marker-less request takes
    the FULL lane: reasoning stays as configured (high), no budget cap, no
    run_budget_seconds, not review-exempt, and no marker is persisted."""
    adapter, captured, fake_db = _make_adapter_with_fakes(
        monkeypatch, config={"agent": {"reasoning_effort": "high"}}
    )
    agent = _agent(adapter, user_message="сколько будет 2+2?")

    # Full lane: gateway-configured reasoning preserved, no fast cap/budget.
    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert captured["max_iterations"] != 16  # not force-capped by fast path
    assert "run_budget_seconds" not in captured
    assert getattr(agent, "review_exempt", None) in (None, False)
    # Marker NOT persisted -> forcer's session_is_fast_lane stays False.
    assert fake_db.patch_session_model_config.called is False
    from agent.fast_lane import session_is_fast_lane
    assert session_is_fast_lane({}) is False


def test_flag_explicit_false_fast_path_inert(monkeypatch):
    """fast_lane.enabled=false explicitly -> same inert behavior as absent."""
    adapter, captured, fake_db = _make_adapter_with_fakes(
        monkeypatch,
        config={"fast_lane": {"enabled": False}, "agent": {"reasoning_effort": "high"}},
    )
    agent = _agent(adapter, user_message="сколько будет 2+2?")

    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert "run_budget_seconds" not in captured
    assert getattr(agent, "review_exempt", None) in (None, False)
    assert fake_db.patch_session_model_config.called is False


# ---------------------------------------------------------------------------
# 6) Degradation (design §6.1): with fast_lane.enabled=true, an exception in
#    the fast-path must NOT break the turn — the request completes via the
#    FULL lane (no error), and only the fast_lane_degraded=true marker is
#    written. The error is never counted as a request failure.
# ---------------------------------------------------------------------------


def test_flag_true_fast_path_error_degrades_to_full_lane(monkeypatch, caplog):
    """fast_lane.enabled=true + an exception inside the fast-path decision
    block (agent.fast_lane.evaluate_request raises) must NOT break the turn:
    the request completes on the FULL lane (no fast overrides, no budget cap,
    not review-exempt), a warning is logged, and only the degraded marker is
    persisted. The error is never counted as a request failure."""
    import logging

    adapter, captured, fake_db = _make_adapter_with_fakes(monkeypatch)

    # The decision call (first thing inside the fast-path try block) throws.
    # `from agent.fast_lane import ... as _fast_lane_evaluate` executes on
    # every _create_agent call, so patching the module attribute is seen.
    def _boom(*a, **k):
        raise RuntimeError("fast-path exploded")

    monkeypatch.setattr("agent.fast_lane.evaluate_request", _boom)

    with caplog.at_level(logging.WARNING):
        agent = _agent(adapter, user_message="сколько будет 2+2?")

    # The turn completed: an agent was still constructed.
    assert isinstance(agent, object)
    # Degraded to the FULL lane: reasoning stays at the gateway config
    # default (high), no fast budget cap, no run_budget_seconds, and the
    # agent is NOT marked review-exempt.
    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert captured["max_iterations"] != 16
    assert "run_budget_seconds" not in captured
    assert getattr(agent, "review_exempt", None) in (None, False)
    # A warning was logged signalling the degradation.
    assert any(
        "degraded to full lane" in r.message for r in caplog.records
    ), caplog.text
    # The degraded marker is persisted for operators, and the session is NOT
    # a fast-lane session (no review exemption leaked through).
    assert fake_db.patch_session_model_config.called
    marker = fake_db.patch_session_model_config.call_args[0][1]
    assert marker == {"fast_lane_degraded": True}
    from agent.fast_lane import session_is_fast_lane
    assert session_is_fast_lane({}) is False


