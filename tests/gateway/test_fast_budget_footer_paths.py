"""Fast-lane part 3: durable overflow footer on callback-less _run_agent paths.

Review t_a7e7d08a R1 (09-independent-reviewer): the durable footer was gated
on ``_fast_budget_user_notice_fired``, which only latches when a live notice
callback is wired — and that callback is threaded into only ONE of the six
``_run_agent`` call sites (gateway/platforms/api_server.py:6310, the OWUI SSE
path).  The other five — non-streaming ``/v1/chat/completions``
(api_server.py:6336), ``/v1/responses`` stream=true (7550) and stream=false
``_compute_response()`` (7590), session chat (5522) and session chat stream
(5698) — never pass ``fast_budget_notice_callback``, so the user got NOTHING
(a "silent" run) when a fast request crossed ~5 minutes.  Exactly what the
task forbids.

Fix (agreed variant 1): the durable footer now gates on the generic overflow
signal — ``run_budget_seconds`` set AND ``_run_budget_wrapup_injected``
latched — which latches on EVERY path whenever 80% of the soft budget elapsed
(see ``conversation_loop._maybe_inject_run_budget_wrapup``), independent of
the live callback.  The live callback remains solely the transient OWUI
status-pill channel.

Module is deliberately self-contained (mirrors
``tests/gateway/test_api_server_cut_path.py``) because the parent
``tests/gateway/test_api_server.py`` used to be blocked at collection by a
missing ``_classify_openwebui_service_request`` symbol; card t_635176e3
removed those tests, so the blocker is gone.

Coverage here:
  * the NON-streaming /v1/chat/completions handler reaches ``_run_agent``
    WITHOUT ``fast_budget_notice_callback`` (the exact 5-of-6 shape the
    reviewer flagged at api_server.py:6336), yet the result built by the real
    ``finalize_turn`` (overflow agent, no callback) carries the footer, which
    the handler serves verbatim as ``message.content``.
  * the full ``finalize_turn`` seam: a budgeted agent whose soft budget was
    exceeded produces ``result["final_response"]`` ending with the options
    block, even with ``_fast_budget_notice_callback=None``.
  * negative gates: no budget / no wrap-up latch -> clean final (no footer);
    interrupted turn -> clean.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("aiohttp")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.fast_budget_text import FAST_BUDGET_USER_NOTICE
from agent.turn_finalizer import finalize_turn
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _fast_overflow_agent(session_id: str = "sess-fast") -> MagicMock:
    """A fast-lane agent that already exceeded its soft 5-min budget, with NO
    live notice callback wired (the 5-of-6 non-streaming shape)."""
    agent = MagicMock()
    agent.run_budget_seconds = 300
    agent._run_budget_started_at = 1000.0
    agent._run_budget_wrapup_injected = True
    agent._fast_budget_notice_callback = None
    agent._fast_budget_user_notice_fired = False
    agent.max_iterations = 90
    agent.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
    agent.quiet_mode = True
    agent.model = "hermes-agent"
    agent.provider = "letovo"
    agent.base_url = ""
    agent.session_id = session_id
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0
    agent.session_cost_status = "unknown"
    agent.session_cost_source = "test"
    agent._tool_guardrail_halt_decision = None
    agent._interrupt_message = None
    agent._response_was_previewed = True
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = []
    agent._persist_session = MagicMock()
    agent._file_mutation_verifier_enabled = lambda: False
    agent._turn_completion_explainer_enabled = lambda: False
    agent._save_trajectory = lambda **_: None
    agent._cleanup_task_resources = lambda **_: None
    agent.clear_interrupt = lambda: None
    return agent


# ---------------------------------------------------------------------------
# NON-streaming /v1/chat/completions (api_server.py:6336) — the reviewer's
# exact 5-of-6 defect shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_completion_no_callback_but_footer_served():
    """The non-streaming handler calls ``_run_agent`` WITHOUT a live notice
    callback, yet the durable overflow footer still reaches the user's
    ``message.content`` (built via the real ``finalize_turn``)."""

    def _run_with_real_finalize(**kwargs):
        # Mirror api_server.py:6336: no fast_budget_notice_callback threaded.
        assert "fast_budget_notice_callback" not in kwargs
        result = finalize_turn(
            _fast_overflow_agent(session_id=kwargs.get("session_id") or "sess-fast"),
            final_response="Ответ: всё посчитано, но дольше 5 минут.",
            api_call_count=8,
            interrupted=False,
            failed=False,
            messages=[
                {"role": "user", "content": "посчитай матрицу"},
                {"role": "assistant", "content": "считаю…"},
            ],
            conversation_history=[],
            effective_task_id="task",
            turn_id="tid",
            user_message="посчитай матрицу",
            original_user_message="посчитай матрицу",
            _should_review_memory=False,
            _turn_exit_reason="text_response(final)",
        )
        return (
            result,
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    adapter = _make_adapter()
    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", side_effect=_run_with_real_finalize) as mock_run:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "посчитай матрицу"}],
                    "stream": False,
                },
            )
        assert resp.status == 200
        body = await resp.json()

    content = body["choices"][0]["message"]["content"]
    assert content.endswith(FAST_BUDGET_USER_NOTICE)
    # The exact 5-of-6 defect shape: no live callback present, yet durable
    # text reached the user (работа не «молчит»).
    assert "fast_budget_notice_callback" not in mock_run.call_args.kwargs


# ---------------------------------------------------------------------------
# finalize_turn seam — the single convergence point of all six _run_agent
# paths (run_conversation -> finalize_turn).
# ---------------------------------------------------------------------------


def test_finalize_appends_footer_on_overflow_without_callback():
    result = finalize_turn(
        _fast_overflow_agent(),
        final_response="Работа завершена с перерасходом времени.",
        api_call_count=9,
        interrupted=False,
        failed=False,
        messages=[
            {"role": "user", "content": "посчитай"},
            {"role": "assistant", "content": "промежуточно"},
        ],
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="посчитай",
        original_user_message="посчитай",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )
    final_text = result.get("final_response")
    assert final_text is not None
    assert final_text.endswith(FAST_BUDGET_USER_NOTICE)


def test_finalize_keeps_fast_answer_clean_within_budget():
    """Budget set but wrap-up NOT latched (still inside soft budget) ->
    no footer (criterion 4: short fast answers stay clean)."""
    agent = _fast_overflow_agent()
    agent._run_budget_wrapup_injected = False
    result = finalize_turn(
        agent,
        final_response="Короткий ответ в пределах 5 минут.",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "вопрос"}, {"role": "assistant", "content": "отв"}],
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="вопрос",
        original_user_message="вопрос",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )
    assert result["final_response"] == "Короткий ответ в пределах 5 минут."


def test_finalize_interrupted_turn_untouched():
    """Interrupted turn -> final response untouched (no footer)."""
    result = finalize_turn(
        _fast_overflow_agent(),
        final_response="Частичный текст",
        api_call_count=5,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "вопрос"}, {"role": "assistant", "content": "частично"}],
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="вопрос",
        original_user_message="вопрос",
        _should_review_memory=False,
        _turn_exit_reason="interrupted",
    )
    assert result["final_response"] == "Частичный текст"
