"""Live probe: `hermes kanban create` (the CLI hole) sizes the card on landing ready.

Run from the task worktree with a throwaway HERMES_KANBAN_HOME so the operator's
real board is never touched.  Creates two cards through the real CLI entry point
(`hermes_cli.main:main`) for assignee 03-software-engineer (budget 180):

  1. ten acceptance items  -> over budget (40 + 30*10 = 340) -> advisory expected
  2. one acceptance item   -> fits                           -> silence expected

then prints `hermes kanban show <id>` for both.
"""

from __future__ import annotations

import os
import sys
import tempfile

# Running this file puts *its own directory* first on sys.path, which would import the
# venv's installed hermes-agent instead of this worktree's code. Pin the checkout.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

kanban_home = tempfile.mkdtemp(prefix="probe-t97c86dea-")
os.environ["HERMES_KANBAN_HOME"] = kanban_home
os.environ.pop("HERMES_KANBAN_DB", None)
os.environ.pop("HERMES_KANBAN_BOARD", None)

from hermes_cli.main import main as cli_main  # noqa: E402

BIG = "## Приёмка\n" + "".join(f"- критерий {i}\n" for i in range(1, 11))
SMALL = "## Приёмка\n- один критерий\n"


def run(argv: list[str]) -> int:
    # ``main()`` reads ``sys.argv``; swap it in for the duration of the call.
    saved = sys.argv
    sys.argv = ["hermes", *argv]
    try:
        cli_main()
    except SystemExit as exc:  # the CLI exits 0 on success
        return int(exc.code or 0)
    finally:
        sys.argv = saved
    return 0


def create(title: str, body: str) -> str:
    import io
    from contextlib import redirect_stdout

    body_path = os.path.join(kanban_home, "body.md")
    with open(body_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    buf = io.StringIO()
    with redirect_stdout(buf):
        run([
            "kanban", "create", title,
            "--assignee", "03-software-engineer",
            "--body-file", body_path,
        ])
    out = buf.getvalue()
    sys.stdout.write(out)
    for token in out.split():
        if token.startswith("t_"):
            return token
    raise SystemExit("create printed no task id")


print(f"== probe: HERMES_KANBAN_HOME={kanban_home}")
print("== 1. over-budget card (10 acceptance items, budget 180, estimate 340)")
big_id = create("ЖИВАЯ ПРОБА: размер карточки на промоушене", BIG)
print("== 2. card that fits (1 acceptance item, estimate 70)")
small_id = create("ЖИВАЯ ПРОБА 2: маленькая карточка без предупреждения", SMALL)

for label, tid in (("1", big_id), ("2", small_id)):
    print(f"\n=== {label}. hermes kanban show {tid}")
    run(["kanban", "show", tid])
