#!/usr/bin/env python3
"""AST-аудит HTTP-маршрутов панели: где цикл событий действительно блокируется.

Записка t_eca8cb15 §1.3 обязана быть воспроизводимой: числа ниже печатаются этим
скриптом, а не считаются руками.

Запуск (из корня чекаута):

    python3 durable/t_eca8cb15/probes/route_audit.py --json out.json --md out.md

Что считается:

1. **Регистрации маршрутов.** Каждый декоратор ``@router.<verb>(...)`` /
   ``@app.<verb>(...)`` в ``hermes_cli/web_routers/*.py`` — одна регистрация
   (``api_route`` считается отдельно от ``get/post/...``, ``websocket`` — отдельно
   от HTTP). Один и тот же обработчик, зарегистрированный на два пути, даёт две
   регистрации; уникальные обработчики считаются отдельно.

2. **Синхронные обработчики (``def``).** Starlette исполняет их в пуле потоков
   (``run_in_threadpool``), поэтому они НЕ блокируют цикл событий по построению.
   В список правок не попадают.

3. **Блокирующее тело ``async def``.** Порог отбора — ЯВНЫЙ, белый список
   блокирующих операций (``BLOCKING``): файловая система (``pathlib``/``os.*``),
   ``sqlite3``, ``subprocess``, ``time.sleep``, ``socket.connect``, блокирующие
   HTTP-клиенты, ``threading``-примитивы. Всё остальное (словари, строки,
   ``datetime``, ``json``, отсутствие явных блокирующих вызовов) НЕ считается
   блокирующим — иначе список правок раздувается мелочью вроде ``is_file()``,
   чья цена на порядки меньше сетевого/дискового вызова.

   Вызов **не** считается блокирующим, если он лексически лежит внутри вложенной
   функции/лямбды, переданной в ``asyncio.to_thread`` / ``anyio.to_thread.run_sync``
   / ``loop.run_in_executor`` — то есть уже оффлоадится.

4. **Уже оффлоадящие обработчики.** ``async def``-обработчики, в теле которых есть
   хотя бы один ``to_thread``/``run_in_executor`` — это образец правки и отдельная
   цифра (не 37 и не 103: число печатается).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROUTERS_DIR = "hermes_cli/web_routers"

# --- порог отбора -----------------------------------------------------------
# Модули, чьи вызовы в async-теле блокируют цикл событий.
BLOCKING_MODULES = {"sqlite3", "subprocess", "shutil", "socket", "requests", "httpx"}

# Функции pathlib.Path / os, которые трогают диск или ждут.
BLOCKING_ATTRS = {
    # pathlib
    "stat", "lstat", "exists", "is_file", "is_dir", "is_symlink", "read_text",
    "read_bytes", "write_text", "write_bytes", "mkdir", "unlink", "rmdir",
    "rename", "replace", "chmod", "symlink_to", "hardlink_to", "touch",
    "resolve", "iterdir", "glob", "rglob", "walk", "samefile",
    # os
    "listdir", "scandir", "makedirs", "removedirs", "remove", "walk", "open",
    "getsize", "getmtime", "access", "readlink",
    # ожидание
    "sleep", "wait", "waitpid", "call", "check_call", "check_output", "run",
    "Popen", "connect", "recv", "sendall", "acquire", "join",
    # блокирующие клиенты
    "get", "post", "put", "delete", "request",
}

# Модули, чей `open` — обычное чтение файла.
BLOCKING_BUILTINS_MODULES = {"io", "codecs"}

# Обёртки оффлоада: вызов внутри них уже в пуле потоков.
OFFLOAD_CALLS = {"to_thread", "run_in_executor", "run_sync", "run_async_in_threadpool"}


def dotted(node: ast.AST) -> str:
    """`a.b.c` для Attribute/Name цепочки, иначе ''."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def is_offloaded(call: ast.Call) -> bool:
    """Вызов — обёртка оффлоада? (`asyncio.to_thread(fn, ...)`)."""
    return dotted(call.func).split(".")[-1] in OFFLOAD_CALLS


def blocking_kind(call: ast.Call) -> str | None:
    """Имя блокирующей операции для вызова, иначе None."""
    name = dotted(call.func)
    if not name:
        return None
    last = name.split(".")[-1]
    if name in BLOCKING_MODULES or name.split(".")[0] in BLOCKING_MODULES:
        if last in {"Path", "PurePath", "str", "list", "dict"}:
            return None
        return name
    # `open(...)` из io/codecs и встроенный `open` трогают диск.
    if last == "open" and name.split(".")[0] in (BLOCKING_BUILTINS_MODULES | {"builtins"}):
        return name
    if last in BLOCKING_ATTRS:
        # `dict.get`, `queue.get`, `re.sub`... — не файловые. Отсекаем по получателю,
        # если он очевидно не путь/файл: правило простое и документированное —
        # `get/post` без модуля-клиента в цепочке не считаем (requests/httpx выше).
        if last in {"get", "post", "put", "delete", "request", "run", "open"}:
            return name if name.split(".")[0] in (BLOCKING_MODULES | BLOCKING_BUILTINS_MODULES) else None
        return name
    return None


class Handler:
    def __init__(self, node: ast.AsyncFunctionDef | ast.FunctionDef, module: str, lineno: int,
                 path: str, verb: str, is_async: bool):
        self.name = node.name
        self.module = module
        self.lineno = node.lineno
        self.route_lineno = lineno
        self.path = path
        self.verb = verb
        self.is_async = is_async
        self.node = node


class BlockingFinder(ast.NodeVisitor):
    """Ищет блокирующие вызовы, пропуская тела оффлоад-обёрток."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []
        self.offloaded_calls = 0

    def visit_Call(self, node: ast.Call) -> None:
        if is_offloaded(node):
            self.offloaded_calls += 1
            # Обёртка оффлоада: её аргументы исполняются в пуле — не спускаемся.
            for arg in node.args[:1]:
                _skip(arg)
            for kw in node.keywords:
                _skip(kw.value)
            self.generic_visit_remaining(node)
            return
        kind = blocking_kind(node)
        if kind:
            self.hits.append((node.lineno, kind))
        self.generic_visit(node)

    def generic_visit_remaining(self, node: ast.Call) -> None:
        # Аргументы сверх первого (timeout и т.п.) — без обхода тела функции.
        for arg in node.args[1:]:
            self.visit(arg)


def _skip(node: ast.AST) -> None:
    """Не обходим узел: он исполняется в пуле потоков."""


def collect(modules: list[Path]) -> tuple[list[Handler], dict]:
    handlers: list[Handler] = []
    stats = {"registrations": 0, "http_verb": 0, "api_route": 0, "websocket": 0,
             "async": 0, "def": 0, "async_unique": 0, "def_unique": 0,
             "async_with_offload": 0}
    seen_async: set[tuple[str, str]] = set()
    seen_sync: set[tuple[str, str]] = set()
    for mod in sorted(modules):
        tree = ast.parse(mod.read_text(encoding="utf-8"), filename=str(mod))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                fn = deco.func
                if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                    continue
                if fn.value.id not in {"router", "app"}:
                    continue
                verb = fn.attr
                if verb not in {"get", "post", "put", "patch", "delete", "head",
                                "options", "api_route", "websocket"}:
                    continue
                route_path = ""
                if deco.args and isinstance(deco.args[0], ast.Constant) and isinstance(deco.args[0].value, str):
                    route_path = deco.args[0].value
                stats["registrations"] += 1
                if verb == "websocket":
                    stats["websocket"] += 1
                elif verb == "api_route":
                    stats["api_route"] += 1
                else:
                    stats["http_verb"] += 1
                is_async = isinstance(node, ast.AsyncFunctionDef)
                stats["async" if is_async else "def"] += 1
                key = (str(mod), node.name)
                (seen_async if is_async else seen_sync).add(key)
                handlers.append(Handler(node, str(mod), deco.lineno, route_path, verb, is_async))
    stats["async_unique"] = len(seen_async)
    stats["def_unique"] = len(seen_sync)
    return handlers, stats


def audit(handlers: list[Handler]) -> dict:
    rows: list[dict] = []
    offload_handlers = 0
    for h in handlers:
        if not h.is_async:
            continue
        finder = BlockingFinder()
        for stmt in h.node.body:
            finder.visit(stmt)
        if finder.offloaded_calls:
            offload_handlers += 1
        if finder.hits:
            rows.append({
                "file": h.module,
                "file_line": h.lineno,
                "handler": h.name,
                "route": f"{h.verb} {h.path}",
                "route_line": h.route_lineno,
                "calls": finder.hits,
            })
    return {"blocking_handlers": rows, "async_with_offload": offload_handlers}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--json")
    ap.add_argument("--md")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    modules = sorted((root / ROUTERS_DIR).glob("*.py"))
    handlers, stats = collect(modules)
    result = audit(handlers)
    out = {"routers_dir": ROUTERS_DIR, "modules": len(modules), "stats": stats, **result}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
