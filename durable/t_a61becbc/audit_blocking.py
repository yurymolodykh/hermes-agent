"""AST audit: blocking calls that still run ON the event loop.

A `def` handler is fine (Starlette runs it in a worker thread). A blocking call
inside `async def` blocks the loop unless it sits in code that is offloaded:
either directly (`asyncio.to_thread(...)`, `run_in_threadpool(...)`) or inside a
closure/helper that the handler passes to one of those by name
(`_run()` defined in the handler and awaited as `scoped_to_thread(profile, _run)`).

Usage: python3 audit_blocking.py [dir ...]
"""
from __future__ import annotations

import ast
import pathlib
import sys

BLOCKING_ATTRS = {
    "read_text", "write_text", "read_bytes", "write_bytes", "mkdir", "unlink", "rmdir",
    "replace", "rename", "listdir", "walk", "scandir", "glob", "rglob", "iterdir",
    "exists", "is_dir", "is_file", "stat", "lstat", "copyfile", "copytree", "rmtree",
    "communicate", "readline", "readlines", "fsync",
    "check_output", "check_call", "Popen", "urlopen",
}
# Blocking sinks. `fetchone`/`execute` are SQLite; `subprocess.run` is a shell spawn.
BLOCKING_NAMES = {"open", "input", "sleep", "urlopen"}
OFFLOAD_FUNCS = {
    "to_thread", "run_in_threadpool", "run_blocking", "run_sync", "_read_off_loop",
    "scoped_to_thread", "config_scoped_to_thread", "run_in_executor", "submit",
}


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _offload_target_names(tree: ast.AST) -> set[str]:
    """Names of functions/closures handed to an offload helper."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node.func) in OFFLOAD_FUNCS:
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Name):
                    names.add(arg.id)
                elif isinstance(arg, ast.Attribute):
                    names.add(arg.attr)
    return names


def _nodes_inside_offload(tree: ast.AST) -> set[int]:
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node.func) in OFFLOAD_FUNCS:
            for arg in node.args:
                for sub in ast.walk(arg):
                    inside.add(id(sub))
    return inside


def audit(paths: list[pathlib.Path]) -> list[tuple[str, str, int, str]]:
    rows = []
    for path in paths:
        tree = ast.parse(path.read_text())
        offloaded = _nodes_inside_offload(tree)
        by_name = _offload_target_names(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            # Local closures that are handed to an offload helper are off-loop.
            for inner in ast.walk(fn):
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner is not fn \
                        and inner.name in by_name:
                    for sub in ast.walk(inner):
                        offloaded.add(id(sub))
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call) or id(node) in offloaded:
                    continue
                name = _callee_name(node.func)
                if name in BLOCKING_ATTRS or name in BLOCKING_NAMES:
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
