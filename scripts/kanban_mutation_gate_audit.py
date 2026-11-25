"""Audit which mutating kanban-dashboard routes actually enforce a boundary.

Acceptance measures for card ``t_ba1321f4``: "мутирующих маршрутов с ``task_id`` без гейта = 0",
plus no installation-wide route left open.

Three gates exist, and the difference is the policy:

* ``_require_held_task`` — the CARD boundary (creator/assignee/host owner may change it; everybody
  may read it). Every route that changes a card, its materials, its run or its place in the graph.
* ``_require_owned_task`` — the READ boundary (any verified caller: the team's own material). Only
  routes in :data:`READ_GATED_BY_DESIGN` may use it, each with the reason it is not a mutation.
* ``_require_owner`` — the INSTALLATION boundary (boards, profile metadata, orchestration knobs).

A route is reported UNGATED when it calls none of them. Exit code is 1 while anything is ungated,
while a declared read-gated route no longer calls the read gate, or while a declared open route
gained a gate (a stale excuse in the other direction: the exemption list must not rot either).

Usage: python3 scripts/kanban_mutation_gate_audit.py [plugin_api.py]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

WRITE_METHODS = {"post", "put", "patch", "delete"}
WRITE_GATE = "_require_held_task"
READ_GATE = "_require_owned_task"
OWNER_GATE = "_require_owner"

#: ``(method, route)`` → why this mutating verb does not change a card.
READ_GATED_BY_DESIGN: dict[tuple[str, str], str] = {
    ("POST", "/tasks/{task_id}/comments"):
        "a comment is team material: any verified caller may add one and the author recorded is the "
        "real person (attributable thread). No card field changes.",
    ("POST", "/tasks/{task_id}/estimate"):
        "reads title/body to ask the aux model for a token estimate; nothing on the card changes.",
}

#: ``(method, route)`` → why creating here is open to every verified caller.
OPEN_BY_DESIGN: dict[tuple[str, str], str] = {
    ("POST", "/tasks"):
        "creating a card is work, not somebody else's: the row records the caller as created_by, "
        "so the new card belongs to whoever made it.",
}


def _called_names(node: ast.AST) -> tuple[set[str], set[str]]:
    """``(bare, attributes)`` — keep them apart.

    A bare ``_require_held_task(...)`` is this module's gate. An ATTRIBUTE call of the same name
    (``kanban_db.get_task``) is somebody else's function, and treating the two alike both hides real
    holes and invents fake ones: ``create_task`` calls ``kanban_db.get_task``, and the module also
    defines a ``get_task`` ROUTE that reads through the read gate — one naively-merged name set
    claimed the create route was gated.
    """
    bare: set[str] = set()
    attrs: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Name):
                bare.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                attrs.add(fn.attr)
    return bare, attrs


def audit(path: Path) -> dict[str, list[tuple[str, str, str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out: dict[str, list[tuple[str, str, str]]] = {
        "write": [], "read": [], "owner": [], "open": [], "ungated": [], "stale": [],
    }
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
            if not isinstance(route, str):
                continue
            names, _attrs = _called_names(node)
            for name in sorted(names):  # one level of indirection: a bare same-module helper call
                helper = funcs.get(name)
                if helper is not None:
                    names |= _called_names(helper)[0]
            entry = (dec.func.attr.upper(), route, node.name)
            key = (entry[0], route)
            if WRITE_GATE in names:
                out["write"].append(entry)      # card boundary, incl. run→task and graph edges
            elif OWNER_GATE in names:
                out["owner"].append(entry)      # installation boundary
            elif key in READ_GATED_BY_DESIGN and READ_GATE in names:
                out["read"].append(entry)
            elif key in OPEN_BY_DESIGN and not (READ_GATE in names or OWNER_GATE in names):
                out["open"].append(entry)
            elif key in READ_GATED_BY_DESIGN or key in OPEN_BY_DESIGN:
                out["stale"].append(entry)      # declared exempt, but the declared state changed
            else:
                out["ungated"].append(entry)
    for key in out:
        out[key].sort()
    return out


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("plugins/kanban/dashboard/plugin_api.py")
    result = audit(target)
    print(f"file: {target}\n")
    for method, route, fn in result["write"]:
        print(f"  CARD-GATE   {method:6} {route:45} {fn}")
    for method, route, fn in result["owner"]:
        print(f"  OWNER-GATE  {method:6} {route:45} {fn}")
    for method, route, fn in result["read"]:
        print(f"  READ-GATE   {method:6} {route:45} {fn}\n"
              f"              ↳ {READ_GATED_BY_DESIGN[(method, route)]}")
    for method, route, fn in result["open"]:
        print(f"  OPEN        {method:6} {route:45} {fn}\n"
              f"              ↳ {OPEN_BY_DESIGN[(method, route)]}")
    for method, route, fn in result["ungated"]:
        print(f"  UNGATED     {method:6} {route:45} {fn}")
    for method, route, fn in result["stale"]:
        print(f"  STALE       {method:6} {route:45} {fn}  (declared exempt, declared gate missing)")

    task_routes = [e for key in ("write", "read") for e in result[key]]
    print(f"\nmutating routes total: {sum(len(v) for v in result.values())}")
    print(f"  card-gated ({WRITE_GATE}): {len(result['write'])}")
    print(f"  owner-gated ({OWNER_GATE}): {len(result['owner'])}")
    print(f"  read-gated by design:     {len(result['read'])} (declared: {len(READ_GATED_BY_DESIGN)})")
    print(f"  open by design:           {len(result['open'])} (declared: {len(OPEN_BY_DESIGN)})")
    print(f"  ungated:                  {len(result['ungated'])}")
    print(f"  stale excuse:             {len(result['stale'])}")
    print(f"\nmutating routes with {{task_id}}: {len(task_routes)}")
    print(f"  of which ungated:            {len([e for e in result['ungated'] if '{task_id}' in e[1]])}")
    return 1 if (result["ungated"] or result["stale"]) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
