"""Planning-node shape signal for the ``auto`` effort pin (t_cabee680).

The gap this closes
-------------------
``auto`` (t_f89294b8) resolved a card's level from its shape AT SPAWN TIME, and
the only board-owned shape signal was the DIRECT CHILD COUNT. That made the
level depend on the ORDER in which a chain happens to be executed: a mid-chain
node whose children were created only after its first spawn looked like a LEAF
on that spawn and ran ``low`` — exactly the decomposition work that needs the
top level. The same signal also decayed backwards: once the children closed,
``child_count`` returned to 0 and the node fell back to ``low`` while it was
still assembling/re-checking its children's output.

The two halves of the fix
-------------------------
1. ``planning_node`` — an explicit, CREATE-time card flag
   (``create_task(planning_node=True)`` / ``kanban create --planning-node`` /
   ``kanban_create(planning_node=True)``). The resolver reads it BEFORE any
   graph signal, so a declared node resolves ``max`` immediately, with zero
   children, in any position of any chain.
2. ``latch_planning_node`` — a one-way inference: a card OBSERVED owning at
   least one child is marked a node, so the inference survives the children's
   completion instead of silently decaying back to ``low``.

What is asserted here (each maps to a criterion of the card)
------------------------------------------------------------
* a 3-link chain whose children appear AFTER the first spawn of the parent
  resolves parent=nodes ``max`` / leaves ``low`` — and the SAME test fails with
  the heuristic disabled (the negative control at the bottom);
* an explicit pin still beats every piece of shape information, including the
  new flag;
* the latch is idempotent, one-way, ``auto``-only, and auditable;
* ``auto`` still never emits ``medium``/``minimal``/``ultra``;
* the operator surfaces (``show`` text, ``--json``, ``kanban_show`` tool) name
  the node property and the resolved level together.
"""

from __future__ import annotations

import argparse
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
    # The dispatch tick swaps HERMES_HOME to the assignee profile env before the
    # provider pre-flight, so every assignee used here must exist as a real
    # profile dir under the isolated root.
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
    # Every card runs on the letovo wire in these tests, which is what makes the
    # "never emit medium/minimal/ultra" assertions meaningful.
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
# 1. The flag exists, persists, and is what the operator declared
# ---------------------------------------------------------------------------


def test_planning_node_defaults_to_false_and_round_trips(conn):
    plain = kb.create_task(
        conn, title="исполнение", assignee="03-software-engineer"
    )
    node = kb.create_task(
        conn,
        title="постановка",
        assignee="03-software-engineer",
        planning_node=True,
    )
    assert kb.get_task(conn, plain).planning_node is False
    assert kb.get_task(conn, node).planning_node is True
    # The flag is independent of the stored level: it describes POSITION, not
    # depth, so it must survive on an explicitly pinned card too.
    pinned = kb.create_task(
        conn,
        title="постановка с пином",
        assignee="03-software-engineer",
        reasoning_effort="xhigh",
        planning_node=True,
    )
    row = kb.get_task(conn, pinned)
    assert row.planning_node is True
    assert row.reasoning_effort == "xhigh"


def test_planning_node_is_read_by_the_pure_resolver():
    from agent.card_reasoning_autopin import resolve_auto_effort

    level, reason = resolve_auto_effort(
        assignee="03-software-engineer",
        title="обычная карточка",
        child_count=0,
        planning_node=True,
    )
    assert (level, reason) == ("max", "auto:planning-node")
    # ... and the absence of the flag keeps the card in the execution lane.
    assert resolve_auto_effort(
        assignee="03-software-engineer",
        title="обычная карточка",
        child_count=0,
        planning_node=False,
    ) == ("low", "auto:execution")


# ---------------------------------------------------------------------------
# 2. THE CARD'S OWN TEST: a 3-link chain whose children appear LATE
# ---------------------------------------------------------------------------


def test_chain_of_three_resolves_nodes_max_and_leaves_low_before_children_exist(
    conn, monkeypatch, tmp_path
):
    """Decomposition chain, built top-down: every spawn happens FIRST.

    This is the exact shape the card names («цепочка из 3 звеньев, дети
    создаются ПОСЛЕ первого спавна родителя»). L1 is declared a node; L2 and L3
    are declared nodes as well EXCEPT that L3 is a leaf; the point is that the
    parent's level is decided before any child exists.
    """
    l1 = kb.create_task(
        conn,
        title="звено 1 — постановка",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        planning_node=True,
    )
    # FIRST spawn, zero children exist in the whole board.
    cmd_l1 = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, l1))
    assert _level(cmd_l1) == "max", "a declared node must not depend on the graph"

    l2 = kb.create_task(
        conn,
        title="звено 2 — постановка",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        planning_node=True,
        parents=[l1],
    )
    cmd_l2 = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, l2))
    assert _level(cmd_l2) == "max"

    l3 = kb.create_task(
        conn,
        title="звено 3 — исполнение",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        parents=[l2],
    )
    cmd_l3 = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, l3))
    assert _level(cmd_l3) == "low", "the leaf of the chain stays cheap"

    # The whole point: the two NODES are on max and the LEAF is on low, even
    # though every level above was resolved before its children existed.
    assert (_level(cmd_l1), _level(cmd_l2), _level(cmd_l3)) == ("max", "max", "low")


def test_negative_control_the_same_chain_without_the_flag_degrades_to_low(
    conn, monkeypatch, tmp_path
):
    """The same graph, WITHOUT the declaration, still shows the old defect.

    This is the "тест падает при отключённой эвристике" half of the criterion:
    with ``planning_node`` false the first spawn of a mid-chain node resolves
    ``low``, i.e. the heuristic is what carries the fix (not some other signal
    that happens to be true in the test above).
    """
    l1 = kb.create_task(
        conn,
        title="звено 1 без метки",
        assignee="03-software-engineer",
        reasoning_effort="auto",
    )
    assert _level(_spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, l1))) == "low"

    l2 = kb.create_task(
        conn,
        title="звено 2 без метки",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        parents=[l1],
    )
    # l2 DOES have a parent, but a parent is not a node signal: still low.
    assert _level(_spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, l2))) == "low"


# ---------------------------------------------------------------------------
# 3. The latch: observed-with-children is remembered, one way
# ---------------------------------------------------------------------------


def test_latch_marks_an_undeclared_node_and_survives_the_children_closing(
    conn, monkeypatch, tmp_path
):
    """The inference must not decay once the children are done."""
    parent = kb.create_task(
        conn, title="узел без метки", assignee="03-software-engineer",
        reasoning_effort="auto",
    )
    child = kb.create_task(
        conn, title="ребёнок", assignee="07-document-producer", parents=[parent]
    )

    # First observation with a child latches the flag.
    assert kb.latch_planning_node(conn, [parent]) == [parent]
    assert kb.get_task(conn, parent).planning_node is True
    # Idempotent: a second pass writes nothing.
    assert kb.latch_planning_node(conn, [parent]) == []

    # The audit trail names the inference, so "declared" and "latched" stay
    # distinguishable on the board.
    kinds = [
        e["kind"]
        for e in (
            json.loads(line)
            for line in _events_json(conn, parent)
        )
    ]
    assert "planning_node_latched" in kinds

    # Close the child: child_count drops to 0, the node flag does not.
    kb.complete_task(conn, child, result="done")
    assert kb.get_task(conn, parent).planning_node is True
    cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, parent))
    assert _level(cmd) == "max", "the node stays on max after its children close"


def test_latch_never_touches_an_explicitly_pinned_card(conn):
    """The latch is ``auto``-only: an operator pin is not a consumer."""
    pinned = kb.create_task(
        conn,
        title="карточка с явным пином",
        assignee="03-software-engineer",
        reasoning_effort="high",
    )
    kb.create_task(
        conn, title="ребёнок", assignee="07-document-producer", parents=[pinned]
    )
    assert kb.latch_planning_node(conn, [pinned]) == []
    assert kb.get_task(conn, pinned).planning_node is False


def test_latch_is_fail_soft_on_a_missing_card(conn):
    """A stale id must never break a spawn."""
    assert kb.latch_planning_node(conn, ["no-such-task-anywhere"]) == []
    assert kb.latch_planning_node(conn, []) == []
    assert kb.latch_planning_node(None, ["anything"]) == []


def _events_json(conn, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    return [
        json.dumps(
            {
                "kind": r["kind"],
                "payload": json.loads(r["payload"]) if r["payload"] else None,
            }
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 4. An explicit pin still wins over every shape signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("explicit", ["none", "low", "high", "xhigh", "max"])
def test_an_explicit_pin_beats_the_planning_node_flag(
    conn, monkeypatch, tmp_path, explicit
):
    tid = kb.create_task(
        conn,
        title="узел с пином оператора",
        assignee="03-software-engineer",
        reasoning_effort=explicit,
        planning_node=True,
    )
    cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid))
    assert _level(cmd) == explicit
    assert kb.get_task(conn, tid).reasoning_effort == explicit


# ---------------------------------------------------------------------------
# 5. ``auto`` still never leaves the served domain
# ---------------------------------------------------------------------------


def test_auto_never_emits_an_out_of_domain_level_with_the_node_signal():
    from agent.card_reasoning_autopin import resolve_auto_effort

    produced = set()
    for assignee in (
        "03-software-engineer",
        "07-document-producer",
        "09-independent-reviewer",
        "15-sre-qa",
        "01-orchestrator",
        "04-web-researcher",
    ):
        for child_count in (0, 1, 3):
            for goal in (False, True):
                for node in (False, True):
                    level, reason = resolve_auto_effort(
                        assignee=assignee,
                        title="контейнер",
                        goal_mode=goal,
                        child_count=child_count,
                        planning_node=node,
                    )
                    produced.add(level)
                    assert reason.startswith("auto:"), reason
    assert produced <= {"low", "max"}, produced
    assert not (produced & {"medium", "minimal", "ultra"}), produced


# ---------------------------------------------------------------------------
# 6. The dispatch lane carries the flag through (both lanes read the same shape)
# ---------------------------------------------------------------------------


def test_dispatch_tick_latches_and_carries_the_node_signal(conn, monkeypatch):
    """A node observed with a child is latched BEFORE its spawn resolves."""
    seen: list = []

    def fake_default_spawn(task, workspace, **kwargs):
        seen.append((task.id, bool(task.planning_node)))
        return 4242

    monkeypatch.setattr(kb, "_default_spawn", fake_default_spawn)
    # Same hermetic pattern as the other dispatch suites: the runtime MODEL_LOCK
    # gate in ``resolve_task_root_route`` resolves against the HOST's provider
    # registry, so an unpinned card is blocked and never reaches spawn. That
    # wiring is unrelated to the node latch under test — stub it out.
    monkeypatch.setattr(
        kb,
        "resolve_task_root_route",
        lambda conn, tid, persist=False, allow_unpinned=False: (
            (tid,), "03-software-engineer", "deepseek-v4-flash"
        ),
    )

    parent = kb.create_task(
        conn,
        title="узел",
        assignee="03-software-engineer",
        reasoning_effort="auto",
    )
    kb.create_task(
        conn, title="ребёнок", assignee="07-document-producer", parents=[parent]
    )
    kb.dispatch_once(conn, max_spawn=5)

    row = [s for s in seen if s[0] == parent]
    assert row, seen
    assert row[0][1] is True, "the spawn path must see the node flag"
    assert kb.get_task(conn, parent).planning_node is True, "and persist it"


# ---------------------------------------------------------------------------
# 7. Operator surfaces report the node property next to the level
# ---------------------------------------------------------------------------


def test_show_text_names_the_node_property(conn, kanban_home, capsys):
    from hermes_cli import kanban as kc

    tid = kb.create_task(
        conn,
        title="постановка",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        planning_node=True,
    )
    kc._cmd_show(
        argparse.Namespace(task_id=tid, json=False, state_type=None, state_name=None)
    )
    out = capsys.readouterr().out
    assert "reasoning: max (auto; stored pin: auto)" in out, out
    assert "node:      yes" in out, out


def test_show_text_names_the_node_property_on_an_explicitly_pinned_card(
    conn, kanban_home, capsys
):
    """A hand-pinned node still advertises the flag.

    Regression for the observability gap: the ``node:`` line originally printed
    only for ``auto`` cards, so an operator looking at a ``high``-pinned node had
    no way to see that the shape flag was set (or silently drifted onto a leaf).
    """
    from hermes_cli import kanban as kc

    tid = kb.create_task(
        conn,
        title="постановка с явным пином",
        assignee="03-software-engineer",
        reasoning_effort="high",
        planning_node=True,
    )
    kc._cmd_show(
        argparse.Namespace(task_id=tid, json=False, state_type=None, state_name=None)
    )
    out = capsys.readouterr().out
    assert "reasoning: high" in out, out
    assert "node:      yes" in out, out
    # The explicit pin governs, and the line must say so rather than implying
    # the flag forced max.
    assert "explicit pin governs the level" in out, out


def test_show_text_says_no_for_an_execution_card(conn, kanban_home, capsys):
    from hermes_cli import kanban as kc

    tid = kb.create_task(
        conn,
        title="исполнение",
        assignee="03-software-engineer",
        reasoning_effort="auto",
    )
    kc._cmd_show(
        argparse.Namespace(task_id=tid, json=False, state_type=None, state_name=None)
    )
    out = capsys.readouterr().out
    assert "reasoning: low (auto; stored pin: auto)" in out, out
    assert "node:      no" in out, out


def test_show_json_reports_the_node_property(conn, kanban_home):
    from hermes_cli import kanban as kc

    tid = kb.create_task(
        conn,
        title="постановка",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        planning_node=True,
    )
    payload = kc._task_to_dict(kb.get_task(conn, tid))
    assert payload["planning_node"] is True
    assert payload["effective_reasoning_effort"] == "max"
    assert payload["reasoning_effort"] == kb.AUTO_EFFORT_PIN


def test_kanban_show_tool_reports_the_node_property(conn, kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    tid = kb.create_task(
        conn,
        title="постановка",
        assignee="03-software-engineer",
        reasoning_effort="auto",
        planning_node=True,
    )
    out = json.loads(kt._handle_show({"task_id": tid}))["task"]
    assert out["planning_node"] is True
    assert out["effective_reasoning_effort"] == "max"


def test_kanban_create_tool_accepts_the_flag(conn, kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    raw = kt._handle_create(
        {
            "title": "постановка через инструмент",
            "assignee": "03-software-engineer",
            "reasoning_effort": "auto",
            "planning_node": True,
        }
    )
    payload = json.loads(raw)
    tid = payload.get("task_id") or payload.get("id") or payload["task"]["id"]
    task = kb.get_task(conn, tid)
    assert task.planning_node is True
    assert task.reasoning_effort == kb.AUTO_EFFORT_PIN
    assert kb.effective_card_reasoning_effort(task, conn=conn)[0] == "max"


# ---------------------------------------------------------------------------
# 8. The board sweep agrees with the spawn resolver about a node
# ---------------------------------------------------------------------------


def test_autopin_sweep_agrees_with_the_create_time_node_decision(conn):
    """A declared node is born on ``max`` and the sweep leaves it there."""
    from agent.card_reasoning_autopin import autopin_active_cards

    tid = kb.create_task(
        conn, title="узел", assignee="03-software-engineer", planning_node=True
    )
    # The CREATE-time rule already resolved the shape, so nothing is left for
    # the sweep to correct — this is the "no second opinion" property.
    stored = kb.get_task(conn, tid).reasoning_effort
    assert stored == "max", stored
    report = autopin_active_cards(conn, task_ids=[tid])
    assert report["changed"] == [], report
    assert kb.get_task(conn, tid).reasoning_effort == "max"


def test_autopin_sweep_repairs_a_legacy_null_node_to_max(conn):
    """A node row that predates the rule (NULL level) lands on ``max``."""
    from agent.card_reasoning_autopin import autopin_active_cards

    tid = kb.create_task(
        conn, title="узел", assignee="03-software-engineer", planning_node=True
    )
    # Simulate a legacy row: level never materialized.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET reasoning_effort = NULL, "
            "reasoning_effort_source = NULL WHERE id = ?",
            (tid,),
        )
    report = autopin_active_cards(conn, task_ids=[tid])
    assert [c[2] for c in report["changed"]] == ["max"], report
    assert "planning-node" in report["changed"][0][3], report
    assert kb.get_task(conn, tid).reasoning_effort == "max"
