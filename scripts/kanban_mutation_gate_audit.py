"""Count mutating kanban dashboard routes that lack an ownership gate in their body.

A route counts as GATED when the handler (or a helper it calls, one level deep, inside the same
module) references ``_require_held_task`` / ``_require_owned_task``... — deliberately only the
WRITE gate (``_require_held_task``) counts for mutating routes whose path carries ``{task_id}``.

Usage: python scripts/kanban_mutation_gate_audit.py [path-to-plugin_api.py]
Exit code 1 when any ungated mutating task route remains.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

WRITE_METHODS = {"post", "put", "patch", "delete"}
WRITE_GATE = "_require_held_task"


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
    return out


def audit(path: Path) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    gated, ungated = [], []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            method = dec.func.attr
            if method not in WRITE_METHODS:
                continue
            route = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else ""
            if "{task_id}" not in route:
                continue
            names = _called_names(node)
            # one level of indirection: a helper defined in this module
            for name in list(names):
                helper = funcs.get(name)
                if helper is not None:
                    names |= _called_names(helper)
            entry = (method.upper(), route, node.name)
            (gated if WRITE_GATE in names else ungated).append(entry)
    return sorted(gated), sorted(ungated)


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("plugins/kanban/dashboard/plugin_api.py")
    gated, ungated = audit(target)
    print(f"file: {target}")
    for method, route, fn in gated:
        print(f"  GATED    {method:6} {route:45} {fn}")
    for method, route, fn in ungated:
        print(f"  UNGATED  {method:6} {route:45} {fn}")
    print(f"\nmutating {sorted(WRITE_METHODS)} routes with {{task_id}}: {len(gated) + len(ungated)}")
    print(f"without {WRITE_GATE}: {len(ungated)}")
    return 1 if ungated else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
