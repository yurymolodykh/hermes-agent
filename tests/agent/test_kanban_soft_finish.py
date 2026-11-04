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


def test_directive_orders_the_undelivered_verdict_written_before_closing():
    """t_417a182e: a review that spent 156/180 steps re-verifying closed with
    no verdict. The directive must say the missing deliverable IS the next
    action, and must ask for an explicit unchecked list."""
    text = soft.SOFT_FINISH_DIRECTIVE
    assert "NOT on disk yet" in text
    assert "НЕ ПРОВЕРЕНО" in text
    assert "NOT CHECKED" in text
    # The durable per-item channel is named, not implied.
    assert "verdict:" in text
    assert "kanban_comment" in text


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


def test_force_complete_declares_run_output_as_artifacts(monkeypatch, caplog, tmp_path):
    """The forced close hands over artifacts, not just prose (t_24120021).

    t_f1581cbb's soft-finish close named six files in ``summary`` and declared
    no artifacts; the scratch dir was then rmtree'd and the verdict was lost.
    """
    import hermes_cli.kanban_db as kb

    written = tmp_path / "VERDICT_t_test_soft.md"
    written.write_text("verdict", encoding="utf-8")
    captured = {}

    def _fake_binding(conn, task_id, run_id, assignee, session_id):
        return True

    def _fake_complete(conn, task_id, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding", _fake_binding)
    monkeypatch.setattr(kb, "complete_task", _fake_complete)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "9")
    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-3")

    messages = _tool_messages()
    messages.insert(1, {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c9", "type": "function",
            "function": {"name": "write_file",
                         "arguments": f'{{"path": "{str(written).replace(chr(92), "/")}"}}'},
        }],
    })

    with caplog.at_level(logging.WARNING, logger="agent.kanban_soft_finish"):
        assert soft.force_complete(messages=messages) is True

    assert captured["metadata"]["artifacts"] == [str(written)]
    # The summary still names the file for a human reader.
    assert "VERDICT_t_test_soft.md" in captured["summary"]


def test_force_complete_omits_nonexistent_artifacts(monkeypatch):
    """A path the run never actually created must not abort the handoff."""
    import hermes_cli.kanban_db as kb

    captured = {}
    ghost = str(__import__("pathlib").Path.home() / "definitely-not-here.md")

    def _fake_binding(conn, task_id, run_id, assignee, session_id):
        return True

    def _fake_complete(conn, task_id, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding", _fake_binding)
    monkeypatch.setattr(kb, "complete_task", _fake_complete)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "11")
    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-4")

    messages = _tool_messages()
    messages.insert(1, {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c11", "type": "function",
            "function": {"name": "write_file",
                         "arguments": __import__("json").dumps({"path": ghost})},
        }],
    })

    assert soft.force_complete(messages=messages) is True
    assert "artifacts" not in captured["metadata"]
    # …but the operator still sees what the run claimed to write.
    assert "definitely-not-here.md" in captured["summary"]


# ── partial verdict: durable per-item record (t_417a182e) ────────────────────


class _Comment:
    def __init__(self, body, author="09-independent-reviewer", created_at=None):
        self.body = body
        self.author = author
        # ``None`` keeps the "written by this run" default; an explicit older
        # value is how a previous attempt's note is modelled (D2).
        self.created_at = created_at


_RUN_STARTED_AT = 1_700_000_000


def _board_with_comments(monkeypatch, comments, started_at=_RUN_STARTED_AT,
                         boundary_known=True):
    """Patch the board so ``force_complete`` reads ``comments`` from it.

    The attempt boundary (t_6c41691e D2) is carried by
    ``HERMES_KANBAN_RUN_STARTED_AT``: ``kb.connect`` is faked here, so the
    board-row lookup in ``_current_run_started_at`` cannot succeed and the env
    value is the documented fallback. ``boundary_known=False`` models a card
    with no resolvable run start at all.
    """
    import hermes_cli.kanban_db as kb

    captured = {}

    def _fake_binding(conn, task_id, run_id, assignee, session_id):
        return True

    def _fake_complete(conn, task_id, **kwargs):
        captured.update(kwargs)
        captured["task_id"] = task_id
        return True

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding", _fake_binding)
    monkeypatch.setattr(kb, "complete_task", _fake_complete)
    monkeypatch.setattr(kb, "list_comments", lambda conn, tid: list(comments))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_PROFILE", "09-independent-reviewer")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-verdict")
    monkeypatch.delenv("HERMES_KANBAN_RUN_STARTED_AT", raising=False)
    if boundary_known:
        monkeypatch.setenv("HERMES_KANBAN_RUN_STARTED_AT", str(started_at))
    for c in comments:
        if getattr(c, "created_at", None) is None:
            c.created_at = started_at + 60
    return captured


def _run_metadata(captured):
    return captured["metadata"]


def test_force_complete_carries_verdict_notes_into_the_handoff(monkeypatch):
    """The decided items are the deliverable: a budget cut must not lose them.

    t_f1581cbb (run 6008) closed at 156/180 steps with the narration
    «Now re-verifying … before I issue any verdict» — every decision it had
    already made was unrecoverable. Items committed to the board as they were
    decided must surface in the close.
    """
    captured = _board_with_comments(
        monkeypatch,
        [
            _Comment("verdict: ENGINE — паритет JS↔Py сошёлся, 12/12 кейсов"),
            _Comment("verdict: BIBLIO — 3 источника без DOI, помечены"),
            _Comment("обычный комментарий, не вердикт"),
            _Comment("verdict: чужой автор", author="03-software-engineer"),
        ],
    )

    assert soft.force_complete(
        messages=_tool_messages(),
        budget={"steps_used": 156, "steps_max": 180, "fraction": 0.87,
                "time_elapsed": None, "time_limit": None},
    ) is True

    summary = captured["summary"]
    assert "ENGINE — паритет JS↔Py сошёлся, 12/12 кейсов" in summary
    assert "BIBLIO — 3 источника без DOI" in summary
    assert "обычный комментарий" not in summary
    # A foreign profile's note is a different attempt's result.
    assert "чужой автор" not in summary
    assert _run_metadata(captured)["verdict_items_recorded"] == 2
    assert _run_metadata(captured)["verdict_items_inherited"] == 0
    assert _run_metadata(captured)["verdict_partial"] is True
    assert _run_metadata(captured)["verdict_boundary_known"] is True


def test_force_complete_without_verdict_notes_reports_zero(monkeypatch):
    """No per-item record → the board must say so instead of implying a verdict."""
    captured = _board_with_comments(monkeypatch, [])

    assert soft.force_complete(messages=_tool_messages()) is True

    assert captured["metadata"]["verdict_items_recorded"] == 0
    assert captured["metadata"]["verdict_partial"] is True
    # The summary keeps the honest forced-close header.
    assert soft._FORCED_SUMMARY_HEADER in captured["summary"]


def test_verdict_notes_read_failure_does_not_block_the_close(monkeypatch):
    """A board read that throws must not cost the worker its handoff."""
    import hermes_cli.kanban_db as kb

    captured = {}
    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding", lambda *a, **kw: True)
    monkeypatch.setattr(kb, "complete_task",
                        lambda conn, task_id, **kw: captured.update(kw) or True)
    monkeypatch.setattr(kb, "list_comments",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db locked")))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "43")
    monkeypatch.setenv("HERMES_PROFILE", "09-independent-reviewer")

    assert soft.force_complete(messages=_tool_messages()) is True
    assert captured["metadata"]["verdict_items_recorded"] == 0


def test_real_world_partial_verdict_is_harvested(monkeypatch):
    """The real reviewer's own wording must count, not only the `verdict:` form.

    Live evidence from t_39fcfc77 (09-independent-reviewer) after run 6008 was
    killed: the worker DID commit its verdict in parts, but as
    «**Промежуточный вердикт … часть 1/3**» and «ВЕРДИКТ НЕЗАВИСИМОЙ ПРИЁМКИ …
    часть 2/2». A prefix-only harvester would return 0 items for exactly the
    case this channel protects — a fix that passes its own test and misses the
    incident it was written for.
    """
    part1 = (
        "**Промежуточный вердикт (пишу по частям, чтобы не потерять при обрыве "
        "бюджета) — часть 1/3: что подтверждено инструментом**\n\nОбъект: отчёт."
    )
    part2 = "ВЕРДИКТ НЕЗАВИСИМОЙ ПРИЁМКИ — t_39fcfc77 (часть 2/2, финал)\n\nСтатус: revision_required."
    captured = _board_with_comments(
        monkeypatch,
        [
            _Comment(part1),
            _Comment("Обычная заметка по ходу работы без выводов."),
            _Comment(part2),
        ],
    )

    assert soft.force_complete(messages=_tool_messages()) is True

    assert captured["metadata"]["verdict_items_recorded"] == 2
    summary = captured["summary"]
    assert "часть 1/3" in summary
    assert "revision_required" in summary
    assert "Обычная заметка по ходу работы" not in summary


def test_verdict_marker_requires_the_head_of_the_comment():
    """A passing mention of «вердикт» deep in a long note is not a verdict."""
    chatty = "Работаю. " * 60 + "Вердикт будет позже."
    assert soft._is_verdict_note(chatty) is False
    assert soft._is_verdict_note("verdict: ENGINE — сошлось") is True
    assert soft._is_verdict_note("# Вердикт: принято") is True
    assert soft._is_verdict_note("") is False


def test_harvested_whole_verdict_is_capped_in_the_summary(monkeypatch):
    """A whole-verdict note must not bloat the close (full text stays on the board)."""
    long_body = "ВЕРДИКТ: " + ("очень длинный разбор пункта. " * 200)
    captured = _board_with_comments(monkeypatch, [_Comment(long_body)])

    assert soft.force_complete(messages=_tool_messages()) is True

    assert captured["metadata"]["verdict_items_recorded"] == 1
    assert len(captured["summary"]) < 2500
    assert "…" in captured["summary"]


# ── D1: a negated opening is the INVERSE of a verdict (t_6c41691e) ──────────
#
# `_is_verdict_note` is head-anchored on the stem «вердикт»/«verdict», so a
# comment opening «Вердикт не вынесен: …» used to classify as a recorded
# verdict — the close then reported an item-level result for the run whose own
# last word was that it issued none (run 6008's incident shape).


@pytest.mark.parametrize(
    "body",
    [
        "Вердикт не вынесен: бюджет исчерпан на перепроверке.",
        "Вердикт не готов, продолжу в следующем прогоне.",
        "Вердиктовать не берусь, данных мало.",
        "Вердиктов пока нет.",
        "Вердикт пока нет",
        "Вердикта нет.",
        "Вердикт не будет: прогон оборван",
        "Вердикт ещё не вынесен.",
        "Вердикт не сформирован — перепроверяю.",
        "Вердикт не выношу: нет данных",
        "Вердикт по существу не вынесен",
        "Вердиктов не будет в этом прогоне",
        "Промежуточный вердикт не готов: 0 из 6.",
        "no verdict yet",
        "verdict not issued",
        "verdict was not issued: budget ran out",
        "no verdict: budget exhausted",
    ],
)
def test_negated_verdict_opening_is_not_harvested(body):
    """Each negated form must classify as «no verdict» (D1).

    Before the fix every one of these returned True, so a forced close on the
    incident's own wording still recorded items.
    """
    assert soft._is_verdict_note(body) is False


@pytest.mark.parametrize(
    "body",
    [
        # The negation after the colon IS the whole first content segment.
        "Вердикт: не вынесен",
        "Вердикт: пока нет",
        "**Вердикт не вынесен** — бюджет.",
    ],
)
def test_colon_negation_without_content_is_not_harvested(body):
    """Shape B: «Вердикт: не вынесен» is a negation, not an empty verdict."""
    assert soft._is_verdict_note(body) is False


@pytest.mark.parametrize(
    "body",
    [
        "Вердикт: approved — все 6 пунктов подтверждены",
        "**Промежуточный вердикт — часть 1/3**",
        "ВЕРДИКТ НЕЗАВИСИМОЙ ПРИЁМКИ — часть 2/2",
        "Вердикты по пунктам:",
        "### ВЕРДИКТ (approve)",
        "verdict: ENGINE — сошлось, 12/12",
        # A NEGATIVE FINDING is still a decided item: the negation has an
        # object/continuation, so it is content, not a missing verdict.
        "Вердикт: пункт 3 — не готов к выпуску",
        "Вердикт: пункт 2 не вынесен, нужны данные",
        "Вердикт: пока нет данных по пункту 4, остальное принято",
        "Вердикт по пункту 1: нет замечаний",
        "Предварительный вердикт — часть 1/2",
        "Частичный вердикт: 3 из 6 проверено",
        "verdict: pending review of item 4, item 1 passed",
        "Вердикт по пункту 1: принято",
    ],
)
def test_verdict_with_negative_finding_is_still_harvested(body):
    """Rejecting negation must not reject a verdict whose item failed (D1).

    The guard against D1 is only safe if it stays narrow: a real verdict may
    legitimately say an item is not ready.
    """
    assert soft._is_verdict_note(body) is True


def test_negated_opening_does_not_put_items_in_the_close(monkeypatch):
    """End-to-end: the incident's own wording yields zero recorded items (D1).

    This is run 6008's shape — a review run that burned its budget re-verifying
    and said so. Before the fix the close reported ``verdict_items_recorded: 2``
    and quoted both negations as if they were findings.
    """
    captured = _board_with_comments(
        monkeypatch,
        [
            _Comment("Вердикт не вынесен: бюджет исчерпан на перепроверке."),
            _Comment("Вердиктов пока нет — продолжу в следующем прогоне."),
        ],
    )

    assert soft.force_complete(messages=_tool_messages()) is True

    assert captured["metadata"]["verdict_items_recorded"] == 0
    assert captured["metadata"]["verdict_items_inherited"] == 0
    summary = captured["summary"]
    assert "бюджет исчерпан на перепроверке" not in summary
    assert "продолжу в следующем прогоне" not in summary
    assert soft._FORCED_SUMMARY_HEADER in summary


# ── D2: the counter must not inherit a previous attempt's items ─────────────


def test_earlier_attempts_items_are_separated_not_counted(monkeypatch):
    """A re-dispatched card must not close run 2 with run 1's score (D2).

    ``task_comments`` carries no run id, so the boundary is
    ``created_at >= started_at`` of THIS run. Attempt 1 wrote one item; attempt
    2 decided nothing. The close must report 0 recorded / 1 inherited, and the
    summary must label the inherited item as a previous attempt's.
    """
    captured = _board_with_comments(
        monkeypatch,
        [
            _Comment("verdict: старый пункт прошлой попытки",
                     created_at=_RUN_STARTED_AT - 500),
            _Comment("Обычная заметка, не вердикт",
                     created_at=_RUN_STARTED_AT + 10),
        ],
    )

    assert soft.force_complete(messages=_tool_messages()) is True

    meta = captured["metadata"]
    assert meta["verdict_items_recorded"] == 0
    assert meta["verdict_items_inherited"] == 1
    summary = captured["summary"]
    # The earlier work is preserved, but under its own header — never as this
    # run's result.
    assert soft._VERDICT_NOTE_INHERITED_HEADER in summary
    assert "старый пункт прошлой попытки" in summary
    recorded_block = summary.split(soft._VERDICT_NOTE_INHERITED_HEADER)[0]
    assert "старый пункт прошлой попытки" not in recorded_block
    assert soft._VERDICT_NOTE_HEADER not in summary


def test_boundary_is_taken_from_the_run_row_when_env_is_absent(monkeypatch):
    """A step-capped card has no RUN_STARTED_AT env — the board row must serve.

    The dispatcher only exports ``HERMES_KANBAN_RUN_STARTED_AT`` when the card
    carries ``max_runtime_seconds``; otherwise relying on the env alone would
    silently demote every item of a normal run to «inherited».
    """
    import hermes_cli.kanban_db as kb

    started = 1_700_000_000
    captured = {}
    comments = [
        _Comment("verdict: пункт этого прогона", created_at=started + 120),
        _Comment("verdict: пункт прошлой попытки", created_at=started - 120),
    ]

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kb, "has_authenticated_worker_binding", lambda *a, **kw: True)
    monkeypatch.setattr(kb, "complete_task",
                        lambda conn, task_id, **kw: captured.update(kw) or True)
    monkeypatch.setattr(kb, "list_comments", lambda conn, tid: list(comments))
    monkeypatch.setattr(
        kb, "get_run",
        lambda conn, run_id: SimpleNamespace(
            started_at=started, task_id="t_test_soft",
        ),
    )
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")
    monkeypatch.setenv("HERMES_PROFILE", "09-independent-reviewer")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-boundary")
    monkeypatch.delenv("HERMES_KANBAN_RUN_STARTED_AT", raising=False)

    assert soft.force_complete(messages=_tool_messages()) is True

    meta = captured["metadata"]
    assert meta["verdict_items_recorded"] == 1
    assert meta["verdict_items_inherited"] == 1
    assert meta["verdict_boundary_known"] is True
    assert meta["verdict_run_started_at"] == started


def test_unknown_boundary_never_claims_items_for_this_run(monkeypatch):
    """No run row and no env → nothing may be counted as this run's (D2).

    Reporting such items as recorded is exactly the defect: the close would
    assert a result the run may never have produced.
    """
    captured = _board_with_comments(
        monkeypatch,
        [_Comment("verdict: нечто неопределённого происхождения")],
        boundary_known=False,
    )

    assert soft.force_complete(messages=_tool_messages()) is True

    meta = captured["metadata"]
    assert meta["verdict_items_recorded"] == 0
    assert meta["verdict_items_inherited"] == 1
    assert meta["verdict_boundary_known"] is False
    assert "verdict_run_started_at" not in meta
    # And the close says so rather than implying the run decided nothing.
    assert soft._VERDICT_NOTE_INHERITED_HEADER in captured["summary"]


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
