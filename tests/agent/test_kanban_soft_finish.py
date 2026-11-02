"""Tests for the kanban soft finish threshold (t_ed744fd2).

Covers the acceptance criteria of the card:

1. the threshold is computed from the run's REAL budget
   (``api_call_count / agent.max_iterations``), never from a constant;
2. the directive is injected exactly once and is present in the worker's
   context (cache-safe: appended to the newest tool result);
3. firing leaves an explicit log line for later audit;
4. if the worker does not call a terminal board tool within the grace
   window, the card is closed ``done`` with the summary the worker already
   formed — not recorded ``timed_out``;
5. regression: a short routine card (≤30 steps) never sees the directive and
   is never force-closed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent import kanban_soft_finish as soft

# Originals captured at import time: the autouse fixture below replaces the
# module attributes, so config-parsing tests call these directly.
_REAL = {
    "ratio": soft.soft_finish_ratio,
    "grace": soft.soft_finish_grace_steps,
    "floor": soft.soft_finish_min_budget_steps,
    "enabled": soft.soft_finish_enabled,
}


class _Agent:
    """Minimal AIAgent double — only what the policy module reads."""

    def __init__(self, max_iterations=200):
        self.max_iterations = max_iterations
        self.quiet_mode = True
        self._kanban_soft_finish_injected = False
        self._kanban_soft_finish_at_step = None
        self._kanban_soft_finish_state = None
        self._kanban_soft_finish_forced = False
        self.statuses = []

    def _emit_status(self, text):
        self.statuses.append(text)


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    """Arm every fence so the tests exercise the threshold, not the fencing.

    The three fences themselves are covered by tests/agent/test_kanban_stop.py
    (this module delegates to the same predicates); here we want a genuine
    "worker is live and past the threshold" state.
    """
    from agent import kanban_stop as ks

    monkeypatch.setattr(ks, "is_dispatcher_worker_env", lambda: True)
    monkeypatch.setattr(ks, "_is_owner_context", lambda: True)
    monkeypatch.setattr(ks, "kanban_stop_nudge_enabled", lambda: True)
    # The threshold policy functions read config.yaml; pin them so the tests
    # do not depend on the host's kanban section. The functions themselves are
    # covered by test_ratio_and_grace_have_sane_defaults_and_clamps, which
    # calls the originals (captured before patching).
    monkeypatch.setattr(soft, "soft_finish_enabled", lambda kanban_cfg=None: True)
    monkeypatch.setattr(soft, "soft_finish_ratio", lambda kanban_cfg=None: 0.8)
    monkeypatch.setattr(soft, "soft_finish_grace_steps", lambda kanban_cfg=None: 12)
    monkeypatch.setattr(soft, "soft_finish_min_budget_steps", lambda kanban_cfg=None: 60)
    monkeypatch.delenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_STARTED_AT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_SOFT_FINISH", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_soft")
    return True


def _tool_messages(n=1):
    """A transcript that ends with a tool result (the usual worker shape)."""
    msgs = [
        {"role": "user", "content": "work kanban task t_test_soft"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "terminal", "arguments": "{}"}},
        ]},
    ]
    for i in range(n):
        msgs.append({"role": "tool", "tool_call_id": "c1", "name": "terminal",
                     "content": f"tool output {i}"})
    return msgs


# ── 1. threshold from the real budget ────────────────────────────────────────


@pytest.mark.parametrize(
    "max_iterations,steps,expected",
    [
        (200, 159, False),   # 79.5% — before the threshold
        (200, 160, True),    # exactly 80%
        (200, 200, True),
        (180, 143, False),   # the t_316f89ca card: 143/180 = 79.4%
        (180, 144, True),
        (150, 119, False),
        (150, 120, True),
        (300, 240, True),
        (30, 12, False),     # short routine card: step arm inert (see floor)
        (30, 30, False),     # still inert at the hard cap — only time can arm
        (61, 48, False),     # just above the floor: 78.7%
        (61, 49, True),      # 80.3%
    ],
)
def test_threshold_uses_real_budget(max_iterations, steps, expected):
    agent = _Agent(max_iterations)
    assert soft.soft_finish_applies(agent, steps) is expected


def test_unlimited_budget_never_fires():
    """``sys.maxsize`` is the core "no cap" sentinel, not a real budget."""
    agent = _Agent(max_iterations=2**63 - 1)
    assert soft.step_budget(agent) == 0
    assert soft.soft_finish_applies(agent, 10_000) is False


def test_wall_clock_arm_uses_card_runtime(monkeypatch):
    """The time arm fires at 80% of the card's own max_runtime_seconds."""
    import time as _time

    now = _time.time()
    monkeypatch.setenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", "3600")
    monkeypatch.setenv("HERMES_KANBAN_RUN_STARTED_AT", str(int(now - 2790)))  # 77.5%
    agent = _Agent(max_iterations=10_000)  # step arm far away
    assert soft.soft_finish_applies(agent, 5) is False
    monkeypatch.setenv("HERMES_KANBAN_RUN_STARTED_AT", str(int(now - 2900)))  # 80.6%
    assert soft.soft_finish_applies(agent, 5) is True


def test_budget_state_reports_both_arms(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", "600")
    monkeypatch.setenv("HERMES_KANBAN_RUN_STARTED_AT", "1000")
    state = soft.budget_state(_Agent(200), 100)
    assert state["steps_used"] == 100
    assert state["steps_max"] == 200
    assert state["step_fraction"] == pytest.approx(0.5)
    assert state["time_limit"] == 600
    assert state["fraction"] >= state["step_fraction"]


# ── 2. one-shot injection into the worker's context ─────────────────────────


def test_directive_injected_once_into_newest_tool_result(caplog):
    agent = _Agent(200)
    messages = _tool_messages()

    with caplog.at_level(logging.INFO, logger="agent.kanban_soft_finish"):
        assert soft.inject_directive(agent, messages, 160) is True
        # Second call is a no-op: the latch holds.
        assert soft.inject_directive(agent, messages, 161) is False

    assert agent._kanban_soft_finish_injected is True
    assert agent._kanban_soft_finish_at_step == 160
    # Visible in the worker's context, on the tool result it will send next.
    assert soft.SOFT_FINISH_DIRECTIVE.split("{")[0][:20] in messages[-1]["content"]
    assert "kanban_complete" in messages[-1]["content"]
    # One user row only — no synthetic alternation break.
    assert [m["role"] for m in messages].count("user") == 1
    # 3. audit line present
    assert any("kanban soft finish: directive injected" in r.message for r in caplog.records)


def test_directive_appended_to_multimodal_tool_result():
    agent = _Agent(200)
    messages = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "c1", "name": "browser_read",
         "content": [{"type": "text", "text": "page body"}]},
    ]
    assert soft.inject_directive(agent, messages, 170) is True
    blocks = messages[-1]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == "page body"
    assert "kanban_complete" in blocks[-1]["text"]


def test_text_only_transcript_gets_flagged_synthetic_row():
    """No tool result at all → flagged user row, never persisted."""
    agent = _Agent(200)
    messages = [{"role": "assistant", "content": "thinking out loud"}]
    assert soft.inject_directive(agent, messages, 161) is True
    assert messages[-1]["role"] == "user"
    assert messages[-1]["_kanban_soft_finish_synthetic"] is True


def test_never_stacks_two_user_rows():
    agent = _Agent(200)
    messages = [{"role": "user", "content": "go"}]
    assert soft.inject_directive(agent, messages, 161) is False
    assert agent._kanban_soft_finish_injected is False


def test_terminal_tool_already_called_suppresses_directive():
    from agent.kanban_stop import session_called_kanban_terminal

    messages = _tool_messages() + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "kanban_complete", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c2", "name": "kanban_complete",
         "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert soft._terminal_tool_called(messages) is True


def test_directive_text_carries_the_required_imperatives():
    text = soft.SOFT_FINISH_DIRECTIVE
    assert "kanban_complete" in text
    assert "Stop all new work" in text
    assert "kanban_block" in text


# ── 4/5. grace + forced close ────────────────────────────────────────────────


def test_grace_window_counts_from_the_directive_step():
    agent = _Agent(300)
    messages = _tool_messages()
    assert soft.grace_expired(agent, 240) is False  # no directive yet
    soft.inject_directive(agent, messages, 240)
    assert soft.grace_expired(agent, 240 + 11) is False
    assert soft.grace_expired(agent, 240 + 12) is True


def test_short_card_never_armed():
    """Regression (acceptance #3): a routine ≤30-step card is untouched."""
    agent = _Agent(30)
    messages = _tool_messages()
    for step in range(1, 31):
        assert soft.soft_finish_applies(agent, step) is False
    assert agent._kanban_soft_finish_injected is False
    assert soft.grace_expired(agent, 30) is False
    # Directly injecting for such a card must also be impossible through the
    # normal loop path (soft_finish_applies gates it), and no wall clock is
    # configured, so nothing can arm it.
    assert soft.budget_state(agent, 30)["step_arm_active"] is False


def test_force_complete_refuses_without_binding(monkeypatch):
    """Fail-closed: no live binding → no board write."""
    import hermes_cli.kanban_db as kb

    called = {}

    def _fake_binding(conn, task_id, run_id, assignee, session_id):
        called["checked"] = (task_id, run_id, assignee, session_id)
        return False

    def _fake_complete(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("complete_task must not run without a live binding")

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding", _fake_binding)
    monkeypatch.setattr(kb, "complete_task", _fake_complete)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-1")

    assert soft.force_complete(messages=_tool_messages()) is False
    assert called["checked"] == ("t_test_soft", 42, "03-software-engineer", "sess-1")


def test_force_complete_writes_done_summary(monkeypatch, caplog):
    """The card is closed ``done`` with the progress the worker had."""
    import hermes_cli.kanban_db as kb

    captured = {}

    def _fake_binding(conn, task_id, run_id, assignee, session_id):
        return True

    def _fake_complete(conn, task_id, **kwargs):
        captured["task_id"] = task_id
        captured.update(kwargs)
        return True

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding", _fake_binding)
    monkeypatch.setattr(kb, "complete_task", _fake_complete)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-2")

    messages = _tool_messages()
    messages.insert(1, {"role": "assistant", "content": "Аудит собран: 13 тезисов."})
    budget = {"steps_used": 172, "steps_max": 200, "fraction": 0.86,
              "time_elapsed": None, "time_limit": None}

    with caplog.at_level(logging.WARNING, logger="agent.kanban_soft_finish"):
        ok = soft.force_complete(
            reason="soft_finish_grace_expired", messages=messages, budget=budget
        )

    assert ok is True
    assert captured["task_id"] == "t_test_soft"
    assert captured["expected_run_id"] == 7
    assert "13 тезисов" in captured["summary"]
    assert captured["metadata"]["soft_finish_forced"] is True
    assert captured["metadata"]["budget_used"] == 172
    assert any("closed done forcibly" in r.message for r in caplog.records)


def test_force_complete_requires_run_identity(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert soft.force_complete(messages=_tool_messages()) is False


def test_force_complete_swallows_board_errors(monkeypatch):
    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_PROFILE", "p")
    assert soft.force_complete(messages=_tool_messages()) is False


# ── config plumbing ─────────────────────────────────────────────────────────


def test_ratio_and_grace_have_sane_defaults_and_clamps():
    """The originals (unpatched) — config parsing must be tolerant."""
    real_ratio = _REAL["ratio"]
    real_grace = _REAL["grace"]
    real_floor = _REAL["floor"]
    real_enabled = _REAL["enabled"]

    assert real_ratio({}) == pytest.approx(soft.DEFAULT_SOFT_FINISH_RATIO)
    assert real_ratio({"soft_finish_ratio": 0.5}) == 0.5
    assert real_ratio({"soft_finish_ratio": "bogus"}) == soft.DEFAULT_SOFT_FINISH_RATIO
    assert real_ratio({"soft_finish_ratio": 0.0}) == 0.10  # clamped up
    assert real_ratio({"soft_finish_ratio": 5}) == 0.95  # clamped down
    assert real_grace({}) == soft.DEFAULT_SOFT_FINISH_GRACE_STEPS
    assert real_grace({"soft_finish_grace_steps": 3}) == 3
    assert real_grace({"soft_finish_grace_steps": -5}) == 0
    assert real_floor({}) == soft.DEFAULT_SOFT_FINISH_MIN_BUDGET_STEPS
    assert real_floor({"soft_finish_min_budget_steps": 10}) == 10
    assert real_enabled({"soft_finish": False}) is False
    assert real_enabled({"soft_finish": "off"}) is False
    assert real_enabled({"soft_finish": True}) is True
    assert real_enabled({}) is True


def test_soft_finish_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_SOFT_FINISH", "0")
    assert _REAL["enabled"]() is False


def test_min_budget_steps_keeps_short_cards_inert():
    """A 30-step card: 80% would be step 24, far too early to say 'finish'."""
    agent = _Agent(30)
    state = soft.budget_state(agent, 29)
    assert state["step_arm_active"] is False
    assert state["step_fraction"] == 0.0
    assert soft.soft_finish_applies(agent, 29) is False
