#!/usr/bin/env python3
"""Одноразовый выкат волны 2: рестарт шлюза извне его дерева + замеры после.

ЖИВЁТ В РЕПОЗИТОРИИ: ``ops/deploy-wave2.py`` — канонический файл под контролем версий
(дифф, ревью, откат). Прежний маршрут ``01-orchestrator/ops/bin/deploy-wave2.py`` оставлен
тонкой обёрткой, которая запускает этот файл; править надо здесь.

Запускается через systemd-run --user --on-active, поэтому НЕ живёт внутри
перезапускаемого процесса. Всё пишет в лог рядом: pid до/после, время подъёма,
/api/status, 8 одновременных запросов, число ошибок.

ПРЕДОХРАНИТЕЛИ (26.11.2026, рестарт 03:19 оборвал живые прогоны):
  * запуск ИЗ ВОРКЕРА запрещён — он убил бы шлюз, который сам его породил;
  * перед рестартом обязательны подтверждённое окно (--window) и отсутствие живых прогонов;
  * при любом сомнении — отказ (код 3/4), рестарт не выполняется вообще.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))
BASE = "http://127.0.0.1:9120"
LOG = os.path.join(
    "/home/hermes/workspaces/01-orchestrator/ops/logs",
    f"deploy-wave2-{datetime.now(MSK).strftime('%Y%m%dT%H%M%S')}.log",
)
KANBAN_DB = os.environ.get("HERMES_KANBAN_DB") or os.path.expanduser("~/.hermes/kanban.db")

#: Воркер карточки ставит эти переменные: их наличие = «нас запустил прогон».
_WORKER_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_WORKSPACE")


def log(msg: str) -> None:
    line = f"{datetime.now(MSK).strftime('%d.%m %H:%M:%S')} МСК | {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def refuse(why: str, code: int = 3) -> int:
    log(f"ОТКАЗ: {why}")
    log("=== выкат НЕ выполнен, шлюз не тронут ===")
    return code


def worker_context() -> str | None:
    """Причина, по которой это запуск из воркера (иначе None)."""
    for key in _WORKER_ENV:
        if os.environ.get(key):
            return f"переменная окружения {key}={os.environ[key]!r}"
    try:
        parent = open(f"/proc/{os.getppid()}/cmdline", "rb").read().decode("utf-8", "replace")
    except OSError:
        return None
    if "kanban_worker_client" in parent:
        return "родительский процесс — kanban_worker_client"
    return None


def live_runs() -> list[tuple[str, int]]:
    """``[(task_id, worker_pid)]`` карточек в ``running`` с ЖИВЫМ PID воркера.

    Пустой список — рестарт никого не оборвёт. Недоступная база — это «неизвестно»:
    вызывающий обязан отказаться, а не рестартовать наугад.
    """
    if not os.path.exists(KANBAN_DB):
        raise RuntimeError(f"база доски не найдена: {KANBAN_DB}")
    conn = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, COALESCE(worker_pid, 0) FROM tasks WHERE status = 'running'"
        ).fetchall()
    finally:
        conn.close()
    live = []
    for task_id, pid in rows:
        if not pid:
            live.append((task_id, 0))  # running без PID считаем живым
            continue
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            continue  # PID мёртв: прогон осиротел, рестарт его не оборвёт
        live.append((task_id, int(pid)))
    return live


def window_ok(path: str | None) -> str | None:
    """Причина отказа, если окно не подтверждено (иначе None).

    Окно — файл-маркер: первая строка — срок действия в ISO-8601. Истёкший, пустой
    или отсутствующий маркер окном не считается.
    """
    if not path:
        return "не указано окно: передайте --window <путь к маркеру окна>"
    if not os.path.exists(path):
        return f"маркер окна не найден: {path}"
    try:
        text = open(path, encoding="utf-8").read().strip().splitlines()
        until = datetime.fromisoformat(text[0].strip())
    except (OSError, IndexError, ValueError) as exc:
        return f"маркер окна нечитаем ({path}): {exc}"
    if until.tzinfo is None:
        until = until.replace(tzinfo=MSK)
    if datetime.now(MSK) > until:
        return (f"окно истекло {until.isoformat()} "
                f"(сейчас {datetime.now(MSK).isoformat(timespec='seconds')})")
    return None


def pid_of_gateway() -> str:
    out = subprocess.run(
        ["systemctl", "--user", "show", "-p", "MainPID", "--value", "hermes-gateway.service"],
        capture_output=True, text=True,
    ).stdout.strip()
    return out or "0"


def fetch(path: str) -> tuple[int, float, bytes]:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as resp:
            body = resp.read()
            return resp.status, (time.perf_counter() - start) * 1000, body
    except urllib.error.HTTPError as exc:
        return exc.code, (time.perf_counter() - start) * 1000, exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, (time.perf_counter() - start) * 1000, str(exc).encode()


def main(argv: list[str]) -> int:
    window = None
    if "--window" in argv:
        idx = argv.index("--window")
        window = argv[idx + 1] if idx + 1 < len(argv) else None

    log("=== выкат волны 2 начат ===")

    # 1. Из воркера — никогда: рестарт убил бы процесс, который нас же и запустил.
    if (why := worker_context()):
        return refuse(f"запуск из kanban-воркера запрещён ({why}); выкат выполняет внешний "
                      "systemd-run-таймер, а не прогон карточки")

    # 2. Живые прогоны: рестарт оборвал бы admitted-исполнения (как 03:19).
    try:
        live = live_runs()
    except Exception as exc:  # noqa: BLE001
        return refuse(f"не удалось проверить живые прогоны: {exc}", code=4)
    if live:
        listing = ", ".join(f"{tid}(pid {pid or '?'})" for tid, pid in live[:8])
        return refuse(f"{len(live)} живых прогонов: {listing} — дождитесь их завершения", code=4)
    log("живых прогонов нет")

    # 3. Окно: рестарт — плановая операция с явно подтверждённым интервалом.
    if (why := window_ok(window)):
        return refuse(why)
    log(f"окно подтверждено: {window}")

    pid_before = pid_of_gateway()
    log(f"PID до рестарта: {pid_before}")

    code, ms, body = fetch("/api/status")
    log(f"до: /api/status код={code} {ms:.0f} мс, {len(body)} байт")

    log("рестарт: systemctl --user restart hermes-gateway.service")
    r = subprocess.run(
        ["systemctl", "--user", "restart", "hermes-gateway.service"],
        capture_output=True, text=True,
    )
    log(f"systemctl вернул код={r.returncode} stderr={r.stderr.strip()[:200]}")

    up_at = None
    for attempt in range(1, 121):
        time.sleep(1)
        code, ms, _ = fetch("/api/status")
        if code == 200:
            up_at = attempt
            break
    pid_after = pid_of_gateway()
    log(f"PID после рестарта: {pid_after} (смена: {'да' if pid_after != pid_before else 'НЕТ'})")
    log(f"шлюз отвечает 200 через {up_at if up_at else '>120'} с")

    if up_at is None:
        log("ОТКАТ: шлюз не поднялся — вернуть предыдущий коммит и перезапустить")
        return 2

    time.sleep(5)
    single = [fetch("/api/status") for _ in range(5)]
    log("одиночные: " + ", ".join(f"{c}/{m:.0f}мс" for c, m, _ in single))

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        eight = list(pool.map(lambda _: fetch("/api/status"), range(8)))
    wall = (time.perf_counter() - start) * 1000
    lat = sorted(m for _, m, _ in eight)
    errors = sum(1 for c, _, _ in eight if c != 200)
    log(
        f"8 одновременно: общее время {wall:.0f} мс, медиана {lat[len(lat) // 2]:.0f} мс, "
        f"максимум {lat[-1]:.0f} мс, ошибок {errors}/8"
    )

    code, ms, body = fetch("/api/status")
    try:
        payload = json.loads(body)
    except Exception:  # noqa: BLE001
        payload = {}
    offload = payload.get("handler_offload")
    log(f"handler_offload после: {json.dumps(offload, ensure_ascii=False)}")
    log(f"версия: {payload.get('version')} ({payload.get('release_date')})")
    log("=== выкат волны 2 завершён ===")
    log(f"лог: {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
