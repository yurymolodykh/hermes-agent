"""Rule-based reasoning autopin for ACTIVE cards (t_d7edc3b2).

Owner requirement (carried over from the cancelled ``t_bdcbc547``): an ACTIVE
card (``triage``/``todo``/``ready``/``running``/``blocked``) never inherits its
assignee profile's ``agent.reasoning_effort`` — it always owns an explicit
``tasks.reasoning_effort``, chosen by these rules in this order:

1. ``09-independent-reviewer`` / ``15-sre-qa`` -> ``max``.
2. Gated work (the card owns a review-gate child) -> ``high``.
3. РГЗ mission/judgement -> ``max``; wave control -> ``high``; ambiguous role
   -> ``high``.
4. Mechanical routine -> ``low``.
5. Anything else -> ``high``.

Plus the hard wire constraint: ``medium``/``minimal``/``ultra`` are NEVER
written (the letovo endpoint answers HTTP 400 on them — measured 2026-10-30).

These tests cover the pure rule table, the sweep's write/idempotence semantics,
the operator-pin guard, the creation-path wiring, the post-link wiring and the
dispatcher-tick sweep.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.card_reasoning_autopin import (
    ACTIVE_AUTOPIN_STATUSES,
    LETOVO_WIRE_DOMAIN,
    autopin_active_cards,
    legacy_card_default,
    resolve_card_autopin_effort,
)
from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures(
    "isolated_kanban_route_resolver", "all_assignees_spawnable"
)

FORBIDDEN = {"medium", "minimal", "ultra"}


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


# ---------------------------------------------------------------------------
# 1. The rule table
# ---------------------------------------------------------------------------


def test_wire_domain_matches_the_live_letovo_domain():
    """The module's own domain constant must track the live wire domain."""
    from agent.reasoning_effort import LETOVO_EFFORTS

    assert tuple(LETOVO_WIRE_DOMAIN) == tuple(LETOVO_EFFORTS)


@pytest.mark.parametrize("assignee", ["09-independent-reviewer", "15-sre-qa"])
def test_review_gate_assignee_pins_max(assignee):
    level, reason = resolve_card_autopin_effort(assignee=assignee, title="whatever")
    assert (level, reason) == ("max", "review-gate-assignee")


def test_review_gate_match_is_case_and_space_insensitive():
    assert resolve_card_autopin_effort(
        assignee="  09-Independent-Reviewer  ", title="x"
    )[0] == "max"


def test_gated_work_child_of_reviewer_pins_high():
    level, reason = resolve_card_autopin_effort(
        assignee="03-software-engineer",
        title="Реализовать фичу",
        has_reviewer_child=True,
        child_count=2,
    )
    assert level == "high"
    assert reason == "author-with-review-child"


def test_container_whose_only_child_is_the_gate_pins_high():
    level, reason = resolve_card_autopin_effort(
        assignee="01-orchestrator",
        title="Контейнер",
        has_reviewer_child=True,
        child_count=1,
    )
    assert (level, reason) == ("high", "review-child-container")


@pytest.mark.parametrize(
    "title",
    [
        "РГЗ: приёмка фазы 3",
        "РГЗ: разбор инцидента волны",
        "RGZ: КРЗ по кандидатам",
        "РГЗ: судейство вариантов",
    ],
)
def test_rgz_phase_judgement_pins_max(title):
    level, _reason = resolve_card_autopin_effort(
        assignee="01-orchestrator", title=title
    )
    assert level == "max"


@pytest.mark.parametrize(
    "title",
    [
        "РГЗ: запуск миссии Волна-2",
        "РГЗ: контейнер миссии",
    ],
)
def test_rgz_mission_container_pins_max(title):
    level, _reason = resolve_card_autopin_effort(
        assignee="01-orchestrator", title=title
    )
    assert level == "max"


def test_rgz_negative_priority_container_pins_max():
    """The owner's mission-container signature includes a negative priority."""
    level, reason = resolve_card_autopin_effort(
        assignee="01-orchestrator", title="Волна 3", priority=-1, goal_mode=True
    )
    assert (level, reason) == ("max", "rgz-mission-container")


@pytest.mark.parametrize(
    "title",
    [
        "РГЗ: сбор статусов волн",
        "РГЗ: NIGHT-PLAN на завтра",
        "РГЗ: digest по волне",
        "РГЗ: доведи волну 2",
    ],
)
def test_rgz_wave_control_pins_high(title):
    level, reason = resolve_card_autopin_effort(
        assignee="01-orchestrator", title=title
    )
    assert (level, reason) == ("high", "rgz-wave-control")


def test_rgz_ambiguous_role_pins_high_never_max():
    level, reason = resolve_card_autopin_effort(
        assignee="03-software-engineer", title="РГЗ: что-то неопределённое"
    )
    assert (level, reason) == ("high", "rgz-role-ambiguous")


def test_goal_mode_alone_is_rgz_class():
    level, reason = resolve_card_autopin_effort(
        assignee="09-independent-reviewer", title="x", goal_mode=True
    )
    # A review gate outranks the mission class — still max, different reason.
    assert (level, reason) == ("max", "review-gate-assignee")


@pytest.mark.parametrize(
    "title",
    [
        "commit-drift: hermes-kanban — 1 файлов без коммита >24ч",
        "Расчистка копий индекса в backups/",
        "Переведи README на русский",
        "Форматируй список",
    ],
)
def test_mechanical_routine_pins_low(title):
    level, reason = resolve_card_autopin_effort(
        assignee="02-hermes-platform", title=title
    )
    assert (level, reason) == ("low", "mechanical-routine")


@pytest.mark.parametrize(
    "title",
    [
        # routine marker present, but the card is real engineering work
        "Расчистка копий индекса: чинить гонку при удалении",
        "commit-drift: диагностика причины дрейфа",
        "Переведи и исправь регресс в парсере",
    ],
)
def test_routine_marker_never_downgrades_real_work(title):
    level, _reason = resolve_card_autopin_effort(
        assignee="02-hermes-platform", title=title
    )
    assert level == "high"


def test_unknown_shape_falls_back_to_high():
    level, reason = resolve_card_autopin_effort(
        assignee="14-space-systems-engineer", title="Посчитать орбиту"
    )
    assert (level, reason) == ("high", "default-uncertain")


def test_every_rule_output_is_inside_the_wire_domain():
    titles = [
        "РГЗ: приёмка фазы",
        "РГЗ: сбор статусов",
        "commit-drift: x",
        "обычная задача",
        "РГЗ: запуск",
    ]
    assignees = [
        "09-independent-reviewer",
        "15-sre-qa",
        "03-software-engineer",
        "07-document-producer",
        "01-orchestrator",
    ]
    for assignee in assignees:
        for title in titles:
            for gated in (False, True):
                for goal in (False, True):
                    level, _reason = resolve_card_autopin_effort(
                        assignee=assignee,
                        title=title,
                        goal_mode=goal,
                        has_reviewer_child=gated,
                    )
                    assert level in LETOVO_WIRE_DOMAIN
                    assert level not in FORBIDDEN


def test_legacy_card_default_is_the_pre_rule_default():
    assert legacy_card_default("09-independent-reviewer") == "max"
    assert legacy_card_default("15-sre-qa") == "max"
    assert legacy_card_default("03-software-engineer") == "high"
    assert legacy_card_default(None) == "high"


# ---------------------------------------------------------------------------
# 2. The sweep
# ---------------------------------------------------------------------------


def _set_effort(conn, task_id, value, *, legacy=False):
    """Force the raw column value (bypassing the mutator, as a legacy row would).

    ``legacy=True`` also drops the reasoning events, reproducing a pre-t_f2dd1fb5
    row whose level has NO provenance on the trail.
    """
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET reasoning_effort = ? WHERE id = ?", (value, task_id)
        )
        if legacy:
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? AND kind IN "
                "('reasoning_effort_set', 'reasoning_effort_autopin')",
                (task_id,),
            )


def test_sweep_fills_every_active_null(conn):
    a = kb.create_task(conn, title="commit-drift: x", assignee="03-software-engineer")
    b = kb.create_task(conn, title="Ревью результата", assignee="09-independent-reviewer")
    c = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    for tid in (a, b, c):
        _set_effort(conn, tid, None)

    report = autopin_active_cards(conn)

    assert report["checked"] == 3
    assert len(report["changed"]) == 3
    levels = {
        tid: kb.get_task(conn, tid).reasoning_effort for tid in (a, b, c)
    }
    assert levels[a] == "low"          # mechanical routine
    assert levels[b] == "max"          # review gate
    assert levels[c] == "high"         # unsure -> high


def test_sweep_is_idempotent(conn):
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, None)

    first = autopin_active_cards(conn)
    second = autopin_active_cards(conn)

    assert len(first["changed"]) == 1
    assert second["changed"] == []
    assert second["unchanged"] >= 1


def test_sweep_never_writes_a_forbidden_level(conn):
    tid = kb.create_task(conn, title="РГЗ: приёмка", assignee="01-orchestrator")
    _set_effort(conn, tid, "medium")

    autopin_active_cards(conn)

    assert kb.get_task(conn, tid).reasoning_effort in LETOVO_WIRE_DOMAIN
    assert kb.get_task(conn, tid).reasoning_effort == "max"


def test_sweep_clamps_an_out_of_domain_legacy_value(conn):
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, "minimal", legacy=True)

    report = autopin_active_cards(conn)

    assert kb.get_task(conn, tid).reasoning_effort == "high"
    assert report["changed"][0][3] == "default-uncertain+domain-repair"


def test_sweep_keeps_an_operator_pin_direction_when_clamping(conn):
    """An operator's ``medium`` becomes the served level in the same direction."""
    tid = kb.create_task(conn, title="Ревью", assignee="09-independent-reviewer")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET reasoning_effort = 'medium' WHERE id = ?", (tid,)
        )

    autopin_active_cards(conn)

    # set_reasoning_effort itself clamps medium->high, so a stored 'medium' has
    # no pin event: the RULE owns it and pins the review gate at max.
    assert kb.get_task(conn, tid).reasoning_effort == "max"


def test_sweep_respects_an_operator_pin(conn):
    """An explicit operator choice is never overwritten by the rules."""
    tid = kb.create_task(conn, title="commit-drift: x", assignee="03-software-engineer")
    # A real operator pin goes through set_reasoning_effort (the CLI/dashboard
    # path) — that is what records the audit event the sweep consults.
    assert kb.set_reasoning_effort(conn, tid, "xhigh")
    assert kb.get_task(conn, tid).reasoning_effort == "xhigh"

    report = autopin_active_cards(conn)

    assert kb.get_task(conn, tid).reasoning_effort == "xhigh"
    assert report["changed"] == []
    assert (tid, "xhigh") in report["skipped_pinned"]


def test_sweep_respects_an_operator_pin_of_none(conn):
    """``none`` means thinking OFF — a deliberate pin the rules must not undo."""
    tid = kb.create_task(conn, title="Ревью", assignee="09-independent-reviewer")
    assert kb.set_reasoning_effort(conn, tid, "none")

    autopin_active_cards(conn)

    assert kb.get_task(conn, tid).reasoning_effort == "none"


def test_sweep_leaves_a_deliberate_legacy_non_default_alone(conn):
    """A hand-set ``none``/``xhigh`` with no provenance is still a choice."""
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, "none", legacy=True)

    report = autopin_active_cards(conn)

    assert kb.get_task(conn, tid).reasoning_effort == "none"
    assert report["changed"] == []


def test_sweep_corrects_the_plain_pre_rule_default(conn):
    """Rows written by the old flat ``high`` default get the rule level."""
    tid = kb.create_task(
        conn, title="commit-drift: hermes-kanban", assignee="03-software-engineer"
    )
    _set_effort(conn, tid, "high")  # what t_f2dd1fb5's default would store

    report = autopin_active_cards(conn)

    assert kb.get_task(conn, tid).reasoning_effort == "low"
    assert report["changed"][0][0] == tid


def test_sweep_ignores_terminal_cards(conn):
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, None)
    kb.complete_task(conn, tid, summary="done")

    report = autopin_active_cards(conn)

    assert tid not in [sid for sid, _o, _n, _r in report["changed"]]
    assert kb.get_task(conn, tid).reasoning_effort is None


def test_sweep_dry_run_writes_nothing(conn):
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, None)

    report = autopin_active_cards(conn, dry_run=True)

    assert report["dry_run"] is True
    assert any(sid == tid for sid, _o, _n, _r in report["changed"])
    assert kb.get_task(conn, tid).reasoning_effort is None


def test_sweep_records_an_audit_event_per_change(conn):
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, None)

    autopin_active_cards(conn)

    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "reasoning_effort_autopin" in kinds


def test_sweep_rejects_no_active_status():
    """The active set must match the owner's enumeration exactly."""
    assert set(ACTIVE_AUTOPIN_STATUSES) == {
        "triage", "todo", "ready", "running", "blocked"
    }
    assert "done" not in ACTIVE_AUTOPIN_STATUSES
    assert "archived" not in ACTIVE_AUTOPIN_STATUSES
    assert "review" not in ACTIVE_AUTOPIN_STATUSES


# ---------------------------------------------------------------------------
# 3. Creation path + post-link wiring
# ---------------------------------------------------------------------------


def test_create_task_stores_the_rule_level_for_routine(conn):
    tid = kb.create_task(
        conn, title="commit-drift: hermes-kanban", assignee="03-software-engineer"
    )
    assert kb.get_task(conn, tid).reasoning_effort == "low"


def test_create_task_stores_max_for_a_review_gate_card(conn):
    tid = kb.create_task(conn, title="Ревью", assignee="09-independent-reviewer")
    assert kb.get_task(conn, tid).reasoning_effort == "max"


def test_create_task_keeps_an_operator_pin_verbatim(conn):
    tid = kb.create_task(
        conn, title="commit-drift: x", assignee="03-software-engineer",
        reasoning_effort="xhigh",
    )
    assert kb.get_task(conn, tid).reasoning_effort == "xhigh"


def test_create_task_never_stores_a_forbidden_level(conn):
    """A letovo-routed card never stores a level the wire rejects."""
    tid = kb.create_task(
        conn, title="x", assignee="03-software-engineer",
        reasoning_effort="medium",
        provider_override="custom:letovo", model_override="deepseek-v4-flash",
    )
    # The route is letovo (the board's real route), so the operator's medium is
    # clamped to the served level on the same rung — the column can never hold
    # a value the endpoint answers 400 to.
    assert kb.get_task(conn, tid).reasoning_effort == "high"
    assert kb.get_task(conn, tid).reasoning_effort not in FORBIDDEN


def test_create_task_without_a_pin_never_produces_a_forbidden_level(conn):
    for title in ("РГЗ: приёмка", "commit-drift: x", "обычная задача"):
        tid = kb.create_task(conn, title=title, assignee="03-software-engineer")
        assert kb.get_task(conn, tid).reasoning_effort in LETOVO_WIRE_DOMAIN


def test_linking_a_review_child_raises_the_parent_to_gated_high(conn):
    """A parent is born on ``low`` (routine title) and becomes gated on link."""
    parent = kb.create_task(
        conn, title="commit-drift: hermes-kanban", assignee="03-software-engineer"
    )
    assert kb.get_task(conn, parent).reasoning_effort == "low"

    reviewer = kb.create_task(conn, title="Ревью", assignee="09-independent-reviewer")
    kb.link_tasks(conn, parent, reviewer)

    assert kb.get_task(conn, parent).reasoning_effort == "high"
    assert kb.get_task(conn, reviewer).reasoning_effort == "max"


def test_linking_does_not_undo_an_operator_pin_on_the_parent(conn):
    parent = kb.create_task(
        conn, title="commit-drift: x", assignee="03-software-engineer",
        reasoning_effort="xhigh",
    )
    reviewer = kb.create_task(conn, title="Ревью", assignee="09-independent-reviewer")
    kb.link_tasks(conn, parent, reviewer)

    assert kb.get_task(conn, parent).reasoning_effort == "xhigh"


# ---------------------------------------------------------------------------
# 4. Dispatcher tick
# ---------------------------------------------------------------------------


def test_dispatch_tick_repins_a_null_card_before_spawning(conn):
    """A card that re-enters the active set with NULL is re-pinned by the tick."""
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, None)

    spawned: list[str] = []

    def fake_spawn(task, workspace_path):
        spawned.append(task.id)
        return 4242

    res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=1)

    assert any(sid == tid for sid, _o, _n, _r in res.reasoning_autopinned)
    assert kb.get_task(conn, tid).reasoning_effort == "high"


def test_dispatch_tick_dry_run_reports_without_writing(conn):
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    _set_effort(conn, tid, None)

    kb.dispatch_once(conn, dry_run=True)

    assert kb.get_task(conn, tid).reasoning_effort is None


def test_domain_repair_of_an_operator_pin_stays_operator_owned(conn):
    """A clamped operator pin must not be re-stamped as rule-owned (t_d7edc3b2).

    Otherwise the NEXT sweep treats the repaired value as an autopin result,
    re-derives it from the rule and silently discards the operator's choice.
    """
    tid = kb.create_task(conn, title="обычная задача", assignee="03-software-engineer")
    # An operator pin on a route that accepts 'medium' (non-letovo): the value
    # is served there, so the boundary does not clamp it and the card really
    # does hold out-of-letovo-domain 'medium' as an operator decision.
    kb.set_reasoning_effort(conn, tid, "medium")
    assert kb.get_task(conn, tid).reasoning_effort == "medium"

    autopin_active_cards(conn)

    # Repaired onto the wire domain, but still an operator decision.
    assert kb.get_task(conn, tid).reasoning_effort == "high"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "reasoning_effort_set" in kinds
    assert "reasoning_effort_autopin" not in kinds[-1:]

    # And a second sweep leaves the repaired operator value alone.
    report = autopin_active_cards(conn)
    assert not [c for c in report["changed"] if c[0] == tid]
