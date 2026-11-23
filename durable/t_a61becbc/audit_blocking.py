"""AST audit: blocking calls inside `async def` handlers without offload.

A `def` handler is fine (Starlette runs it in a worker thread). A blocking call
inside `async def` that is not already inside `asyncio.to_thread(...)` /
`run_in_threadpool(...)` / `await run_blocking(...)` blocks the event loop.

Usage: python3 durable/t_a61becbc/audit_blocking.py [dir ...]
"""
from __future__ import annotations

import ast
import pathlib
import sys

BLOCKING_ATTRS = {
    "read_text", "write_text", "read_bytes", "write_bytes", "mkdir", "unlink", "rmdir",
    "replace", "rename", "listdir", "walk", "scandir", "glob", "rglob", "iterdir",
    "exists", "is_dir", "is_file", "stat", "lstat", "copyfile", "copytree", "rmtree",
    "wait", "communicate", "readline", "readlines", "fileno", "flush", "fsync",
    "connect", "recv", "sendall", "sleep", "run", "call", "check_output", "Popen",
    "Executable", "execute", "fetchall", "fetchone", "execute", "commit", "begin",
}
OFFLOAD_FUNCS = {"to_thread", "run_in_threadpool", "run_blocking", "run_sync", "_read_off_loop"}


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _offloaded_node_ids(tree: ast.AST) -> set[int]:
    """Ids of every node sitting inside an offload call's arguments."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node.func) in OFFLOAD_FUNCS:
            for arg in node.args:
                for sub in ast.walk(arg):
                    inside.add(id(sub))
            for kw in node.keywords:
                for sub in ast.walk(kw.value):
                    inside.add(id(sub))
    return inside


def audit(paths: list[pathlib.Path]) -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text())
        offloaded = _offloaded_node_ids(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                if id(node) in offloaded:
                    continue
                name = _callee_name(node.func)
                if name not in BLOCKING_ATTRS:
                    continue
                rows.append((str(path), fn.name, node.lineno, name))
    return rows


def main(argv: list[str]) -> int:
    targets = argv[1:] or ["hermes_cli/web_routers"]
    paths: list[pathlib.Path] = []
    for target in targets:
        p = pathlib.Path(target)
        paths.extend(sorted(p.glob("*.py")) if p.is_dir() else [p])
    rows = audit(paths)
    for path, fn, line, name in rows:
        print(f"{path}:{line} {fn} -> {name}")
    print(f"total={len(rows)} files={len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
