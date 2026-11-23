#!/usr/bin/env python3
"""AST-аудит HTTP-маршрутов панели: где цикл событий действительно блокируется.

Записка t_eca8cb15 §1.3 обязана быть воспроизводимой: каждое число ниже печатает
этот скрипт, ничего не считается руками.

    python3 durable/t_eca8cb15/probes/route_audit.py --json durable/t_eca8cb15/probes/route_audit.json
    # сверка с живым приложением (FastAPI-объект, а не текст):
    python3 durable/t_eca8cb15/probes/route_audit.py --live

## Что считается

1. **Регистрации.** Декоратор `<любое_имя>.<verb>(...)` в
   `hermes_cli/web_routers/*.py`, где `verb` из HTTP/WS-набора. Имя объекта-роутера
   не проверяется: модули объявляют `router`, `admin_router`, `public_router` и др.,
   и фильтр по `router` теряет часть регистраций. Один обработчик на двух путях —
   две регистрации; уникальные обработчики считаются отдельно.
   `api_route` и `websocket` считаются отдельно от `get/post/...`.

2. **Синхронные обработчики (`def`).** Starlette исполняет их через
   `run_in_threadpool`, то есть они НЕ блокируют цикл событий по построению и в
   список правок не входят — их считает только перепись.

3. **Порог отбора блокирующего (ЯВНЫЙ).** В `async def`-обработчике блокирующим
   считается вызов из списка ниже. Всё остальное — не блокирующее.

   * `PATH_IO` — трогает данные или строит/удаляет узлы: `read_text`, `read_bytes`,
     `write_text`, `write_bytes`, `mkdir`, `makedirs`, `unlink`, `remove`, `rmdir`,
     `rename`, `replace`, `chmod`, `touch`, `symlink_to`, `iterdir`, `glob`, `rglob`,
     `scandir`, `listdir`, `walk`, `opens`/`open`, `getsize`, `readlink`, `fsync`.
   * `open(...)` (встроенный и `io`/`codecs`) — чтение/запись файла.
   * `os`/`shutil`/`subprocess`/`sqlite3`/`socket`/`requests`/`urllib` — любой вызов
     с корнем в этом модуле (кроме конструкторов объектов).
   * `time.sleep`, `threading.*.acquire/join/release`, `concurrent.futures` `wait`.

   **Исключены из порога осознанно — метаданные** (`exists`, `is_file`, `is_dir`,
   `is_symlink`, `stat`, `lstat`, `samefile`, `resolve`, `st_*`): это вызов
   `stat(2)` ценой единицы микросекунд, на 8 одновременных запросах — доли
   миллисекунды. Записи и чтения (пункт выше) стоят на два-четыре порядка дороже
   и в список входят. Исключение печатается отдельным числом (`meta_only`), чтобы
   обработчик «только со stat» был виден, а не просто отброшен.

   **Исключены асинхронные двойники:** `httpx.AsyncClient`, `asyncio.sleep`,
   `asyncio.*`, `anyio.*` (кроме `anyio.to_thread.run_sync`, который сам оффлоадит),
   `websockets.*` — они не блокируют цикл.

   **Ложные срабатывания по имени метода** (`str.replace`, `list.remove`,
   `dict.get`, `queue.get`) отсекаются проверкой получателя: `replace`/`remove`/
   `run`/`get`/`open`/`write`/`read` считаются файловыми только если последний
   компонент имени получателя содержит `path|file|dir|root|target|tmp|dest|src|`
   `archive|staging|home|soul|skill|child|store|db|catalog|lock|buf`. Правило
   документировано здесь и одинаково для всех файлов.

4. **Оффлоад.** Обработчик считается уже оффлоадящим, если в теле есть обёртка
   пула: `asyncio.to_thread`, `anyio.to_thread.run_sync`, `loop.run_in_executor`,
   `starlette.concurrency.run_in_threadpool`, а также проектные обёртки, которые
   сводятся к ним — `scoped_to_thread`, `config_scoped_to_thread` (`_common.py`),
   `_read_off_loop` (`profiles.py`), `_hub_lookup` (`skills.py`),
   `_run_config_scoped` (`audio.py`). Вызовы внутри первого аргумента (тела
   лямбды/функции) блокирующими не считаются.

5. **Вложенные функции.** Тело `def _run(): …` исполняется не в момент
   объявления, поэтому считается отдельно: если имя помощника передано в обёртку
   оффлоада (`_hub_lookup(_run, …)`), он помечается `offloaded: true`; иначе
   попадает в `nested_left` — кандидат на ручное решение, а не в основной список.
   Обратная сторона названа честно: блокирующий вызов во вложенной функции,
   которую никто не оффлоадит, автоматически основным списком не ловится.

Числа §1.3 записки — вывод этого скрипта на ревизии, указанной в записке;
`--live` сверяет их с FastAPI-объектом; `--md` печатает готовую таблицу.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROUTERS_DIR = "hermes_cli/web_routers"

VERBS = {"get", "post", "put", "patch", "delete", "head", "options",
         "api_route", "websocket"}

# Трогает данные или строит/удаляет узлы — дороже метаданных на порядки.
PATH_IO = {
    "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "makedirs",
    "unlink", "remove", "rmdir", "rename", "replace", "chmod", "touch",
    "symlink_to", "hardlink_to", "iterdir", "glob", "rglob", "scandir",
    "listdir", "walk", "open", "opens", "getsize", "readlink", "fsync",
    "truncate", "copy", "copy2", "copyfile", "copytree", "move", "rmtree",
}

# Метаданные: в порог не входят, но пересчитываются отдельно.
PATH_META = {"exists", "is_file", "is_dir", "is_symlink", "stat", "lstat",
             "samefile", "resolve"}

BLOCKING_MODULES = {"os", "shutil", "subprocess", "sqlite3", "socket",
                    "requests", "urllib", "fcntl", "msvcrt"}
# CPU-класс: НЕ файловый ввод-вывод, но столько же блокирующий цикл, потому что
# работает на размере тела (25 МиБ base64 = 125-230 мс, см. probes/body_ops.json).
# Без этого класса перепись пропускала `base64.b64decode` в audio.py:107 и
# `base64.b64encode` в files.py:509 — самые дорогие одиночные вызовы в цикле.
CPU_MODULES = {"base64", "zipfile", "hashlib", "zlib", "gzip", "tarfile", "lzma", "bz2"}
ASYNC_MODULES = {"asyncio", "anyio", "httpx", "websockets", "aiohttp", "trio",
                 "starlette", "fastapi"}
BLOCKING_CALLS = {"time.sleep", "threading.Event.wait", "threading.Lock.acquire",
                  "threading.RLock.acquire", "concurrent.futures.wait",
                  "select.select", "selectors.DefaultSelector"}

OFFLOAD_CALLS = {
    # stdlib / anyio / starlette
    "to_thread", "run_in_executor", "run_sync", "run_in_threadpool",
    # проектные обёртки: все они сводятся к asyncio.to_thread/run_in_threadpool
    # (`hermes_cli/web_routers/_common.py`: scoped_to_thread,
    # config_scoped_to_thread; profiles.py: _read_off_loop; skills.py: _hub_lookup;
    # audio.py: _run_config_scoped).
    "config_scoped_to_thread", "scoped_to_thread", "_read_off_loop",
    "_hub_lookup", "_run_config_scoped",
}

# Получатель «похож на путь» — см. пункт 3 докстринга.
RECEIVER_HINTS = ("path", "file", "dir", "root", "target", "tmp", "dest", "src",
                  "archive", "staging", "home", "soul", "skill", "child",
                  "store", "db", "catalog", "lock", "buf", "proc", "pool",
                  "future", "event", "thread", "tmpfile")


def dotted(node: ast.AST) -> str:
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
    return dotted(call.func).split(".")[-1] in OFFLOAD_CALLS


def _receiver_ok(call: ast.Call) -> bool:
    """Получатель метода похож на путь/файл (см. RECEIVER_HINTS)."""
    if not isinstance(call.func, ast.Attribute):
        return False
    recv = dotted(call.func.value) or (
        call.func.value.id if isinstance(call.func.value, ast.Name) else ""
    )
    if not recv:
        return False
    last = recv.split(".")[-1].lower()
    return any(h in last for h in RECEIVER_HINTS)


def classify(call: ast.Call) -> tuple[str, str] | None:
    """('blocking'|'meta', имя) для вызова, иначе None."""
    full = dotted(call.func)
    if not full:
        return None
    root = full.split(".")[0]
    last = full.split(".")[-1]

    if full in BLOCKING_CALLS:
        return ("blocking", full)
    # Не блокирующее: разбор URL, чтение os.environ, идентификаторы процесса.
    if root == "urllib" and full.split(".")[1:2] == ["parse"]:
        return None
    if full in {"urllib.request.Request", "urllib.request.build_opener"}:
        return None
    if root == "os" and full.split(".")[1:2] == ["environ"]:
        return None
    if full in {"os.getpid", "os.getcwd", "os.cpu_count", "os.urandom", "os.fspath"}:
        return None
    if root == "os" and full.split(".")[1:2] == ["path"] and last in {
        "join", "basename", "dirname", "splitext", "isabs", "normpath",
        "abspath", "expanduser",
    }:
        return None
    if root == "os" and full.split(".")[1:2] == ["path"] and last in {
        "exists", "isfile", "isdir", "getsize", "getmtime",
    }:
        return ("meta", full)
    # Асинхронные модули/классы: httpx.AsyncClient, asyncio.sleep и т.п.
    if root in ASYNC_MODULES:
        if "AsyncClient" in full or "Async" in last:
            return None
        if last in {"get", "post", "put", "delete", "request", "stream"} and "Client" in full:
            return ("blocking", full)  # httpx.Client — синхронный
        return None
    if root in CPU_MODULES:
        if last in {"b64encode", "b64decode", "urlsafe_b64encode", "urlsafe_b64decode",
                    "ZipFile", "is_zipfile", "extractall", "extract", "crc32",
                    "compress", "decompress", "compressobj", "decompressobj", "open",
                    "sha256", "sha1", "md5", "blake2b", "pbkdf2_hmac"}:
            return ("blocking", full)
        return None
    if root in BLOCKING_MODULES:
        if last in {"Path", "PurePath", "str", "list", "dict", "fspath"}:
            return None
        if last in {"connect"} and root == "sqlite3":
            return ("blocking", full)
        return ("blocking", full)
    if root == "builtins" and last == "open":
        return ("blocking", full)
    if last in {"open", "read", "write", "readlines", "writelines"} and _receiver_ok(call):
        return ("blocking", full)
    if last in PATH_IO:
        # `str.replace`, `list.remove`, `dict.pop`, `re.sub` — не файловые.
        if last in {"replace", "remove", "open", "walk", "copy", "move", "run"} and not _receiver_ok(call):
            return None
        return ("blocking", full)
    if last in PATH_META:
        return ("meta", full)
    # Ожидание процесса/потока/будущего: ждёт, пока получатель «похож» на них.
    if last in {"wait", "communicate", "join"} and _receiver_ok(call):
        return ("blocking", full)
    return None


class Finder(ast.NodeVisitor):
    """Обход тела async-обработчика.

    Тело вложенной функции (`def _run(): ...`) исполняется НЕ в момент
    объявления: она уходит в пул целиком (`asyncio.to_thread(_run)`), поэтому её
    вызовы считаются отдельно и в основной список не попадают. Если её имя
    передано в обёртку оффлоада, она помечается `offloaded_by_name`.
    Обратная сторона того же решения названа в докстринге модуля: блокирующий
    вызов в вложенной функции, которую никто не оффлоадит, виден только в
    `nested_helpers` и требует ручного решения.
    """

    def __init__(self) -> None:
        self.blocking: list[tuple[int, str]] = []
        self.meta: list[tuple[int, str]] = []
        self.offload_wrappers = 0
        self.offloaded_names: set[str] = set()
        self.nested_helpers: list[dict] = []
        self._nested: Finder | None = None

    # --- вложенные функции -------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._scan_nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._scan_nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        inner = Finder()
        inner._nested = self if self._nested is None else self._nested
        outer = self._nested or self
        for stmt in [node.body]:
            inner.visit(stmt)
        outer.nested_helpers.append({
            "helper": "<lambda>", "line": node.lineno,
            "calls": inner.blocking + inner.meta, "offloaded": False,
        })

    def _scan_nested(self, node) -> None:  # node: ast.FunctionDef|ast.AsyncFunctionDef
        owner = self._nested or self
        inner = Finder()
        inner._nested = owner
        for stmt in node.body:
            inner.visit(stmt)
        owner.nested_helpers.append({
            "helper": node.name, "line": node.lineno,
            "calls": inner.blocking + inner.meta, "offloaded": False,
        })

    # --- вызовы ------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        if is_offloaded(node):
            self.offload_wrappers += 1
            # Имя функции, переданной в пул, трекаем, чтобы пометить вложенную.
            # Обёртки проекта передают её не первым аргументом
            # (`config_scoped_to_thread(profile, _run)`), поэтому смотрим все.
            for arg in node.args:
                target = dotted(arg)
                if target:
                    (self._nested or self).offloaded_names.add(target.split(".")[-1])
            # Первый аргумент исполняется в пуле — внутрь не спускаемся.
            for arg in node.args[1:]:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
            return
        kind = classify(node)
        if kind:
            (self.blocking if kind[0] == "blocking" else self.meta).append(
                (node.lineno, kind[1]))
        self.generic_visit(node)


def collect(modules: list[Path]) -> tuple[list[dict], dict]:
    handlers: list[dict] = []
    stats = {"registrations": 0, "http_verb": 0, "api_route": 0, "websocket": 0,
             "async": 0, "def": 0}
    uniq_async: set[tuple[str, str]] = set()
    uniq_sync: set[tuple[str, str]] = set()
    for mod in sorted(modules):
        tree = ast.parse(mod.read_text(encoding="utf-8"), filename=str(mod))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                fn = deco.func
                if not isinstance(fn, ast.Attribute):
                    continue
                if fn.attr not in VERBS:
                    continue
                path = ""
                if deco.args and isinstance(deco.args[0], ast.Constant) \
                        and isinstance(deco.args[0].value, str):
                    path = deco.args[0].value
                stats["registrations"] += 1
                key = "websocket" if fn.attr == "websocket" else (
                    "api_route" if fn.attr == "api_route" else "http_verb")
                stats[key] += 1
                is_async = isinstance(node, ast.AsyncFunctionDef)
                stats["async" if is_async else "def"] += 1
                (uniq_async if is_async else uniq_sync).add((str(mod), node.name))
                handlers.append({
                    "file": str(mod), "file_line": node.lineno, "handler": node.name,
                    "layer": key, "route": f"{fn.attr} {path}",
                    "route_line": deco.lineno, "is_async": is_async,
                    "node": node,
                })
    stats["async_unique"] = len(uniq_async)
    stats["def_unique"] = len(uniq_sync)
    return handlers, stats


def audit(handlers: list[dict]) -> dict:
    blocking_rows, meta_only_rows, nested_rows = [], [], []
    offload_handlers = 0
    # Разбивка оффлоада: сколько вызовов и сколько обработчиков на каждую обёртку.
    # Печатается, чтобы §1.3 не зависел от ручного подсчёта (прежняя редакция
    # приводила 102/18/1/55 без команды, которой это проверяется).
    offload_calls: dict[str, int] = {}
    offload_handlers_by_kind: dict[str, int] = {}
    for h in handlers:
        if not h["is_async"]:
            continue
        f = Finder()
        for stmt in h["node"].body:
            f.visit(stmt)
        if f.offload_wrappers:
            offload_handlers += 1
        kinds: set[str] = set()
        for node in ast.walk(h["node"]):
            if isinstance(node, ast.Call) and is_offloaded(node):
                name = dotted(node.func)
                offload_calls[name] = offload_calls.get(name, 0) + 1
                kinds.add(name)
        for kind in kinds:
            offload_handlers_by_kind[kind] = offload_handlers_by_kind.get(kind, 0) + 1
        base = {k: v for k, v in h.items() if k != "node"}
        if f.blocking:
            blocking_rows.append({**base, "calls": f.blocking})
        elif f.meta:
            meta_only_rows.append({**base, "calls": f.meta})
        # Вложенные помощники с блокирующими вызовами: оффлоадятся по имени?
        for nh in f.nested_helpers:
            if not nh["calls"]:
                continue
            offloaded = nh["helper"] in f.offloaded_names
            nested_rows.append({**base, **nh, "offloaded": offloaded})
            if offloaded and not f.blocking:
                # Этот обработчик оффлоадит вложенного помощника — уже правильный.
                if not any(r["handler"] == h["handler"] and r["file"] == h["file"]
                           for r in blocking_rows):
                    blocking_rows = [r for r in blocking_rows if not (
                        r["handler"] == h["handler"] and r["file"] == h["file"])]
    return {"blocking_handlers": blocking_rows,
            "meta_only_handlers": meta_only_rows,
            "nested_helpers": [r for r in nested_rows if r["offloaded"]],
            "nested_left": [r for r in nested_rows if not r["offloaded"]],
            "async_with_offload": offload_handlers,
            "offload_calls": dict(sorted(offload_calls.items(), key=lambda kv: -kv[1])),
            "offload_handlers_by_kind": dict(sorted(offload_handlers_by_kind.items(),
                                                    key=lambda kv: -kv[1]))}


def live_counts() -> dict:
    """Перепись по живому FastAPI-объекту (не по тексту)."""
    import inspect
    from hermes_cli import web_server
    app = web_server.app
    http = [r for r in app.routes if getattr(r, "methods", None)]
    ws = [r for r in app.routes if type(r).__name__ == "APIWebSocketRoute"]

    def bucket(rs):
        a = s = 0
        for r in rs:
            ep = getattr(r, "endpoint", None)
            if ep is None:
                continue
            if inspect.iscoroutinefunction(ep):
                a += 1
            else:
                s += 1
        return [a, s]

    def ours(rs):
        return [r for r in rs if str(getattr(getattr(r, "endpoint", None),
                                            "__module__", "")).startswith("hermes_cli.web_routers")]

    return {
        "all_http": [len(http), bucket(http)],
        "all_ws": len(ws),
        "web_routers_http": [len(ours(http)), bucket(ours(http))],
        "web_routers_ws": len(ours(ws)),
    }


def render_md(out: dict, rev: str) -> str:
    s = out["stats"]
    lines = [
        f"Ревизия: `{rev}`. Сгенерировано `durable/t_eca8cb15/probes/route_audit.py`.",
        "",
        f"Регистраций в `{out['routers_dir']}/*.py` ({out['modules']} модулей): "
        f"**{s['registrations']}** = HTTP {s['http_verb']} + `api_route` {s['api_route']} "
        f"+ WS {s['websocket']}; `async` {s['async']} (уникальных {s['async_unique']}), "
        f"`def` {s['def']}. `async`-обработчиков с оффлоадом: **{out['async_with_offload']}**.",
        "",
        "Разбивка оффлоада (обёртка → вызовов / обработчиков):",
        "",
        "| Обёртка | Вызовов | Обработчиков |",
        "|---|---|---|",
    ]
    kinds = out.get("offload_handlers_by_kind") or {}
    for kind, n_calls in (out.get("offload_calls") or {}).items():
        lines.append(f"| `{kind}` | {n_calls} | {kinds.get(kind, 0)} |")
    lines += [
        "| Файл:строка | Обработчик | Блокирующие вызовы (строка:вызов) |",
        "|---|---|---|",
    ]
    for r in out["blocking_handlers"]:
        calls = ", ".join(f"{n}:`{c}`" for n, c in r["calls"])
        lines.append(f"| `{Path(r['file']).name}:{r['file_line']}` | `{r['handler']}` | {calls} |")
    lines += ["", "Только метаданные (в порог не входят):", "",
              "| Файл:строка | Обработчик | Вызовы |", "|---|---|---|"]
    for r in out["meta_only_handlers"]:
        calls = ", ".join(f"{n}:`{c}`" for n, c in r["calls"])
        lines.append(f"| `{Path(r['file']).name}:{r['file_line']}` | `{r['handler']}` | {calls} |")
    lines += ["", "Вложенные помощники, уже уходящие в пул по имени:", "",
              "| Файл:строка | Обработчик | Помощник | Вызовы |", "|---|---|---|---|"]
    for r in out["nested_helpers"]:
        calls = ", ".join(f"{n}:`{c}`" for n, c in r["calls"])
        lines.append(f"| `{Path(r['file']).name}:{r['line']}` | `{r['handler']}` | `{r['helper']}` | {calls} |")
    if out["nested_left"]:
        lines += ["", "**Вложенные помощники без оффлоада (ручное решение):**", "",
                  "| Файл:строка | Обработчик | Помощник | Вызовы |", "|---|---|---|---|"]
        for r in out["nested_left"]:
            calls = ", ".join(f"{n}:`{c}`" for n, c in r["calls"])
            lines.append(f"| `{Path(r['file']).name}:{r['line']}` | `{r['handler']}` | `{r['helper']}` | {calls} |")
    return "\n".join(lines) + "\n"


def git_rev(root: Path) -> str:
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--json")
    ap.add_argument("--md")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    if args.live:
        sys.path.insert(0, str(root))
        print(json.dumps(live_counts(), ensure_ascii=False, indent=2))
        return 0
    modules = sorted((root / ROUTERS_DIR).glob("*.py"))
    handlers, stats = collect(modules)
    rev = git_rev(root)
    out = {"git_rev": rev, "routers_dir": ROUTERS_DIR, "modules": len(modules),
           "stats": stats, **audit(handlers)}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    if args.md:
        Path(args.md).write_text(render_md(out, rev), encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
