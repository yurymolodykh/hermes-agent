"""``auto`` card pins — the level is chosen from the card's SHAPE at spawn.

t_f89294b8. Before this change every card carried a frozen level, so an
operator had to guess the depth per card and could not express "this card is
an authoring/container card — give it the top level, and a mechanical one —
give it the cheap one". The ``auto`` sentinel is that expression:

1. ``normalize_reasoning_effort`` accepts ``auto`` (and ``clamp_card_effort``
   never maps it onto a wire level — it is a RULE, not a depth).
2. ``create_task(..., reasoning_effort="auto")`` STORES the sentinel, so the
   pin keeps describing the rule instead of freezing today's answer.
3. The spawn path resolves it into a concrete level from the card's own shape,
   strictly in this order (first match wins):

   * ``09-independent-reviewer`` / ``15-sre-qa`` → ``max``;
   * ``01-orchestrator`` OR a card with direct children OR ``goal_mode`` OR a
     РГЗ phrase → ``max``;
   * everything else → ``low``.

4. ``kanban show`` / the ``kanban_show`` tool expose the stored sentinel AND
   the level it currently resolves to; the ``spawned`` event records both, so
   ``auto -> low`` / ``auto -> max`` is read from the board, not assumed.
5. An explicit level always wins over ``auto``; archived cards are untouched.

The ordering limit the card explicitly accepted is covered too: a card that
posts its children only AFTER its first spawn resolves ``low`` on that spawn
and ``max`` on the next one (``t_cabee680`` owns closing that gap).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from hermes_cli import kanban_db as kb


pytestmark = pytest.mark.usefixtures(
    "isolated_kanban_route_resolver", "all_assignees_spawnable"
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    # The dispatch tick swaps HERMES_HOME to the assignee profile env before
    # the provider pre-flight, so every assignee used here must exist as a
    # real profile dir under the isolated root.
    for name in (
        "01-orchestrator",
        "03-software-engineer",
        "07-document-producer",
        "09-independent-reviewer",
        "15-sre-qa",
    ):
        (home / "profiles" / name).mkdir(parents=True, exist_ok=True)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home, monkeypatch):
    # Every card runs on the letovo wire in these tests, which is what makes
    # the "never emit medium/minimal/ultra" assertions meaningful.
    monkeypatch.setattr(
        kb,
        "_assignee_profile_default_route",
        lambda _a: ("custom:letovo", "deepseek-v4-flash"),
    )
    c = kb.connect()
    yield c
    c.close()


def _spawn_cmd(monkeypatch, tmp_path, task) -> list[str]:
    """Build the worker argv for *task* without launching anything."""
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        kb,
        "_assignee_profile_default_route",
        lambda _a: ("custom:letovo", "deepseek-v4-flash"),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kb._default_spawn(task, str(workspace))
    return captured["cmd"]


def _level(cmd: list[str]) -> str:
    return cmd[cmd.index("--reasoning") + 1]


# ---------------------------------------------------------------------------
# Acceptance 1 — ``auto`` is a storable, valid pin
# ---------------------------------------------------------------------------


def test_normalize_accepts_auto_case_insensitively():
    assert kb.normalize_reasoning_effort("auto") == kb.AUTO_EFFORT_PIN
    assert kb.normalize_reasoning_effort("AUTO") == kb.AUTO_EFFORT_PIN
    assert kb.normalize_reasoning_effort("  Auto ") == kb.AUTO_EFFORT_PIN


def test_normalize_never_clamps_auto_onto_the_letovo_domain():
    """``auto`` is a rule; clamping would silently turn it into ``high``."""
    assert (
        kb.normalize_reasoning_effort("auto", provider="custom:letovo")
        == kb.AUTO_EFFORT_PIN
    )


def test_clamp_card_effort_lets_auto_through_untouched():
    assert (
        kb.clamp_card_effort_to_route("auto", provider="custom:letovo")
        == kb.AUTO_EFFORT_PIN
    )


def test_create_task_stores_the_auto_sentinel(conn):
    tid = kb.create_task(
        conn, title="t", assignee="03-software-engineer", reasoning_effort="auto"
    )
    task = kb.get_task(conn, tid)
    assert task.reasoning_effort == kb.AUTO_EFFORT_PIN
    assert task.reasoning_effort_source == "pin"


def test_create_task_still_rejects_a_typo(conn):
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        kb.create_task(
            conn, title="t", assignee="03-software-engineer", reasoning_effort="autoo"
        )


# ---------------------------------------------------------------------------
# Acceptance 1 (cont.) — the 15 card shapes give the expected levels
# ---------------------------------------------------------------------------

#: ``(assignee, title, goal_mode, child_count, expected)`` — the card body's
#: "15 разных форм карточки" plus a few degenerate inputs, so one table drives
#: the whole rule order.
AUTO_SHAPES = [
    # --- review gate: always max -------------------------------------------
    ("09-independent-reviewer", "Effort-P5 приёмка механизма", False, 0, "max"),
    ("15-sre-qa", "регрессия и merge", False, 0, "max"),
    ("15-sre-qa", "ревью с детьми", False, 2, "max"),
    # --- authoring / container: max ---------------------------------------
    ("01-orchestrator", "постановка карточек", False, 0, "max"),
    ("01-orchestrator", "постановка, дети ещё не созданы", False, 0, "max"),
    ("03-software-engineer", "узел с детьми", False, 1, "max"),
    ("07-document-producer", "узел с тремя детьми", False, 3, "max"),
    ("03-software-engineer", "goal-loop карточка", True, 0, "max"),
    ("04-web-researcher", "гигантский план миссии по рынку", False, 0, "max"),
    ("02-hermes-platform", "гигантская задача: переписать контур", False, 0, "max"),
    ("05-rag-knowledge-engineer", "гигантский проект индексации", False, 0, "max"),
    # --- execution lane: low ----------------------------------------------
    ("03-software-engineer", "обычная авторская карточка", False, 0, "low"),
    ("04-web-researcher", "сбор сырья по конкурентам", False, 0, "low"),
    ("07-document-producer", "сверстать приложение", False, 0, "low"),
]


@pytest.mark.parametrize(
    "assignee,title,goal_mode,child_count,expected", AUTO_SHAPES
)
def test_auto_rule_table(
    conn, assignee, title, goal_mode, child_count, expected
):
    """Each card shape resolves to the expected level, end to end through the DB."""
    tid = kb.create_task(
        conn,
        title=title,
        assignee=assignee,
        goal_mode=goal_mode,
        reasoning_effort="auto",
    )
    for i in range(child_count):
        kb.create_task(
            conn,
            title=f"child {i}",
            assignee="07-document-producer",
            parents=[tid],
        )
    task = kb.get_task(conn, tid)
    level, is_default = kb.effective_card_reasoning_effort(task, conn=conn)
    assert level == expected, (assignee, title, level)
    assert is_default is False, "auto is a pin, not a substituted default"
    # The stored column keeps describing the RULE.
    assert task.reasoning_effort == kb.AUTO_EFFORT_PIN


def test_auto_never_emits_an_out_of_domain_level(conn):
    """No shape may resolve to a level the letovo wire answers 400 on."""
    forbidden = {"medium", "minimal", "ultra"}
    produced = set()
    for assignee, title, goal_mode, child_count, _expected in AUTO_SHAPES:
        from agent.card_reasoning_autopin import resolve_auto_effort

        level, _reason = resolve_auto_effort(
            assignee=assignee,
            title=title,
            goal_mode=goal_mode,
            child_count=child_count,
        )
        produced.add(level)
    assert not (produced & forbidden), produced
    assert produced <= {"low", "max"}, produced


def test_degenerate_input_never_raises(conn):
    """Empty/NULL shapes degrade to the execution lane instead of exploding."""
    from agent.card_reasoning_autopin import resolve_auto_effort

    assert resolve_auto_effort(assignee=None, title=None) == (
        "low",
        "auto:execution",
    )
    assert resolve_auto_effort(assignee="", title="", child_count=-5) == (
        "low",
        "auto:execution",
    )


def test_resolver_survives_a_broken_graph_read(conn):
    """A DB failure degrades to ``low``; a spawn is never blocked by this."""
    tid = kb.create_task(
        conn, title="t", assignee="03-software-engineer", reasoning_effort="auto"
    )
    task = kb.get_task(conn, tid)
    task.id = "no-such-task-anywhere"
    level, reason = kb.resolve_card_auto_effort(conn, task)
    assert level == "low"
    assert reason == "auto:execution"


# ---------------------------------------------------------------------------
# Acceptance 2 — the SPAWN command carries the RESOLVED level, not ``auto``
# ---------------------------------------------------------------------------


def test_spawn_resolves_auto_to_low_for_an_execution_card(
    conn, monkeypatch, tmp_path
):
    tid = kb.create_task(
        conn, title="обычная", assignee="03-software-engineer", reasoning_effort="auto"
    )
    cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid))
    assert _level(cmd) == "low"
    assert kb.AUTO_EFFORT_PIN not in cmd


def test_spawn_resolves_auto_to_max_for_a_container(conn, monkeypatch, tmp_path):
    tid = kb.create_task(
        conn, title="узел", assignee="03-software-engineer", reasoning_effort="auto"
    )
    kb.create_task(
        conn, title="ребёнок", assignee="07-document-producer", parents=[tid]
    )
    cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid))
    assert _level(cmd) == "max"


def test_spawn_resolves_auto_to_max_for_a_review_card(
    conn, monkeypatch, tmp_path
):
    tid = kb.create_task(
        conn,
        title="приёмка",
        assignee="15-sre-qa",
        reasoning_effort="auto",
    )
    cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid))
    assert _level(cmd) == "max"


def test_spawn_records_the_resolution_on_the_task(conn, monkeypatch, tmp_path):
    """``_default_spawn`` stashes the pair the dispatch loop writes to the event."""
    tid = kb.create_task(
        conn, title="t", assignee="01-orchestrator", reasoning_effort="auto"
    )
    task = kb.get_task(conn, tid)
    _spawn_cmd(monkeypatch, tmp_path, task)
    assert task.spawn_reasoning_effort == "max"
    assert task.spawn_reasoning_reason == "auto:authoring-assignee"


def test_spawn_event_records_auto_and_the_resolved_level(conn, monkeypatch, tmp_path):
    """Acceptance: ``auto -> low`` / ``auto -> max`` is READABLE on the board."""
    tid = kb.create_task(
        conn, title="t", assignee="03-software-engineer", reasoning_effort="auto"
    )
    task = kb.get_task(conn, tid)
    _spawn_cmd(monkeypatch, tmp_path, task)
    kb._set_worker_pid(
        conn,
        tid,
        4242,
        auto_effort=task.spawn_reasoning_effort,
        auto_reason=task.spawn_reasoning_reason,
    )
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'spawned' "
        "ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["reasoning_effort_pin"] == "auto"
    assert payload["reasoning_effort_resolved"] == "low"
    assert payload["reasoning_effort_resolution"] == "auto:execution"
    assert payload["pid"] == 4242


def test_spawn_event_is_unchanged_for_a_frozen_pin(conn, monkeypatch, tmp_path):
    """A non-auto card must not grow the auto keys (no noisy payloads)."""
    tid = kb.create_task(
        conn, title="t", assignee="03-software-engineer", reasoning_effort="xhigh"
    )
    task = kb.get_task(conn, tid)
    _spawn_cmd(monkeypatch, tmp_path, task)
    assert task.spawn_reasoning_effort is None
    kb._set_worker_pid(conn, tid, 4242)
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'spawned' "
        "ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert "reasoning_effort_resolved" not in payload


def test_dispatch_tick_carries_the_child_count_to_the_spawn(
    conn, monkeypatch, tmp_path
):
    """The dispatch loop reads the graph ONCE and hands it to the spawn path.

    The full spawn is stubbed out: this test owns the DISPATCH→spawn handoff
    (child_count + the stored sentinel must survive it), not argv/gating
    (covered by the dedicated spawn-command tests above).
    """
    seen: list = []

    def fake_default_spawn(task, workspace, **kwargs):
        seen.append((task.id, task.child_count, task.reasoning_effort))
        return 4242

    monkeypatch.setattr(kb, "_default_spawn", fake_default_spawn)

    parent = kb.create_task(
        conn,
        title="узел",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        model_override="deepseek-v4-flash",
        provider_override="custom:letovo",
    )
    kb.create_task(
        conn, title="ребёнок", assignee="07-document-producer", parents=[parent]
    )
    kb.dispatch_once(conn, max_spawn=5)
    row = [s for s in seen if s[0] == parent]
    assert row, seen
    _tid, child_count, stored = row[0]
    assert child_count == 1
    assert stored == kb.AUTO_EFFORT_PIN


# ---------------------------------------------------------------------------
# Acceptance 4 — an explicit pin always wins; archive is untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("explicit", ["low", "high", "xhigh", "max", "none"])
def test_an_explicit_pin_beats_auto(conn, explicit):
    """Auto is a DEFAULT rule, never an override of an operator's choice."""
    tid = kb.create_task(
        conn,
        title="постановка от оркестратора",
        assignee="01-orchestrator",
        reasoning_effort=explicit,
    )
    task = kb.get_task(conn, tid)
    assert task.reasoning_effort == explicit
    level, is_default = kb.effective_card_reasoning_effort(task, conn=conn)
    assert level == explicit
    assert is_default is False


def test_set_reasoning_effort_can_switch_a_card_to_auto(conn):
    tid = kb.create_task(
        conn, title="t", assignee="03-software-engineer", reasoning_effort="high"
    )
    assert kb.set_reasoning_effort(conn, tid, "auto")
    assert kb.get_task(conn, tid).reasoning_effort == kb.AUTO_EFFORT_PIN


def test_set_reasoning_effort_keeps_auto_out_of_the_domain_clamp(conn):
    """``medium`` still clamps — ``auto`` must not be clamped into a level."""
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    assert kb.set_reasoning_effort(conn, tid, "medium")
    assert kb.get_task(conn, tid).reasoning_effort == "high"
    assert kb.set_reasoning_effort(conn, tid, "auto")
    assert kb.get_task(conn, tid).reasoning_effort == kb.AUTO_EFFORT_PIN


def test_archived_card_keeps_its_auto_pin_untouched(conn):
    tid = kb.create_task(
        conn, title="t", assignee="03-software-engineer", reasoning_effort="auto"
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
    assert kb.get_task(conn, tid).reasoning_effort == kb.AUTO_EFFORT_PIN


def test_autopin_sweep_never_replaces_the_auto_sentinel(conn):
    """The sweep must not freeze an auto card into today's level."""
    from agent.card_reasoning_autopin import autopin_active_cards

    leaf = kb.create_task(
        conn, title="обычная", assignee="03-software-engineer", reasoning_effort="auto"
    )
    container = kb.create_task(
        conn, title="узел", assignee="03-software-engineer", reasoning_effort="auto"
    )
    kb.create_task(
        conn, title="ребёнок", assignee="07-document-producer", parents=[container]
    )
    report = autopin_active_cards(conn)
    assert kb.get_task(conn, leaf).reasoning_effort == kb.AUTO_EFFORT_PIN
    assert kb.get_task(conn, container).reasoning_effort == kb.AUTO_EFFORT_PIN
    skipped = {tid for tid, _level in report["skipped_pinned"]}
    assert {leaf, container} <= skipped


# ---------------------------------------------------------------------------
# Acceptance 5 — idempotency across re-spawns, and the accepted ordering limit
# ---------------------------------------------------------------------------


def test_respawn_after_unblock_keeps_the_same_level(conn, monkeypatch, tmp_path):
    tid = kb.create_task(
        conn, title="обычная", assignee="03-software-engineer", reasoning_effort="auto"
    )
    first = _level(_spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid)))
    second = _level(_spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid)))
    assert first == second == "low"


def test_a_container_that_gains_children_flips_to_max_on_the_NEXT_spawn(
    conn, monkeypatch, tmp_path
):
    """Documented ordering limit: the shape is read at spawn, not predicted."""
    tid = kb.create_task(
        conn, title="узел", assignee="03-software-engineer", reasoning_effort="auto"
    )
    first = _level(_spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid)))
    assert first == "low", "a childless node looks like a leaf on its first spawn"

    kb.create_task(
        conn, title="ребёнок", assignee="07-document-producer", parents=[tid]
    )
    second = _level(_spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid)))
    assert second == "max", "the NEXT spawn follows the new shape"


# ---------------------------------------------------------------------------
# Acceptance 3 — the operator surfaces report BOTH values
# ---------------------------------------------------------------------------


def test_kanban_show_json_reports_the_sentinel_and_the_effective_level(
    conn, kanban_home
):
    from hermes_cli import kanban as kc

    tid = kb.create_task(
        conn, title="t", assignee="03-software-engineer", reasoning_effort="auto"
    )
    payload = kc._task_to_dict(kb.get_task(conn, tid))
    assert payload["reasoning_effort"] == kb.AUTO_EFFORT_PIN
    assert payload["effective_reasoning_effort"] == "low"
    assert payload["reasoning_effort_source"] == "auto"


def test_kanban_show_text_names_auto_and_the_level(conn, kanban_home, capsys):
    import argparse

    from hermes_cli import kanban as kc

    tid = kb.create_task(
        conn, title="t", assignee="01-orchestrator", reasoning_effort="auto"
    )
    kc._cmd_show(
        argparse.Namespace(
            task_id=tid, json=False, state_type=None, state_name=None
        )
    )
    out = capsys.readouterr().out
    assert "reasoning: max (auto; stored pin: auto)" in out, out


def test_kanban_show_tool_reports_auto(conn, kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    tid = kb.create_task(
        conn, title="t", assignee="15-sre-qa", reasoning_effort="auto"
    )
    out = json.loads(kt._handle_show({"task_id": tid}))
    task = out["task"]
    assert task["reasoning_effort"] == kb.AUTO_EFFORT_PIN
    assert task["effective_reasoning_effort"] == "max"
    assert task["reasoning_effort_source"] == "auto"


# ---------------------------------------------------------------------------
# The pin helper the L1 layer uses must not drop ``auto`` to NULL
# ---------------------------------------------------------------------------


def test_pin_card_effort_keeps_auto_verbatim(conn, monkeypatch):
    from agent import reasoning_effort_l1 as l1

    monkeypatch.setattr(l1, "reasoning_class_routing_enabled", lambda _cfg=None: True)
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    assert l1.pin_card_effort(conn, tid, "auto", cfg={}) is True
    assert kb.get_task(conn, tid).reasoning_effort == kb.AUTO_EFFORT_PIN
