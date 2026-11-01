"""Progress handoff across the automatic budget split.

Regression cover for the turn_finalizer → budget_split seam: of 542 automatic
splits in one month, 538 handed their children an EMPTY ``progress_note``
because ``_record_kanban_budget_exhausted`` never passed the argument. Children
then restarted the work from scratch and exhausted their own budget again
(t_2d037564: 16 cards in 4.5h).

Four contracts are pinned here:

1. the finalizer collects a deterministic note from the run's own transcript /
   tool-call history and passes it through to ``run_budget_split``;
2. ``_fallback_children`` puts the note into the body of BOTH children;
3. an empty note is recorded honestly as ``progress_note_source="none"`` (after
   the comment / workspace recovery sources are tried);
4. a note that reports the work as finished resolves to ONE "verify and close"
   card, not 2-4.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import turn_finalizer
from hermes_cli import kanban_budget_split as bsplit
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import profiles as _profiles

    monkeypatch.setattr(_profiles, "profile_exists", lambda _n: True)
    kb.init_db()
    return home


@pytest.fixture
def no_llm(monkeypatch):
    """Aux LLM unavailable → deterministic fallback path only."""
    import sys

    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)
    monkeypatch.setattr(bsplit, "_load_config", lambda: {})


@pytest.fixture
def no_plugins(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])


class _BudgetAgent:
    """Minimal agent double for the iteration-limit exit path."""

    def __init__(self, max_iterations=60):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=0, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-progress"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self._completion_explainer = False
        self.max_iterations_calls = 0

    def _handle_max_iterations(self, messages, api_call_count):
        self.max_iterations_calls += 1
        return "summary from extra call"

    def _emit_status(self, *_a, **_kw):
        pass

    def _safe_print(self, *_a, **_kw):
        pass

    def _save_trajectory(self, *_a, **_kw):
        pass

    def _cleanup_task_resources(self, *_a, **_kw):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        pass

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _format_turn_completion_explanation(self, _r, _c=None):
        return ""

    def _drain_pending_steer(self):
        return None

    def _sync_external_memory_for_turn(self, **_kw):
        pass

    def clear_interrupt(self):
        pass


def _finalize_budget_exit(agent, messages, final_response=None, exit_reason="unknown"):
    return turn_finalizer.finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=agent.max_iterations,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
    )


# ---------------------------------------------------------------------------
# Contract 1 — turn_finalizer passes a non-empty progress_note
# ---------------------------------------------------------------------------

def test_finalizer_passes_collected_progress_note_to_budget_split(
    monkeypatch, no_plugins,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_progress")
    captured = {}

    def _fake_split(task_id, *, source, progress_note="", expected_run_id=None):
        captured["task_id"] = task_id
        captured["source"] = source
        captured["progress_note"] = progress_note
        return bsplit.BudgetSplitResult(True, child_ids=["c1", "c2"], via="fallback")

    monkeypatch.setattr(
        "hermes_cli.kanban_budget_split.run_budget_split", _fake_split,
    )
    agent = _BudgetAgent()
    messages = [
        {"role": "user", "content": "build the report"},
        {
            "role": "assistant",
            "content": "Собрал каркас отчёта, осталось наполнить раздел 3.",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path": "reports/draft.md", "content": "x"}',
                },
            }],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
    ]

    _finalize_budget_exit(agent, messages)

    note = captured["progress_note"]
    assert note, "progress_note must be non-empty when the run left a trace"
    assert captured["task_id"] == "t_progress"
    assert captured["source"] == "iteration_budget"
    # What the worker said and what it wrote both survive into the handoff.
    assert "Собрал каркас отчёта" in note
    assert "reports/draft.md" in note
    # The explicit do-not-redo marker and the stop counter are present.
    assert turn_finalizer.PROGRESS_DONE_HEADER in note
    assert "60/60" in note


def test_finalizer_writes_empty_note_when_run_left_no_trace(
    monkeypatch, no_plugins,
):
    """No assistant text, no file mutations, no workspace → honest empty note.

    Exercised through the bounded-fallback branch (``elif budget_exhausted``):
    the iteration-limit summary path always yields *some* text (the model's own
    summary), which is a legitimate trace. Here the turn died without an answer
    at all, so the note must stay honestly empty.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_traceless")
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    captured = {}

    def _fake_split(task_id, *, source, progress_note="", expected_run_id=None):
        captured["progress_note"] = progress_note
        return bsplit.BudgetSplitResult(True, child_ids=["c1", "c2"], via="fallback")

    monkeypatch.setattr(
        "hermes_cli.kanban_budget_split.run_budget_split", _fake_split,
    )
    agent = _BudgetAgent()
    _finalize_budget_exit(
        agent,
        [{"role": "user", "content": "task"}],
        final_response=None,
        exit_reason="provider_failure",
    )

    assert captured["progress_note"] == ""


def test_collect_progress_note_uses_final_response_fallback():
    """When the transcript has no assistant row, the delivered response is used."""
    note = turn_finalizer._collect_budget_progress_note(
        messages=[{"role": "user", "content": "task"}],
        final_response="Готово: файл out.md создан.",
        api_call_count=12,
        max_iterations=12,
        exit_reason="max_iterations_reached(12/12)",
    )
    assert "Готово: файл out.md создан." in note
    assert turn_finalizer.PROGRESS_DONE_HEADER in note


# ---------------------------------------------------------------------------
# Contract 2 — the note reaches BOTH fallback children
# ---------------------------------------------------------------------------

def test_fallback_children_put_progress_block_into_both_bodies():
    progress = "Сделал каркас, осталось наполнить раздел 3. Файлы: reports/draft.md"
    children = bsplit._fallback_children(
        "t_parent", "build report", "acceptance: report renders", progress,
    )
    assert len(children) == 2
    for child in children:
        assert "Progress note from the previous worker" in child["body"]
        assert "reports/draft.md" in child["body"]


def test_run_budget_split_child_bodies_carry_the_note(kanban_home, no_llm):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="big work", body="acceptance: it works",
            assignee="03-software-engineer",
        )
        assert kb.claim_task(conn, tid) is not None

    res = bsplit.run_budget_split(
        tid,
        source="iteration_budget",
        progress_note="Сделал половину: src/a.py готов, осталось src/b.py",
        cfg={},
    )
    assert res.ok is True
    assert res.child_ids and len(res.child_ids) == 2

    with kb.connect() as conn:
        bodies = [kb.get_task(conn, cid).body for cid in res.child_ids]
    for body in bodies:
        assert "Progress note from the previous worker" in body
        assert "src/a.py готов" in body


# ---------------------------------------------------------------------------
# Contract 3 — empty progress is recorded honestly, after recovery attempts
# ---------------------------------------------------------------------------

def test_empty_progress_records_source_none(kanban_home, no_llm, monkeypatch):
    monkeypatch.setattr(bsplit, "_recover_from_workspace", lambda _p: "")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bare work", body="body")
        assert kb.claim_task(conn, tid) is not None

    res = bsplit.run_budget_split(tid, source="iteration_budget", progress_note="", cfg={})
    assert res.ok is True

    with kb.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["progress_note_source"] == "none"
    assert payload["progress_note_len"] == 0
    assert payload["progress_note"] == ""


def test_recovery_from_parent_comment_is_recorded(kanban_home, no_llm):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="work with notes", body="body")
        assert kb.claim_task(conn, tid) is not None
        kb.add_comment(
            conn, tid, "03-software-engineer",
            "Сделал шаг 1 и 2: src/parse.py готов, тесты проходят. "
            "Осталось подключить CLI.",
        )

    res = bsplit.run_budget_split(tid, source="iteration_budget", progress_note="", cfg={})
    assert res.ok is True

    with kb.connect() as conn:
        payload = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"][0].payload
        bodies = [kb.get_task(conn, cid).body for cid in res.child_ids]
    assert payload["progress_note_source"] == "parent_comment"
    assert payload["progress_note_len"] > 0
    assert "src/parse.py готов" in payload["progress_note"]
    assert any("src/parse.py готов" in b for b in bodies)


def test_recovery_from_workspace_is_recorded(kanban_home, no_llm, monkeypatch):
    monkeypatch.setattr(
        bsplit, "_recover_from_workspace",
        lambda _p: "Рабочий каталог карточки содержит изменённые файлы:\n- M src/x.py",
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ws work", body="body")
        assert kb.claim_task(conn, tid) is not None

    res = bsplit.run_budget_split(tid, source="iteration_budget", cfg={})
    assert res.ok is True
    with kb.connect() as conn:
        payload = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"][0].payload
    assert payload["progress_note_source"] == "workspace"
    assert "- M src/x.py" in payload["progress_note"]


def test_caller_supplied_note_is_recorded_as_run_buffer(kanban_home, no_llm):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="buffered", body="body")
        assert kb.claim_task(conn, tid) is not None

    res = bsplit.run_budget_split(
        tid, source="iteration_budget",
        progress_note="ВЫПОЛНЕНО РАНЕЕ: шаг 1/60. Файлы: src/a.py", cfg={},
    )
    assert res.ok is True
    with kb.connect() as conn:
        payload = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"][0].payload
    assert payload["progress_note_source"] == "run_buffer"
    assert payload["progress_note_len"] == len("ВЫПОЛНЕНО РАНЕЕ: шаг 1/60. Файлы: src/a.py")


# ---------------------------------------------------------------------------
# Contract 4 — finished work resolves to ONE "verify and close" card
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "progress",
    [
        "Работа выполнена полностью, осталось только закрыть карточку.",
        "The work is already done — only the card is still open.",
        "Артефакт собран и проверен, осталось закрыть.",
        "already completed; nothing left to implement",
    ],
)
def test_close_only_progress_creates_single_card(progress):
    children = bsplit._fallback_children("t_p", "report", "body", progress)
    assert len(children) == 1
    assert "Verify and close" in children[0]["title"]
    body = children[0]["body"]
    assert "ALREADY DONE" in body
    assert "DO NOT" in body and "redo" in body
    # The note itself is still attached for the closing worker.
    assert "Progress note from the previous worker" in body


def test_open_work_progress_still_splits_into_two():
    """The «ВЫПОЛНЕНО РАНЕЕ» header alone must NOT collapse a real split."""
    progress = (
        "ВЫПОЛНЕНО РАНЕЕ (не переделывай): перечисленное ниже уже сделано в этом прогоне.\n\n"
        "Остановка по бюджету: шаг 40/40 (причина выхода: max_iterations_reached(40/40)).\n\n"
        "Файлы, созданные/изменённые в этом прогоне (tool-call history):\n- src/a.py"
    )
    assert bsplit._progress_reports_completion(progress) is False
    children = bsplit._fallback_children("t_p", "report", "body", progress)
    assert len(children) == 2


# Negative controls for the close-only detector. Reviewer finding on 8c4ecd0589:
# the bare substring pair ("остал", "закры") fired on «осталось от-КРЫ-ть раздел 3»
# and on «осталось закрыть <работу>», collapsing UNFINISHED work into one
# "verify and close" card — the remainder then had no card at all.
@pytest.mark.parametrize(
    "progress",
    [
        # «закры» hidden inside «открыть»
        "Реализовал парсер, но осталось открыть вопрос с лицензией.",
        "Раздел 1 и 2 готовы, осталось открыть раздел 3.",
        # «осталось закрыть <работу>» — closing a WORK ITEM, not the card
        "Раздел 1 и 2 готовы, осталось закрыть раздел 3.",
        "Сделал парсер, осталось закрыть тестами функцию parse().",
        "Осталось закрыть вопрос с лицензией и дописать раздел 4.",
        "Реализовал API, осталось закрыть обработку ошибок в клиенте.",
        "Схема БД готова, осталось закрыть миграции на прод.",
        # explicit negation beats a positive marker in the same text
        "Работа выполнена не полностью: раздел 3 пустой, надо дописать.",
        "Работа завершена не полностью, осталось закрыть раздел 3.",
        # «закрыть задачи/смену» — work items, not the card
        "Не всё сделано: осталось закрыть задачи 5 и 6.",
        "Ещё не готово, осталось закрыть смену и сессию.",
    ],
)
def test_mid_work_progress_is_not_close_only(progress):
    assert bsplit._progress_reports_completion(progress) is False
    children = bsplit._fallback_children("t_p", "report", "body", progress)
    assert len(children) == 2
    for child in children:
        assert "Progress note from the previous worker" in child["body"]


@pytest.mark.parametrize(
    "progress",
    [
        "Осталось закрыть карточку.",
        "Отчёт готов, осталось только закрыть.",
        "Работа уже выполнена, осталось закрыть задачу.",
        "Всё сделано, осталось закрыть тикет.",
        # the shape the turn_finalizer actually emits in production: the
        # «ВЫПОЛНЕНО РАНЕЕ» header + the model's own closing sentence
        "ВЫПОЛНЕНО РАНЕЕ (не переделывай): перечисленное ниже уже сделано.\n\n"
        "Что воркер сообщил при остановке (итог прогона):\n"
        "Работа выполнена, артефакт готов, осталось закрыть карточку.",
    ],
)
def test_bare_close_remainder_is_close_only(progress):
    """The owner's own shape — «выполнено, осталось закрыть» — must still fire."""
    assert bsplit._progress_reports_completion(progress) is True
    assert len(bsplit._fallback_children("t_p", "report", "body", progress)) == 1


def test_run_budget_split_close_only_creates_one_card(kanban_home, no_llm):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="finished report", body="acceptance: report exists",
            assignee="03-software-engineer",
        )
        assert kb.claim_task(conn, tid) is not None

    res = bsplit.run_budget_split(
        tid, source="iteration_budget",
        progress_note="Отчёт полностью готов, осталось только закрыть карточку.",
        cfg={},
    )
    assert res.ok is True
    assert res.via == "close_only"
    assert len(res.child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, res.child_ids[0])
        root = kb.get_task(conn, tid)
    assert child.status == "ready"
    assert "Verify and close" in child.title
    assert root.status == "todo"


# ---------------------------------------------------------------------------
# Contract 4 boundary — bare completion phrasing is NOT close-only
# ---------------------------------------------------------------------------
# Review of 15743759b0 (blocking): the detector accepted bare substrings
# ("работа выполнена", "всё сделано", "already completed"). Those are exactly
# how a worker OPENS a partial-credit summary, so 11 of 22 realistic
# mid-work tails collapsed to one "[close only] Verify and close" card whose
# body said both «осталось доделать раздел 3» and «ALREADY DONE — DO NOT redo».
# The remainder then had no card at all — worse than the empty note the whole
# task exists to fix.
#
# The corpus below is deliberately NOT hand-picked to the regex under test: it
# is a spread of partial-credit qualifiers (частично / на N% / в основном /
# кроме / наполовину / не до конца) and of "closing a work item" phrasing.
# Both the bare tail and the note shape that `_collect_budget_progress_note`
# actually emits in production are checked, because only the latter reaches
# the splitter in production and only it contains the model's summary section.

_PARTIAL_CREDIT_TAILS = [
    "Сделал каркас и API. Раздел 3 не начат.",
    "Готовы разделы 1 и 2. Осталось написать разделы 3 и 4.",
    "Работа выполнена частично: парсер есть, валидация отсутствует.",
    "Работа выполнена на 70%, осталось закрыть интеграцию с очередью.",
    "Работа выполнена в основном, но раздел 4 не начат.",
    "Работа выполнена наполовину.",
    "Работа выполнена лишь частично; остальное в следующем прогоне.",
    "Работа выполнена, кроме раздела 3 — его надо дописать.",
    "Работа выполнена не до конца: миграции не написаны.",
    "Всё сделано, кроме тестов — их надо дописать и прогнать.",
    "Всё сделано для раздела 1, раздел 2 в работе.",
    "Выполнено: разделы 1-2. Осталось: раздел 3.",
    "Схема готова. Тесты не написаны, сборка не проверялась.",
    "Реализовал 3 из 5 эндпоинтов, остальные в работе.",
    "Черновик отчёта готов, данных за Q3 нет.",
    "Начал рефакторинг, половина модулей переписана.",
    "Интеграция работает на моках, реальный API не подключён.",
    "Only the card is still open for part 1; part 2 is not started.",
    "Работа выполнена, но сборка падает — надо чинить.",
    "Файл отчёта создан, но данные за квартал ещё не собраны.",
    "Готово: каркас, роутинг. Не готово: аутентификация, тесты.",
    "Сделал первую половину, вторая требует решения владельца.",
    # --- reviewer r3 blocking class (a): a colon/semicolon after the closer
    # INTRODUCES the enumeration of what remains. `:`/`;` used to be accepted as
    # a clause end, so «раздел 3» got no card while the only child claimed
    # «ALREADY DONE — DO NOT redo».
    "Осталось закрыть: раздел 3.",
    "Работа выполнена. Осталось закрыть: раздел 3, тесты.",
    "Осталось закрыть: миграции, раздел 3 и тесты.",
    "Осталось закрыть: 1) раздел 3; 2) тесты.",
    "Осталось закрыть карточку и раздел 3.",
    "Осталось закрыть тикет, но сначала надо починить сборку.",
    "Осталось закрыть карточку — остались тесты.",
    # --- reviewer r3 blocking class (b): «закрыть задачу <работа>» closes a WORK
    # ITEM, not the board card. These split correctly on 15743759b0 and were
    # REGRESSIONS on c2338e94d1.
    "Осталось закрыть задачу миграции на прод.",
    "Парсер готов; осталось закрыть задачу по обработке ошибок в клиенте.",
    "Осталось закрыть задачу №5 по разделу 3.",
    # --- remainder re-opened in a LATER clause while the closer ends one
    "Каркас готов. Осталось закрыть.\nОсталось: раздел 3 и тесты.",
    "Работа выполнена, осталось закрыть карточку. Но осталось: раздел 3.",
    "Осталось: раздел 3. Осталось закрыть карточку.",
]

_GENUINE_CLOSE_ONLY_TAILS = [
    "Работа выполнена, артефакт собран, осталось закрыть карточку.",
    "Всё сделано и протестировано, осталось только закрыть тикет.",
    "Отчёт готов, тесты зелёные, осталось закрыть карточку.",
    "Работа уже выполнена полностью, осталось закрыть.",
    "Артефакт собран и проверен, осталось закрыть.",
]


def _production_note(tail: str) -> str:
    """The exact shape ``_collect_budget_progress_note`` emits in production."""
    return turn_finalizer._collect_budget_progress_note(
        messages=[
            {"role": "user", "content": "собери отчёт"},
            {
                "role": "assistant",
                "content": "Начал работу.",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "reports/draft.md", "content": "x"}',
                    },
                }],
            },
            {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
        ],
        final_response=tail,
        api_call_count=40,
        max_iterations=40,
        exit_reason="max_iterations_reached(40/40)",
    )


@pytest.mark.parametrize("tail", _PARTIAL_CREDIT_TAILS)
def test_partial_credit_tail_never_collapses_to_close_only(tail):
    """A partial-credit summary must keep a real split in BOTH note shapes."""
    assert bsplit._progress_reports_completion(tail) is False, tail
    note = _production_note(tail)
    assert note, "the production note must carry the summary"
    assert bsplit._progress_reports_completion(note) is False, note
    children = bsplit._fallback_children("t_p", "report", "body", note)
    assert len(children) == 2, f"{tail} collapsed to {len(children)} card(s)"
    for child in children:
        assert "ALREADY DONE" not in child["body"]
        assert "Progress note from the previous worker" in child["body"]


@pytest.mark.parametrize("tail", _GENUINE_CLOSE_ONLY_TAILS)
def test_genuine_close_only_tail_still_resolves_to_one_card(tail):
    """The owner's case must keep working, in the production note shape too."""
    assert bsplit._progress_reports_completion(tail) is True, tail
    note = _production_note(tail)
    assert bsplit._progress_reports_completion(note) is True, note
    children = bsplit._fallback_children("t_p", "report", "body", note)
    assert len(children) == 1
    assert "Verify and close" in children[0]["title"]
    assert "ALREADY DONE" in children[0]["body"]


def test_close_only_needs_a_closing_remainder_not_just_a_completion_claim():
    """Completion phrasing alone is not a closing remainder.

    Each of these says the work is done in one clause and names REMAINING WORK
    in another; none may resolve to the single card.
    """
    for tail in [
        "Работа выполнена. Осталось написать раздел 3.",
        "Всё сделано: осталось проверить сборку на чистой машине.",
        "already completed the parser; the validator is still missing",
        "Осталось закрыть этот вопрос с заказчиком после ревью.",
        "Осталось закрыть миграции на прод.",
        "Работа выполнена на 100%, но раздел 4 не начат.",
    ]:
        assert bsplit._progress_reports_completion(tail) is False, tail
        assert len(bsplit._fallback_children("t_p", "t", "b", tail)) == 2, tail


def test_conflicting_note_resolves_to_the_safe_side():
    """The seam embeds BOTH the post-budget summary and the transcript tail.

    A worker that finished and only needs the card closed may still have a
    transcript tail that names earlier remaining work. The combined note then
    carries both signals; the class boundary must resolve to SPLIT (a false
    negative costs one redundant split, a false positive drops real work).
    """
    note = turn_finalizer._collect_budget_progress_note(
        messages=[
            {"role": "user", "content": "собери отчёт"},
            {"role": "assistant",
             "content": "Готовы разделы 1 и 2, осталось написать разделы 3 и 4."},
        ],
        final_response="Работа выполнена, артефакт готов, осталось закрыть карточку.",
        api_call_count=3,
        max_iterations=3,
        exit_reason="budget_exhausted",
    )
    assert "осталось закрыть карточку" in note
    assert "осталось написать разделы 3 и 4" in note
    assert bsplit._progress_reports_completion(note) is False, note
    children = bsplit._fallback_children("t_p", "report", "body", note)
    assert len(children) == 2
    for child in children:
        assert "ALREADY DONE" not in child["body"]


# ---------------------------------------------------------------------------
# Contract 4 boundary — r4 (23b6af0536): the closer must END its clause, and
# nothing after it may still name open work
# ---------------------------------------------------------------------------
# Review r4 of 23b6af0536 found the r3 fix over-corrected: `\n` had been added
# to the closer's clause-end character class, so a worker who wrote
#
#     Осталось закрыть
#     - раздел 3
#     - тесты
#
# — a bullet list of what REMAINS, the documented mid-work handoff shape —
# collapsed to one «[close only] … ALREADY DONE — DO NOT redo» card and
# «раздел 3» / «тесты» got no card at all. Two of the reviewer's 14 mid-work
# tails regressed from SPLIT (correct on r1/r2) to COLLAPSE.
#
# The same review found the structural hole behind it: the collector appends
# the run's last narration AFTER the post-budget summary, so a closer whose own
# clause "ends" cleanly can still be followed by a section naming open work
# («Осталось закрыть карточку.» + «Код готов, тесты не писал.»).
#
# Both are pinned below, together with the reviewer's POSITIVE control (a
# closer followed by a completion recap must still be ONE card, not a split).

_R4_MIDWORK_TAILS = [
    # the two regressions, verbatim from the review
    "Сделал разделы 1-2. Осталось закрыть\n- раздел 3\n- тесты",
    "Осталось закрыть\nраздел 3 и тесты",
    # closer whose own clause ends, remainder on the next line as a list
    "Осталось закрыть.\n- раздел 3\n- тесты",
    "Каркас готов. Осталось закрыть карточку.\nДальше: раздел 3.",
    "Осталось закрыть карточку.\nНо сначала раздел 3.",
    # closer then open work in the same paragraph
    "Осталось закрыть карточку. Раздел 3 остаётся недоделанным.",
    "Осталось закрыть карточку. Раздел 3 не трогал, тестов нет.",
    "Осталось закрыть карточку. Завтра — разделы 3 и 4.",
    "Осталось закрыть тикет. Впрочем, раздел 3 ещё висит.",
    "Работа по разделам 1-2 принята. Осталось закрыть карточку. Раздел 3 в очереди.",
]

_R4_COMPLETION_TAILS = [
    # the reviewer's s2 control: closer + completion recap = ONE card
    "Собрал отчёт, прогнал тесты, всё зелёное.",
    "Готово: разделы 1-4, тесты зелёные.",
    "Начал работу.",
]


@pytest.mark.parametrize("tail", _R4_MIDWORK_TAILS)
def test_closer_does_not_end_at_a_line_break_or_swallow_open_work(tail):
    """A remainder written after the closer keeps its own card."""
    assert bsplit._progress_reports_completion(tail) is False, tail
    note = _production_note(tail)
    assert note, "the production note must carry the summary"
    assert bsplit._progress_reports_completion(note) is False, note
    assert len(bsplit._fallback_children("t_p", "report", "body", note)) == 2


@pytest.mark.parametrize("tail", _R4_COMPLETION_TAILS)
def test_closer_followed_by_completion_recap_stays_one_card(tail):
    """The tail is only substantive when it still names open work."""
    note = turn_finalizer._collect_budget_progress_note(
        messages=[{"role": "assistant", "content": tail}],
        final_response="Осталось закрыть карточку.",
        api_call_count=3,
        max_iterations=3,
        exit_reason="max_iterations_reached(3/3)",
    )
    # Both the summary and the earlier narration are in the note…
    assert "Осталось закрыть карточку" in note
    assert tail in note
    # …and the pair still resolves to the single closing card.
    assert bsplit._progress_reports_completion(note) is True, note
    children = bsplit._fallback_children("t_p", "report", "body", note)
    assert len(children) == 1
    assert "Verify and close" in children[0]["title"]


def test_tail_veto_only_fires_on_open_work_not_on_the_collector_labels():
    """The collector's own label lines and file inventory are not a tail."""
    note = turn_finalizer._collect_budget_progress_note(
        messages=[
            {"role": "user", "content": "собери отчёт"},
            {
                "role": "assistant",
                "content": "Собрал отчёт, прогнал тесты, всё зелёное.",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "reports/final.md", "content": "x"}',
                    },
                }],
            },
            {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
        ],
        final_response="Работа выполнена, осталось закрыть карточку.",
        api_call_count=3, max_iterations=3,
        exit_reason="max_iterations_reached(3/3)",
    )
    # The file inventory block and the labels ride along…
    assert "reports/final.md" in note
    assert "ВЫПОЛНЕНО РАНЕЕ" in note
    # …and must not be mistaken for a remainder.
    assert bsplit._progress_reports_completion(note) is True
    assert len(bsplit._fallback_children("t_p", "report", "body", note)) == 1


def test_remainder_stated_before_the_closer_also_blocks_collapse():
    """The remainder's POSITION is irrelevant — only that it is still open.

    Reviewer r4 probe `detail-before-closer`: a worker that opens with an
    unfinished section and closes by naming the card. Cutting only the text
    after the closer left this collapsed, even though «раздел 3» was still
    explicitly unfinished.
    """
    for tail in [
        "Раздел 3 остаётся недоделанным. Осталось закрыть карточку.",
        "Раздел 3 не начат. Осталось закрыть карточку.",
        "Тесты не написаны; осталось закрыть карточку.",
    ]:
        assert bsplit._progress_reports_completion(tail) is False, tail
        note = _production_note(tail)
        assert bsplit._progress_reports_completion(note) is False, note
        assert len(bsplit._fallback_children("t_p", "report", "body", note)) == 2


def test_closer_wins_over_completion_detail_on_the_same_line():
    """A finished statement plus the closer is still one card.

    The residual check must not turn a plain completion recap into a veto: no
    remainder marker and no list item means nothing is left open.
    """
    for tail in [
        "Сделал разделы 1-4, собрал отчёт, прогнал тесты; осталось закрыть карточку.",
        "Готово: разделы 1-4 и тесты. Осталось закрыть карточку.",
    ]:
        assert bsplit._progress_reports_completion(tail) is True, tail
        note = _production_note(tail)
        assert bsplit._progress_reports_completion(note) is True, note
        assert len(bsplit._fallback_children("t_p", "report", "body", note)) == 1


# ---------------------------------------------------------------------------
# Contract 4 boundary — r5 (ec0b6d3e74): SHORT-form passives, absence and
# requirement predicates also name the remainder
# ---------------------------------------------------------------------------
# Review r5 of ec0b6d3e74 found the tail veto blind to the way Russian reports
# actually state what is left: short-form passives («раздел 3 не тронут»,
# «тестирование не тронуто», «разделы 3 и 4 не тронуты», «раздел 4 не покрыт»),
# absence predicates («тесты отсутствуют»), requirement predicates («раздел 3
# требует доработки», «для сборки не хватает раздела 3»), «без <чего>»
# («остался без покрытия»), «не проводил(ся/ась)» и «не всё» after a participle
# («покрыто не всё», «завершено не всё»).
#
# All of them collapsed to ONE «[close only] … ALREADY DONE — DO NOT redo» card
# and the named remainder got no card at all — the exact class of failure this
# module exists to remove (t_2d037564). The reviewer's seam probe failed 4/5 on
# the real finalize_turn → run_budget_split → SQLite path.
#
# The control block pins the opposite polarity: a genuine close-only note must
# STILL resolve to one card, so the fix cannot be "never collapse".

_R5_PASSIVE_MIDWORK_TAILS = [
    # short-form passive participles: -т / -то / -ты
    "Раздел 3 не тронут.",
    "Тестирование не тронуто.",
    "Разделы 3 и 4 не тронуты.",
    "Раздел 4 не покрыт тестами.",
    # «не всё» after the participle
    "Тестами покрыто не всё.",
    "Завершено не всё.",
    # absence predicates
    "Тесты отсутствуют.",
    "Раздел 3 отсутствует.",
    # requirement / insufficiency predicates
    "Раздел 3 требует доработки.",
    "Для сборки не хватает раздела 3.",
    # «без <чего>»
    "Раздел 3 остался без покрытия.",
    # «не проводил(ся/ась)»
    "Проверка не проводилась.",
    # plural state predicate
    "Два замечания висят.",
]

# The same 13 shapes as they arrive from production: appended to a completion
# statement and a closer, run through the REAL collector.
_R5_DONE = "Собрал разделы 1-2 в reports/s12.md."
_R5_CLOSER = "Осталось закрыть карточку."

_R5_CLOSE_ONLY_CONTROLS = [
    # owner shape, both note shapes
    "Артефакт собран, тесты зелёные. Осталось закрыть карточку.",
    "Осталось только закрыть карточку.",
    "Всё готово, осталось закрыть тикет.",
    "Сделано, переделывать не нужно: артефакт готов.",
    "Nothing left to implement.",
    "Only the card remains.",
]


@pytest.mark.parametrize("tail", _R5_PASSIVE_MIDWORK_TAILS)
def test_short_passive_tail_keeps_a_card_for_the_remainder(tail):
    """A remainder stated passively must still get its own card(s).

    Both polarities of the note shape are checked — the bare tail and the
    production note built by :func:`_collect_budget_progress_note` — because the
    collector appends its own «ВЫПОЛНЕНО РАНЕЕ» header and file inventory, and
    the veto must fire on the worker's sentence, not be starved by them.
    """
    assert bsplit._progress_reports_completion(tail) is False, tail
    note = _production_note(f"{_R5_DONE} {_R5_CLOSER} {tail}")
    assert tail in note, "the production note must carry the worker's remainder"
    assert bsplit._progress_reports_completion(note) is False, note
    children = bsplit._fallback_children("t_p", "report", "body", note)
    assert len(children) == 2, f"{tail} collapsed to {len(children)} card(s)"
    for child in children:
        assert "ALREADY DONE" not in child["body"], tail
        assert "Progress note from the previous worker" in child["body"], tail


@pytest.mark.parametrize("tail", _R5_CLOSE_ONLY_CONTROLS)
def test_r5_close_only_controls_still_resolve_to_one_card(tail):
    """The fix is additive: genuine close-only notes must not start splitting."""
    assert bsplit._progress_reports_completion(tail) is True, tail
    note = _production_note(tail)
    assert bsplit._progress_reports_completion(note) is True, note
    children = bsplit._fallback_children("t_p", "report", "body", note)
    assert len(children) == 1, f"{tail} split into {len(children)} cards"
    assert "ALREADY DONE" in children[0]["body"]


def test_r5_passive_shapes_through_the_real_seam_produce_children(
    kanban_home, no_llm, monkeypatch
):
    """End-to-end on one representative shape: DB gets 2 children, no ALREADY DONE.

    The parametrised unit test above pins the classifier; this one pins the
    seam (``finalize_turn`` → ``run_budget_split`` → SQLite) for the class, so a
    future change cannot keep the classifier green while the card still
    collapses.
    """
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(kanban_home))
    from hermes_cli import profiles as _profiles

    monkeypatch.setattr(_profiles, "profile_exists", lambda _n: True)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Собрать отчёт по разделам 1-4",
            body="acceptance: 4 sections",
            assignee="03-software-engineer",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        assert kb.claim_task(conn, tid) is not None
        run_id = kb._current_run_id(conn, tid)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    agent = _BudgetAgent(max_iterations=3)
    agent._handle_max_iterations = (
        lambda messages, api_call_count: "Собрал разделы 1-2. "
        "Осталось закрыть карточку. Раздел 3 не тронут."
    )
    _finalize_budget_exit(agent, [{"role": "user", "content": "собери отчёт"}])

    with kb.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"]
        assert len(events) == 1, events
        payload = events[0].payload or {}
        child_ids = payload.get("child_ids") or []
        bodies = [(kb.get_task(conn, cid).body or "") for cid in child_ids]

    assert len(child_ids) >= 2, payload
    assert payload.get("progress_note_source") == "run_buffer", payload
    assert payload.get("progress_note_len", 0) > 0, payload
    for body in bodies:
        assert "ALREADY DONE" not in body
        assert "Progress note from the previous worker" in body
