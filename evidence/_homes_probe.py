import os, tempfile
from pathlib import Path
root = Path(tempfile.mkdtemp(prefix="probe-homes."))
os.environ["HERMES_HOME"] = str(root / "hermes")
os.environ["HERMES_KANBAN_HOME"] = str(root / "hermes")
from hermes_cli.kanban_db import kanban_home
from hermes_constants import get_default_hermes_root, get_hermes_home
print("root      ", get_default_hermes_root())
print("hermes_hom", get_hermes_home())
print("kanban    ", kanban_home())
