"""Audit which mutating kanban-dashboard routes actually enforce the card boundary.

Acceptance measure for card ``t_ba1321f4``: "мутирующих маршрутов с ``task_id`` без гейта = 0".

Two gates exist, and the difference is the policy, not a detail:

* ``_require_held_task`` — the WRITE boundary (creator/assignee/admin). Every route that changes a
  card, its materials, its run or its place in the graph must call it.
* ``_require_owned_task`` — the READ boundary (any verified caller, team material). Only routes
  listed in :data:`READ_GATED_BY_DESIGN` may use it, each with the reason it is not a mutation.

A route is reported UNGATED when it calls neither. Exit code is 1 while any UNGATED route remains
or a route in the read-gated list no longer calls the read gate (so the exception cannot rot into
a hole).

Usage: python3 scripts/kanban_mutation_gate_audit.py [plugin_api.py]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

WRITE_METHODS = {"post", "put", "patch", "delete"}
WRITE_GATE = "_require_held_task"
READ_GATE = "_require_owned_task"

#: ``(method, route)`` → why this mutating verb is not a mutation of the card.
READ_GATED_BY_DESIGN: dict[tuple[str, str], str] = {
    ("POST", "/tasks/{task_id}/comments"):
        "a comment is team material: any verified caller may add one, and the author recorded is "
        "the real person (attributable thread). No card field changes.",
    ("POST", "/tasks/{task_id}/estimate"):
        "reads title/body to ask the aux model for a token estimate; nothing on the card changes.",
}


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


def audit(path: Path) -> dict[str, list[tuple[str, str, str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out: dict[str, list[tuple[str, str, str]]] = {"write": [], "read": [], "ungated": [], "stale": []}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if dec.func.attr not in WRITE_METHODS:
                continue
            const = dec.args[0] if dec.args else None
            route = const.value if isinstance(const, ast.Constant) else ""
            if not isinstance(route, str) or "{task_id}" not in route:
                continue
            names = _called_names(node)
            for name in list(names):  # one level of indirection: a module-level helper
                helper = funcs.get(name)
                if helper is not None:
                    names |= _called_names(helper)
            entry = (dec.func.attr.upper(), route, node.name)
            key = (entry[0], route)
            if WRITE_GATE in names:
                out["write"].append(entry)
            elif key in READ_GATED_BY_DESIGN and READ_GATE in names:
                out["read"].append(entry)
            elif key in READ_GATED_BY_DESIGN:
                out["stale"].append(entry)  # declared read-gated but no longer calls the read gate
            else:
                out["ungated"].append(entry)
    for key in ("write", "read", "ungated", "stale"):
        out[key].sort()
    return out


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("plugins/kanban/dashboard/plugin_api.py")
    result = audit(target)
    print(f"file: {target}\n")
    for method, route, fn in result["write"]:
        print(f"  WRITE-GATED  {method:6} {route:45} {fn}")
    for method, route, fn in result["read"]:
        print(f"  READ-GATED   {method:6} {route:45} {fn}\n"
              f"               ↳ {READ_GATED_BY_DESIGN[(method, route)]}")
    for method, route, fn in result["ungated"]:
        print(f"  UNGATED      {method:6} {route:45} {fn}")
    for method, route, fn in result["stale"]:
        print(f"  STALE-EXCUSE {method:6} {route:45} {fn}  (declared read-gated, gate missing)")

    total = sum(len(v) for v in result.values())
    print(f"\nmutating routes with {{task_id}}: {total}")
    print(f"  write-gated:  {len(result['write'])}")
    print(f"  read-gated:   {len(result['read'])} (declared: {len(READ_GATED_BY_DESIGN)})")
    print(f"  ungated:      {len(result['ungated'])}")
    print(f"  stale excuse: {len(result['stale'])}")
    return 1 if (result["ungated"] or result["stale"]) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
