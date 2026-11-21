#!/usr/bin/env python3
"""Live probe: kanban_create's card-sizing warning against the REAL profile configs.

Runs the production path ``tools.kanban_tools._card_sizing_warning`` — the same call
``_handle_create`` makes — so the numbers below come from each profile's actual
``agent.max_turns``, not from a fixture.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACE",
             "HERMES_KANBAN_WORKSPACES_ROOT"):
    os.environ.pop(_key, None)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="probe-sizing-home-")

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt
from agent.kanban_card_sizing import assignee_iteration_budget

db_path = Path(os.environ["HERMES_HOME"]) / "kanban.db"
kb.init_db(db_path)
os.environ["HERMES_KANBAN_DB"] = str(db_path)

BIG = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(9))
SMALL = "## Приёмка\n- один критерий\n- второй критерий\n"

print("Assignee budgets as the rule reads them (real profile configs):")
for name in ("03-software-engineer", "05-rag-knowledge-engineer", "08-visual-media-designer",
             "10-skill-librarian", "15-sre-qa", "01-orchestrator"):
    print(f"  {name:26s} agent.max_turns = {assignee_iteration_budget(name)}")

print("\nkanban_create sizing path (9 acceptance items = 'big' card):")
for assignee in ("03-software-engineer", "08-visual-media-designer"):
    print(f"  {assignee}:")
    print(f"    {kt._card_sizing_warning(assignee, BIG)}")

print("\nSame call for a card that fits (2 acceptance items):")
for assignee in ("03-software-engineer", "08-visual-media-designer"):
    print(f"  {assignee}: {kt._card_sizing_warning(assignee, SMALL)!r}")

print("\nA card with no acceptance heading cannot be sized — the rule stays silent:")
print(f"  {kt._card_sizing_warning('03-software-engineer', 'Just make it work.')!r}")

print("\nA card whose body is not a string (defensive path in _handle_create):")
print(f"  {kt._card_sizing_warning('03-software-engineer', None)!r}")
