"""Criterion 5 — end-to-end budget split with a REAL DB and a real seam.

Not a unit test of one function: this drives ``agent.turn_finalizer.finalize_turn``
through an iteration-budget exit against a live (tmp) kanban database, with the
real ``run_budget_split`` (aux LLM disabled → deterministic fallback), and then
reads the actual ``budget_split`` event row and the actual child bodies back out
of SQLite.

Pins the whole chain that was broken in production:

    finalize_turn → _record_kanban_budget_exhausted
                 → _collect_budget_progress_note (run transcript + tool calls)
                 → run_budget_split(progress_note=...)
                 → split_task_on_budget (event payload: note, len, source)
                 → child bodies carrying «Progress note from the previous worker»
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import turn_finalizer
from hermes_cli import kanban_budget_split as bsplit
from hermes_cli import kanban_db as kb


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
    """Aux LLM unavailable → the deterministic split path, no network."""
    import sys

    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)
    monkeypatch.setattr(bsplit, "_load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])


class _BudgetAgent:
    """Agent double for the iteration-limit exit path (max_iterations=3)."""

    def __init__(self, max_iterations=3):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=0, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-integration"
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

    def _handle_max_iterations(self, messages, api_call_count):
        return "Собрал каркас отчёта: reports/draft.md, раздел 3 не начат."

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


def test_iteration_budget_exit_splits_with_real_progress_note(
    kanban_home, no_llm, monkeypatch, tmp_path,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Собрать отчёт по разделу 3",
            body="acceptance: reports/draft.md contains section 3",
            assignee="03-software-engineer",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = kb._current_run_id(conn, tid)
    assert run_id, "claim must open a run for the CAS expected_run_id guard"

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    messages = [
        {"role": "user", "content": "собери отчёт"},
        {
            "role": "assistant",
            "content": "Каркас готов, начал раздел 3.",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": "reports/draft.md", "content": "# draft"}
                    ),
                },
            }],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
    ]

    agent = _BudgetAgent(max_iterations=3)
    turn_finalizer.finalize_turn(
        agent,
        final_response=None,
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id=tid,
        turn_id="turn-1",
        user_message="собери отчёт",
        original_user_message="собери отчёт",
        _should_review_memory=False,
        _turn_exit_reason="max_iterations_reached(3/3)",
    )

    # ---- read the real rows back -----------------------------------------
    with kb.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"]
        root = kb.get_task(conn, tid)
        run_row = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        payload_raw = events[0].payload if events else {}
        split_child_ids = list(payload_raw.get("child_ids") or [])
        children = [kb.get_task(conn, cid) for cid in split_child_ids]
        # Cross-check: the waiting-trick links (child -> root) must exist.
        linked = {
            r["parent_id"] for r in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (tid,),
            ).fetchall()
        }
        child_ids = [c.id for c in children if c is not None]
        bodies = [c.body or "" for c in children if c is not None]

    assert len(events) == 1, f"expected exactly one budget_split event, got {len(events)}"
    payload = events[0].payload
    note = payload.get("progress_note") or ""

    # 1. the note is real and non-empty, sourced from the run transcript
    assert note, "budget_split event must carry a non-empty progress_note"
    assert payload.get("progress_note_source") == "run_buffer"
    # Event payload stores a bounded copy (first 300 chars) plus the REAL length;
    # the full note is what lands in the child bodies.
    assert payload.get("progress_note_len") > 300
    assert len(note) == 300
    assert turn_finalizer.PROGRESS_DONE_HEADER in note
    assert "3/3" in note
    assert "Файлы, созданные/изменённые в этом прогоне" in note

    # 2. the split actually happened and closed the run
    assert len(child_ids) >= 2, f"expected 2+ children, got {child_ids}"
    assert set(child_ids) == linked, "each child must link the root as its child"
    assert root.status == "todo"
    assert run_row is not None and run_row["outcome"] == "budget_split"

    # 3. every child body carries the handoff block and the note itself
    for cid, body in zip(child_ids, bodies):
        assert "Progress note from the previous worker" in body, cid
        assert "reports/draft.md" in body, cid

    # Report the actual rows so the completion handoff can quote them.
    print("\n=== budget_split event (real DB row) ===")
    print("task:", tid, "| event kind:", events[0].kind)
    print("payload.progress_note_source =", payload["progress_note_source"])
    print("payload.progress_note_len    =", payload["progress_note_len"])
    print("payload.child_ids            =", payload.get("child_ids"))
    print("payload.progress_note (first 400 chars):")
    print(note[:400])
    print("\n=== children (real DB rows) ===")
    for cid, body in zip(child_ids, bodies):
        print(f"- {cid}: {body.splitlines()[0] if body else ''}")
        print("  has progress block:", "Progress note from the previous worker" in body)
        print("  has reports/draft.md:", "reports/draft.md" in body)


class _MidWorkAgent(_BudgetAgent):
    """Same seam, but the worker's last words describe UNFINISHED work.

    Reviewer reproduction on 8c4ecd0589: this exact tail used to be classified
    close-only, so the remainder (раздел 3) got no card at all.

    Uses ``_turn_exit_reason="budget_exhausted"`` — the branch where the model
    is actually asked for a final summary and ``_handle_max_iterations`` runs.
    (The transcript row alone says only «Готовы разделы 1 и 2», i.e. it stops
    one step short of what remains; the summary is the fresher text and must
    reach the children.)
    """

    def _handle_max_iterations(self, messages, api_call_count):
        return "Раздел 1 и 2 готовы, осталось закрыть раздел 3."


def test_mid_work_exit_through_the_real_seam_still_splits(
    kanban_home, no_llm, monkeypatch, tmp_path,
):
    workspace = tmp_path / "ws2"
    workspace.mkdir()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Собрать отчёт по разделам 1-4",
            body="acceptance: all four sections present",
            assignee="03-software-engineer",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        assert kb.claim_task(conn, tid) is not None
        run_id = kb._current_run_id(conn, tid)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    messages = [
        {"role": "user", "content": "собери отчёт"},
        {
            "role": "assistant",
            "content": "Готовы разделы 1 и 2.",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": "reports/sections12.md", "content": "# 1-2"}
                    ),
                },
            }],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
    ]

    agent = _MidWorkAgent(max_iterations=3)
    turn_finalizer.finalize_turn(
        agent,
        final_response=None,
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id=tid,
        turn_id="turn-mid",
        user_message="собери отчёт",
        original_user_message="собери отчёт",
        _should_review_memory=False,
        _turn_exit_reason="budget_exhausted",
    )

    with kb.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"]
        payload = events[0].payload if events else {}
        children = [kb.get_task(conn, cid) for cid in (payload.get("child_ids") or [])]
    assert len(events) == 1
    # `via` is NOT a payload key (real keys: child_ids, depth, progress_note,
    # progress_note_len, progress_note_source, source), so a `payload.get("via")`
    # assertion here would be vacuously true. Collapse is caught structurally
    # instead: no close-only title, and the remainder keeps a real card.
    child_ids = [c.id for c in children if c is not None]
    assert len(child_ids) >= 2, f"mid-work must not collapse to one card: {child_ids}"
    for c in children:
        if c is None:
            continue
        assert "[close only]" not in (c.title or ""), c.id
        assert "ALREADY DONE" not in (c.body or ""), c.id
        assert "осталось закрыть раздел 3" in (c.body or ""), c.id
    print("\n=== mid-work split (real DB rows) ===")
    print("payload.source =", payload.get("progress_note_source"),
          "| children =", child_ids,
          "| titles =", [(c.title or "")[:48] for c in children if c is not None])


# ---------------------------------------------------------------------------
# The reviewer's differential case, pinned through the REAL seam
# ---------------------------------------------------------------------------
# On 15743759b0 this exact pair diverged: a partial-credit summary collapsed to
# one card while the genuine close-only one also collapsed (i.e. the two were
# indistinguishable). Both are driven through finalize_turn → run_budget_split
# → SQLite here, so the class boundary is asserted end-to-end, not on bare
# strings.

class _PartialCreditAgent(_BudgetAgent):
    """Worker whose post-budget summary admits the work is only partly done."""

    def _handle_max_iterations(self, messages, api_call_count):
        return "Работа выполнена частично, осталось доделать раздел 3."


class _EverythingButTestsAgent(_BudgetAgent):
    def _handle_max_iterations(self, messages, api_call_count):
        return "Всё сделано, кроме тестов — их надо дописать и прогнать."


class _ColonEnumerationAgent(_BudgetAgent):
    """Reviewer r3 (a): «осталось закрыть:» introduces an ENUMERATION of work.

    `:` was treated as a clause end by c2338e94d1, so this note collapsed to one
    «[close only]» card whose body named «раздел 3» AND «ALREADY DONE» — the
    remainder got no card at all. This is the exact reviewer reproduction.
    """

    def _handle_max_iterations(self, messages, api_call_count):
        return "Осталось закрыть: раздел 3."


class _CardArtifactPlusWorkAgent(_BudgetAgent):
    """Reviewer r3 (a): artifact word followed by more work in the same clause."""

    def _handle_max_iterations(self, messages, api_call_count):
        return "Осталось закрыть карточку и раздел 3."


class _WorkItemTaskAgent(_BudgetAgent):
    """Reviewer r3 (b): «закрыть задачу <работа>» — a WORK ITEM, not the card.

    REGRESSION on c2338e94d1 relative to 15743759b0: `задач[у]` had been added as
    a free alternative in the card-artifact group, so closing a work task was
    mistaken for closing the board card.
    """

    def _handle_max_iterations(self, messages, api_call_count):
        return "Осталось закрыть задачу миграции на прод."


class _GenuineCloseOnlyAgent(_BudgetAgent):
    def _handle_max_iterations(self, messages, api_call_count):
        return "Работа выполнена, артефакт собран, осталось закрыть карточку."


def _run_seam_and_read(kanban_home, monkeypatch, tmp_path, agent, label):
    workspace = tmp_path / ("ws_" + label)
    workspace.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Собрать отчёт по разделам 1-4",
            body="acceptance: all four sections present",
            assignee="03-software-engineer",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        assert kb.claim_task(conn, tid) is not None
        run_id = kb._current_run_id(conn, tid)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    messages = [
        {"role": "user", "content": "собери отчёт"},
        {
            "role": "assistant",
            "content": "Начал работу над отчётом.",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": "reports/draft.md", "content": "# draft"}
                    ),
                },
            }],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
    ]

    turn_finalizer.finalize_turn(
        agent,
        final_response=None,
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id=tid,
        turn_id="turn-" + label,
        user_message="собери отчёт",
        original_user_message="собери отчёт",
        _should_review_memory=False,
        _turn_exit_reason="budget_exhausted",
    )

    with kb.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "budget_split"]
        payload = events[0].payload if events else {}
        children = [kb.get_task(conn, cid) for cid in (payload.get("child_ids") or [])]
    assert len(events) == 1, f"{label}: expected exactly one budget_split event"
    return tid, payload, [c for c in children if c is not None]


@pytest.mark.parametrize(
    "agent_cls,label,expect_children",
    [
        (_PartialCreditAgent, "partial-credit", 2),
        (_EverythingButTestsAgent, "everything-except-tests", 2),
        # Reviewer r3 blocking finding, both sub-classes, end-to-end.
        (_ColonEnumerationAgent, "colon-enumeration", 2),
        (_CardArtifactPlusWorkAgent, "artifact-plus-work", 2),
        (_WorkItemTaskAgent, "work-item-task", 2),
        (_GenuineCloseOnlyAgent, "genuine-close-only", 1),
    ],
)
def test_class_boundary_through_the_real_seam(
    kanban_home, no_llm, monkeypatch, tmp_path,
    agent_cls, label, expect_children,
):
    tid, payload, children = _run_seam_and_read(
        kanban_home, monkeypatch, tmp_path, agent_cls(max_iterations=3), label,
    )
    assert payload.get("progress_note_source") == "run_buffer", payload
    assert payload.get("progress_note_len", 0) > 0, payload
    assert len(children) == expect_children, (
        f"{label}: expected {expect_children} children, got "
        f"{[(c.id, c.title) for c in children]}"
    )

    close_only = [c for c in children if "[close only]" in (c.title or "")]
    if expect_children == 1:
        assert len(close_only) == 1
        assert "ALREADY DONE" in (close_only[0].body or "")
    else:
        # Unfinished work must keep real cards AND never claim it is done.
        assert close_only == [], f"{label} collapsed to close-only: {close_only[0].title}"
        for c in children:
            assert "ALREADY DONE" not in (c.body or ""), c.id
            assert "Progress note from the previous worker" in (c.body or ""), c.id

    print("\n=== seam: %s ===" % label)
    print("task:", tid, "| source:", payload.get("progress_note_source"),
          "| note_len:", payload.get("progress_note_len"))
    for c in children:
        print("  -", c.id, "|", (c.title or "")[:60],
              "| ALREADY_DONE:", "ALREADY DONE" in (c.body or ""))


# ---------------------------------------------------------------------------
# Reviewer r4 (23b6af0536) regressions, through the REAL seam
# ---------------------------------------------------------------------------
# `\n` had been accepted as the end of the closer's clause, so a worker whose
# budget-exhaustion summary listed the remainder on the following lines
#
#     Сделал разделы 1-2. Осталось закрыть
#     - раздел 3
#     - тесты
#
# collapsed into ONE «[close only] … ALREADY DONE — DO NOT redo» card: «раздел
# 3» and «тесты» got no card at all, while the only child was told the work was
# finished. r1/r2 split this correctly; r3 regressed it. Pinned here on the real
# finalize_turn → run_budget_split → SQLite path, not on bare strings.

class _BulletListRemainderAgent(_BudgetAgent):
    def _handle_max_iterations(self, messages, api_call_count):
        return "Сделал разделы 1-2. Осталось закрыть\n- раздел 3\n- тесты"


class _NewlinePlainRemainderAgent(_BudgetAgent):
    def _handle_max_iterations(self, messages, api_call_count):
        return "Осталось закрыть\nраздел 3 и тесты"


@pytest.mark.parametrize(
    "agent_cls,label",
    [
        (_BulletListRemainderAgent, "r4-bullets-after-newline"),
        (_NewlinePlainRemainderAgent, "r4-newline-plain-list"),
    ],
)
def test_r4_newline_remainder_keeps_real_cards(
    kanban_home, no_llm, monkeypatch, tmp_path, agent_cls, label,
):
    tid, payload, children = _run_seam_and_read(
        kanban_home, monkeypatch, tmp_path, agent_cls(max_iterations=3), label,
    )
    ids = [c.id for c in children]
    print("\n=== %s === " % label)
    print("summary:", repr(agent_cls(max_iterations=3)._handle_max_iterations([], 0)))
    print("source :", payload.get("progress_note_source"),
          "| len:", payload.get("progress_note_len"))
    for c in children:
        print("  child:", c.id, "|", (c.title or "")[:56],
              "| ALREADY_DONE:", "ALREADY DONE" in (c.body or ""))

    assert payload.get("progress_note_source") == "run_buffer", payload
    assert len(ids) >= 2, (
        f"{label}: the named remainder collapsed to {len(ids)} card(s) "
        f"{[(c.id, (c.title or '')[:40]) for c in children]} — "
        "«раздел 3» loses its card while the child claims ALREADY DONE"
    )
    for c in children:
        assert "[close only]" not in (c.title or ""), c.id
        assert "ALREADY DONE" not in (c.body or ""), c.id
