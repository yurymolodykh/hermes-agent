"""Per-card reasoning effort — cards never inherit the profile setting.

t_f2dd1fb5. Before this change ``tasks.reasoning_effort`` was allowed to stay
NULL, and the dispatcher only appended ``--reasoning`` when it was set — so a
card silently ran at whatever ``agent.reasoning_effort`` its assignee profile
happened to configure. Two workers on the same board could therefore run the
same card at different depths, and an operator could not see the level from the
board at all.

The contract now:

1. Creation ALWAYS stores an explicit level. An operator pin is normalized and
   clamped to the route's served vocabulary; an absent pin resolves to the
   card's own default — ``max`` for a review-gate assignee (09/15), ``high``
   otherwise.
2. The spawn command ALWAYS carries ``--reasoning <level>``. A legacy NULL row
   gets the card default substituted (and logged once), never "no flag".
3. ``medium``/``minimal``/``ultra`` are clamped onto the letovo wire domain
   (``medium``->``high``, ``minimal``->``low``, ``ultra``->``max``) instead of
   being stored verbatim and 400-ing the next dispatch.
4. ``kanban show`` / ``kanban_show`` expose the effective level plus a
   ``default`` vs ``pin`` provenance marker.

The mass backfill of pre-existing NULL rows is a separate card (t_bdcbc547);
these tests only cover new writes plus the spawn-time substitution.
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


pytestmark = pytest.mark.usefixtures(
    "isolated_kanban_route_resolver", "all_assignees_spawnable"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
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
    # Keep the profile lookup out of the filesystem: every assignee resolves to
    # the same synthetic profile so only the reasoning branch is under test.
    monkeypatch.setattr(
        kb, "_assignee_profile_default_route", lambda _a: ("custom:letovo", "deepseek-v4-flash")
    )
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kb._default_spawn(task, str(workspace))
    return captured["cmd"]


# ---------------------------------------------------------------------------
# Acceptance 1 — create_task always stores a level
# ---------------------------------------------------------------------------


def test_create_task_without_pin_stores_high_default(conn):
    """A routine card owns ``high``; it does not stay NULL."""
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    assert kb.get_task(conn, tid).reasoning_effort == "high"


@pytest.mark.parametrize("assignee", ["09-independent-reviewer", "15-sre-qa"])
def test_create_task_review_assignee_stores_max(conn, assignee):
    """Review-gate cards own the top served level."""
    tid = kb.create_task(conn, title="t", assignee=assignee)
    assert kb.get_task(conn, tid).reasoning_effort == "max"


def test_create_task_keeps_an_explicit_valid_pin(conn):
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer", reasoning_effort="low")
    assert kb.get_task(conn, tid).reasoning_effort == "low"


def test_create_task_accepts_none_as_a_real_pin(conn):
    """``none`` disables thinking — it must survive as a value, not a default."""
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer", reasoning_effort="none")
    assert kb.get_task(conn, tid).reasoning_effort == "none"


def test_create_task_rejects_a_typo(conn):
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        kb.create_task(conn, title="t", assignee="03-software-engineer", reasoning_effort="turbo")


def test_no_new_card_is_created_with_null(conn):
    """Acceptance 4 — the three card shapes all land non-NULL."""
    ids = [
        kb.create_task(conn, title="plain", assignee="03-software-engineer"),
        kb.create_task(conn, title="review", assignee="15-sre-qa"),
    ]
    parent = ids[0]
    ids.append(
        kb.create_task(conn, title="child", assignee="07-document-producer", parents=[parent])
    )
    placeholders = ",".join("?" * len(ids))
    row = conn.execute(
        f"SELECT count(*) AS n FROM tasks "
        f"WHERE reasoning_effort IS NULL AND id IN ({placeholders})",
        ids,
    ).fetchone()
    assert row["n"] == 0


def test_created_event_records_the_level_and_its_provenance(conn):
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    ev = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'",
        (tid,),
    ).fetchone()
    payload = json.loads(ev["payload"])
    assert payload["reasoning_effort"] == "high"
    assert payload["reasoning_effort_default"] is True


# ---------------------------------------------------------------------------
# Acceptance 2 — the spawn command ALWAYS carries --reasoning
# ---------------------------------------------------------------------------


def test_spawn_passes_the_stored_level(conn, monkeypatch, tmp_path):
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer", reasoning_effort="xhigh")
    cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid))
    assert cmd[cmd.index("--reasoning") + 1] == "xhigh"


def test_spawn_substitutes_the_default_for_a_legacy_null_row(conn, monkeypatch, tmp_path):
    """Acceptance 2 core case: NULL in the DB still yields ``--reasoning``."""
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET reasoning_effort = NULL WHERE id = ?", (tid,))
    task = kb.get_task(conn, tid)
    assert task.reasoning_effort is None

    cmd = _spawn_cmd(monkeypatch, tmp_path, task)
    assert "--reasoning" in cmd
    assert cmd[cmd.index("--reasoning") + 1] == kb.DEFAULT_CARD_REASONING_EFFORT


def test_spawn_substitutes_max_for_a_legacy_null_review_row(conn, monkeypatch, tmp_path):
    tid = kb.create_task(conn, title="t", assignee="15-sre-qa")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET reasoning_effort = NULL WHERE id = ?", (tid,))
    cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid))
    assert cmd[cmd.index("--reasoning") + 1] == kb.REVIEW_CARD_REASONING_EFFORT


def test_spawn_never_omits_reasoning_across_card_shapes(conn, monkeypatch, tmp_path):
    """No card shape may fall back to the profile's agent.reasoning_effort."""
    cases = [
        kb.create_task(conn, title="plain", assignee="03-software-engineer"),
        kb.create_task(conn, title="review", assignee="09-independent-reviewer"),
        kb.create_task(conn, title="none", assignee="04-web-researcher", reasoning_effort="none"),
    ]
    for tid in cases:
        cmd = _spawn_cmd(monkeypatch, tmp_path, kb.get_task(conn, tid))
        assert "--reasoning" in cmd, tid


def test_default_substitution_is_announced_once_per_card(conn, monkeypatch, tmp_path, caplog):
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET reasoning_effort = NULL WHERE id = ?", (tid,))
    task = kb.get_task(conn, tid)

    kb._EFFORT_DEFAULT_ANNOUNCED.discard(tid)
    with caplog.at_level("INFO", logger="hermes_cli.kanban_db"):
        _spawn_cmd(monkeypatch, tmp_path, task)
        _spawn_cmd(monkeypatch, tmp_path, task)
    hits = [r for r in caplog.records if "card default" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Acceptance 3 — the letovo wire domain is enforced at the storage boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested,expected",
    [("medium", "high"), ("minimal", "low"), ("ultra", "max")],
)
def test_normalize_clamps_onto_the_letovo_domain(requested, expected):
    assert kb.normalize_reasoning_effort(requested, provider="custom:letovo") == expected


@pytest.mark.parametrize("served", ["none", "low", "high", "xhigh", "max"])
def test_normalize_keeps_served_levels_verbatim(served):
    assert kb.normalize_reasoning_effort(served, provider="custom:letovo") == served


def test_normalize_keeps_medium_for_a_non_letovo_route():
    """The clamp is route-scoped — openai-compatible wires that serve ``medium`` keep it."""
    assert (
        kb.normalize_reasoning_effort(
            "medium", provider="openrouter", model="anthropic/claude-sonnet-4"
        )
        == "medium"
    )


def test_create_task_clamps_a_medium_pin_against_the_assignee_profile_route(conn, monkeypatch):
    """A card with no route pin is clamped against its assignee's default route."""
    monkeypatch.setattr(
        kb, "_assignee_profile_default_route", lambda _a: ("custom:letovo", "deepseek-v4-flash")
    )
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer", reasoning_effort="medium")
    assert kb.get_task(conn, tid).reasoning_effort == "high"


def test_set_reasoning_effort_clamps_and_keeps_a_provenance_event(conn, monkeypatch):
    monkeypatch.setattr(
        kb, "_assignee_profile_default_route", lambda _a: ("custom:letovo", "deepseek-v4-flash")
    )
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    assert kb.set_reasoning_effort(conn, tid, "minimal")
    assert kb.get_task(conn, tid).reasoning_effort == "low"
    ev = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'reasoning_effort_set' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(ev["payload"])
    assert payload["reasoning_effort"] == "low"
    assert payload["reasoning_effort_requested"] == "minimal"


def test_clearing_the_pin_falls_back_to_the_card_default(conn):
    """Clearing is stored as NULL but still RESOLVES to the card default."""
    tid = kb.create_task(conn, title="t", assignee="15-sre-qa", reasoning_effort="low")
    assert kb.set_reasoning_effort(conn, tid, "")
    task = kb.get_task(conn, tid)
    assert task.reasoning_effort is None          # "no pin", not "profile decides"
    assert kb.effective_card_reasoning_effort(task) == ("max", True)


# ---------------------------------------------------------------------------
# Part D — operator-visible effective level
# ---------------------------------------------------------------------------


def test_effective_level_reports_default_provenance_for_a_null_row(conn):
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET reasoning_effort = NULL WHERE id = ?", (tid,))
    level, is_default = kb.effective_card_reasoning_effort(kb.get_task(conn, tid))
    assert (level, is_default) == ("high", True)


def test_effective_level_reports_pin_provenance(conn):
    tid = kb.create_task(conn, title="t", assignee="03-software-engineer", reasoning_effort="low")
    level, is_default = kb.effective_card_reasoning_effort(kb.get_task(conn, tid))
    assert (level, is_default) == ("low", False)


def test_cli_show_marks_a_substituted_default(kanban_home, capsys):
    """``hermes kanban show`` must name the provenance, not stay silent.

    t_d7edc3b2 renamed the two labels: an operator choice reads
    ``(operator pin)`` and a rule/default-derived level reads ``(rule)``.
    """
    from hermes_cli import kanban as kc
    import argparse

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="03-software-engineer")
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET reasoning_effort = NULL WHERE id = ?", (tid,))

    kc._cmd_show(argparse.Namespace(
        task_id=tid,
        json=False,
        state_type=None,
        state_name=None,
    ))
    out = capsys.readouterr().out
    assert "reasoning: high (rule)" in out, out


def test_cli_show_marks_a_pin(kanban_home, capsys):
    from hermes_cli import kanban as kc
    import argparse

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="pinned", assignee="03-software-engineer", reasoning_effort="xhigh"
        )

    kc._cmd_show(argparse.Namespace(
        task_id=tid, json=False, state_type=None, state_name=None,
    ))
    out = capsys.readouterr().out
    assert "reasoning: xhigh (operator pin)" in out, out


def test_kanban_show_tool_exposes_effective_level(kanban_home, monkeypatch):
    """The agent-facing ``kanban_show`` carries the effective level too."""
    import os
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="03-software-engineer")
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET reasoning_effort = NULL WHERE id = ?", (tid,))

    out = json.loads(kt._handle_show({"task_id": tid}))
    task = out["task"]
    assert task["reasoning_effort"] is None
    assert task["effective_reasoning_effort"] == "high"
    assert task["reasoning_effort_source"] == "default"


# ---------------------------------------------------------------------------
# Other card-creation paths must not reopen the NULL hole
# ---------------------------------------------------------------------------


def test_decompose_children_own_a_level(conn):
    """Triage decomposition inserts rows directly — they must not be NULL."""
    root = kb.create_task(
        conn, title="root", assignee="01-orchestrator", triage=True, reasoning_effort="low"
    )
    created = kb.decompose_triage_task(
        conn,
        root,
        root_assignee="01-orchestrator",
        children=[
            {"title": "a", "assignee": "03-software-engineer"},
            {"title": "b", "assignee": "15-sre-qa"},
        ],
    )
    assert created, "decomposition produced no children"
    levels = {
        kb.get_task(conn, cid).reasoning_effort for cid in created
    }
    assert None not in levels, levels
    assert levels == {"high", "max"}, levels


def test_budget_split_children_inherit_or_default_never_null(conn):
    """A NULL parent must not clone the hole into its split children."""
    root = kb.create_task(conn, title="root", assignee="03-software-engineer")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET reasoning_effort = NULL WHERE id = ?", (root,))
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (root,))
    result = kb.split_task_on_budget(
        conn,
        root,
        children=[{"title": "part1"}, {"title": "part2"}],
        expected_run_id=None,
    )
    assert result, "budget split produced no children"
    levels = {kb.get_task(conn, cid).reasoning_effort for cid in result}
    assert None not in levels, levels
    assert levels == {"high"}, levels
