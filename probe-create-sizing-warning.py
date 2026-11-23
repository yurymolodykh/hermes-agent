#!/usr/bin/env python3
"""Live probe: kanban_create's card-sizing warning against the REAL profile configs.

Runs the production path ``tools.kanban_tools._handle_create`` — the handler the
``kanban_create`` tool is — against a throwaway board, so the numbers below come from
each profile's actual ``agent.max_turns``, from ``create_task`` writing the wall the
card did not name, and from the real answer shape. Every card the probe makes is
archived immediately afterwards.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACE",
             "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_TASK",
             # The dispatcher marks its worker as a fenced Kanban descendant; this probe
             # owns the throwaway board it creates, so the fence must not apply to it.
             "HERMES_DELEGATED_CHILD_CONTEXT"):
    os.environ.pop(_key, None)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="probe-sizing-home-")

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from tools import kanban_tools as kt
from agent.kanban_card_sizing import assignee_iteration_budget

db_path = Path(os.environ["HERMES_HOME"]) / "kanban.db"
kb.init_db(db_path)
os.environ["HERMES_KANBAN_DB"] = str(db_path)

BIG = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(9))
FOUR = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(4))
SMALL = "## Приёмка\n- один критерий\n- второй критерий\n"
ZERO = "## Приёмка\n"


def budget_table() -> None:
    print("Assignee budgets as the rule reads them (real profile configs):")
    for name in ("03-software-engineer", "05-rag-knowledge-engineer",
                 "08-visual-media-designer", "10-skill-librarian", "15-sre-qa",
                 "01-orchestrator"):
        print(f"  {name:26s} agent.max_turns = {assignee_iteration_budget(name)}")


def make(title: str, body: str, assignee: str = "03-software-engineer",
         **extra) -> dict:
    card = json.loads(kt._handle_create({
        "title": title, "assignee": assignee, "body": body,
        "workspace_kind": "scratch", **extra,
    }))
    if card.get("ok"):
        with connect_closing(db_path) as conn:
            kb.archive_task(conn, card["task_id"])
    return card


def show(label: str, card: dict) -> None:
    with connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (card["task_id"],),
        ).fetchone()
    print(f"\n--- {label} ---")
    print("  ok                   :", card.get("ok"), "| task_id:", card.get("task_id"))
    print("  estimated_minutes    :", card.get("estimated_minutes"))
    print("  max_runtime_seconds  :", card.get("max_runtime_seconds"))
    print("  sizing_warning       :", card.get("sizing_warning") or "None")
    print("  row status after     :", row["status"] if row else "GONE")
    assert card.get("ok"), card
    assert row is not None and row["status"] == "archived", ("not archived", dict(row) if row else None)


budget_table()

show("(а) 4 критерия, предел карточки не задан (create_task пишет 3600)",
     make("probe: four criteria, default wall", FOUR))
show("(б) 0 критериев, предел не задан",
     make("probe: zero criteria, default wall", ZERO))
show("(в) 4 критерия, явный предел 1800 с",
     make("probe: four criteria, 1800 s", FOUR, max_runtime_seconds=1800))
show("4 критерия, широкий предел 14400 с (предупреждения быть не должно)",
     make("probe: four criteria, wide wall", FOUR, max_runtime_seconds=14400))
show("9 критериев, профиль 08-visual-media-designer (обе оси)",
     make("probe: nine criteria", BIG, assignee="08-visual-media-designer"))
show("2 критерия (интерполяция 61→81 = 74 мин, поэтому ось минут честно срабатывает)",
     make("probe: two criteria", SMALL))
print("\nA card with no acceptance heading cannot be sized — the rule stays silent:")
print(f"  {kt._card_sizing_warning('03-software-engineer', 'Just make it work.')!r}")
print("\nA card whose body is not a string (defensive path in _handle_create):")
print(f"  {kt._card_sizing_warning('03-software-engineer', None)!r}")
