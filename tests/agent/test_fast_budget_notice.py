"""Unit tests for the fast-lane budget overflow USER notice (part 3, «не молчать»).

Task t_a7e7d08a — fast requests on profile 11 must not "silently die" when
they exceed the soft ~5-minute wall-clock budget. Part 1 already removed the
LLM-call-count truncation (max_iterations=TURN_LIMIT_UNLIMITED, soft
run_budget_seconds=300). Part 3 adds a user-visible overflow notice:

  * ``agent.conversation_loop._fire_fast_budget_user_notice`` fires the
    one-shot user notice through the dedicated non-content callback
    (``agent._fast_budget_notice_callback``) at the same 80% wall-clock
    threshold that latches the model wrap-up notice. Never content deltas —
    render purity (OWUI final-answer bubble) is preserved.
  * ``agent.turn_finalizer.finalize_turn`` appends the durable options block
    to the final assistant response when the latch fired (covers streaming
    paths where the transient status pill may be missed and the JSON paths).

Covered here (unit): the latch semantics, callback firing, one-time-ness,
inert-ness when no callback/flag/budget, and the durable footer appending in
finalize_turn. Instrumental continuity: the existing fast-lane suites
(tests/agent/test_fast_lane_fast_path.py, tests/gateway/test_fast_lane_*.py)
keep passing unchanged.
"""

from __future__ import annotations

import time

import pytest


# ── _fire_fast_budget_user_notice ─────────────────────────────────────────


class _StubAgent:
    """Minimal agent stub mirroring the attributes the notice path touches."""

    def __init__(
        self,
        budget=300,
        started=None,
        wrapup_injected=True,
        callback=None,
        latch=None,
    ):
        self.run_budget_seconds = budget
        self._run_budget_started_at = started if started is not None else time.time() - 400
        self._run_budget_wrapup_injected = wrapup_injected
        self._fast_budget_notice_callback = callback
        self._fast_budget_user_notice_fired = (
            latch if latch is not None else False
        )


def test_notice_fires_once_with_callback() -> None:
    from agent.conversation_loop import _fire_fast_budget_user_notice
    from agent.fast_budget_text import FAST_BUDGET_USER_NOTICE

    fired: list = []

    def _cb(text: str) -> None:
        fired.append(text)

    agent = _StubAgent(budget=300, callback=_cb)
    assert _fire_fast_budget_user_notice(agent) is True
    assert fired == [FAST_BUDGET_USER_NOTICE]
    assert agent._fast_budget_user_notice_fired is True

    # One-shot: second call does not re-fire.
    assert _fire_fast_budget_user_notice(agent) is False
    assert fired == [FAST_BUDGET_USER_NOTICE]


def test_notice_inert_without_model_wrapup_latch() -> None:
    """No user notice before the internal wrap-up latch (same 80% threshold)."""
    from agent.conversation_loop import _fire_fast_budget_user_notice

    fired: list = []
    agent = _StubAgent(
        budget=300,
        wrapup_injected=False,
        callback=lambda t: fired.append(t),
    )
    assert _fire_fast_budget_user_notice(agent) is False
    assert fired == []
    assert agent._fast_budget_user_notice_fired is False


def test_notice_inert_without_callback() -> None:
    """No consumer wired (CLI / non-fast gateway) -> no fire, no crash."""
    from agent.conversation_loop import _fire_fast_budget_user_notice

    agent = _StubAgent(budget=300, callback=None)
    assert _fire_fast_budget_user_notice(agent) is False


def test_notice_inert_without_budget() -> None:
    from agent.conversation_loop import _fire_fast_budget_user_notice

    fired: list = []
    agent = _StubAgent(
        budget=None,
        callback=lambda t: fired.append(t),
    )
    assert _fire_fast_budget_user_notice(agent) is False
    assert fired == []


def test_notice_already_fired_is_latched() -> None:
    from agent.conversation_loop import _fire_fast_budget_user_notice

    fired: list = []
    agent = _StubAgent(
        budget=300,
        callback=lambda t: fired.append(t),
        latch=True,
    )
    assert _fire_fast_budget_user_notice(agent) is False
    assert fired == []


def test_notice_callback_exception_is_swallowed() -> None:
    """A raising consumer must not propagate into the turn loop."""
    from agent.conversation_loop import _fire_fast_budget_user_notice

    def _boom(text: str) -> None:
        raise RuntimeError("consumer down")

    agent = _StubAgent(budget=300, callback=_boom)
    assert _fire_fast_budget_user_notice(agent) is False
    assert agent._fast_budget_user_notice_fired is False


def test_notice_requires_elapsed_budget_like_wrapup() -> None:
    """The notice path only runs after the model wrap-up latched, which itself
    enforces the 80% threshold; a not-yet-latched agent cannot fire."""
    from agent.conversation_loop import _fire_fast_budget_user_notice

    fired: list = []
    agent = _StubAgent(
        budget=300,
        started=time.time() - 10,  # only 3% elapsed
        wrapup_injected=False,      # wrap-up not yet latched
        callback=lambda t: fired.append(t),
    )
    assert _fire_fast_budget_user_notice(agent) is False
    assert fired == []


# ── FAST_BUDGET_USER_NOTICE text contract ─────────────────────────────────


def test_notice_text_contains_options_and_status() -> None:
    """Format-minimum per task: status + три варианта (ждать / карточка /
    «подумай») in one user-visible block."""
    from agent.fast_budget_text import FAST_BUDGET_USER_NOTICE

    assert "не уложилас" in FAST_BUDGET_USER_NOTICE
    assert "продолжить как есть" in FAST_BUDGET_USER_NOTICE
    assert "в карточку" in FAST_BUDGET_USER_NOTICE
    assert "подумай" in FAST_BUDGET_USER_NOTICE


# ── durable footer in finalize_turn ───────────────────────────────────────


def test_finalize_appends_fast_budget_footer_when_fired() -> None:
    """Final response carries the options block durably once the latch fired."""
    from unittest.mock import MagicMock

    from agent.fast_budget_text import FAST_BUDGET_USER_NOTICE
    from agent.turn_finalizer import _append_fast_budget_footer

    agent = MagicMock()
    agent._fast_budget_user_notice_fired = True

    out = _append_fast_budget_footer("Ответ готов", agent)
    assert out.endswith(FAST_BUDGET_USER_NOTICE)
    assert "Ответ готов\n\n" in out


def test_finalize_skips_footer_when_not_fired() -> None:
    """No latch -> final response untouched (short fast answers stay clean)."""
    from unittest.mock import MagicMock

    from agent.turn_finalizer import _append_fast_budget_footer

    agent = MagicMock()
    agent._fast_budget_user_notice_fired = False

    out = _append_fast_budget_footer("Короткий ответ", agent)
    assert out == "Короткий ответ"


def test_finalize_skips_footer_on_empty_response() -> None:
    """No real conversational answer -> untouched."""
    from unittest.mock import MagicMock

    from agent.turn_finalizer import _append_fast_budget_footer

    agent = MagicMock()
    agent._fast_budget_user_notice_fired = True

    assert _append_fast_budget_footer("", agent) == ""
    assert _append_fast_budget_footer(None, agent) is None


def test_imports_resolve() -> None:
    """The module path used by the conversation loop and finalizer resolves."""
    from agent.conversation_loop import _fire_fast_budget_user_notice  # noqa: F401
    from agent.fast_budget_text import FAST_BUDGET_USER_NOTICE  # noqa: F401
    from agent.turn_finalizer import _append_fast_budget_footer  # noqa: F401
    assert isinstance(FAST_BUDGET_USER_NOTICE, str) and FAST_BUDGET_USER_NOTICE.strip()
