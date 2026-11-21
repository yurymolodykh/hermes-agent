import sys, os, json
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sys as _s; from pathlib import Path
import tempfile
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="pcw-")
for k in ("HERMES_KANBAN_DB","HERMES_KANBAN_BOARD","HERMES_KANBAN_WORKSPACE"):
    os.environ.pop(k, None)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
db = Path(os.environ["HERMES_HOME"])/ "kanban.db"
kb.init_db(db); os.environ["HERMES_KANBAN_DB"] = str(db)
from tools import kanban_tools as kt

big = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(9))
small = "## Приёмка\n- один\n- два\n"
for label, body, assignee in (("big on 03 (300)", big, "03-software-engineer"),
                              ("small on 03 (300)", small, "03-software-engineer"),
                              ("big on 08 (200)", big, "08-visual-media-designer")):
    out = kt._card_sizing_warning(assignee, body)
    print(f"{label}: -> {out}")
    print()
