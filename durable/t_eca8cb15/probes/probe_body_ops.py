#!/usr/bin/env python3
"""Цена каждой операции из тел обработчиков — из этого числа получается порог §1.3.

    python3 durable/t_eca8cb15/probes/probe_body_ops.py            # таблица в stdout
    python3 durable/t_eca8cb15/probes/probe_body_ops.py --json ... --md ...

Зачем. Тип вызова ничего не говорит о цене: `mkdir` на существующем каталоге —
0,01 мс, а `unlink` одного файла даёт хвост в 74 мс; `zipfile.is_zipfile` читает
только конец файла (0,05 мс на 50 МиБ), а `base64.b64decode` тех же 25 МиБ —
105–230 мс. Поэтому §1.3 отбирает по **измеренной цене**, а не по имени функции.

Правило отбора (применяется здесь же, механически):

    оффлоад, если   p50 >= 1,0 мс
                 ИЛИ p95 >= 10,0 мс
                 ИЛИ 28 (размер задаёт вызывающий: лимит 8/16/25/100 МиБ)

иначе — остаётся в цикле событий. Оба порога проверяются на этой машине
(Linux, ext4, тёплый page cache); хвост p95 берётся потому, что именно он
блокирует цикл в худшем случае, а не медиана.

Колонка `locator` — место вызова в коде на ревизии, указанной в прогоне.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile

P50_MS = 1.0
P95_MS = 10.0

LIMIT_8MIB = 8 * 1024 * 1024        # files.py:129 _FS_TEXT_WRITE_MAX_BYTES
LIMIT_16MIB = 16 * 1024 * 1024      # web_server._FS_DATA_URL_MAX_BYTES
LIMIT_25MIB = 25 * 1024 * 1024      # audio.py:52 / files.py:_MEDIA_MAX_BYTES
LIMIT_100MIB = 100 * 1024 * 1024    # web_server._MANAGED_FILE_MAX_BYTES


def bench(fn, repeats: int) -> dict:
    samples: list[float] = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000.0)
    samples.sort()

    def pct(q: float) -> float:
        idx = min(len(samples) - 1, max(0, int(round(q * (len(samples) - 1)))))
        return round(samples[idx], 4)

    return {"p50_ms": round(statistics.median(samples), 4), "p95_ms": pct(0.95),
            "max_ms": round(samples[-1], 4), "n": repeats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(pathlib.Path(__file__).with_name("body_ops.json")))
    ap.add_argument("--md", default=str(pathlib.Path(__file__).with_name("body_ops.md")))
    args = ap.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None, prefix="probe-body-ops-"))
    existing_dir = work / "existing"
    existing_dir.mkdir(parents=True)
    small = work / "small.txt"
    small.write_bytes(b"0" * 4096)
    soul = work / "SOUL.md"
    soul.write_text("# SOUL\n" + ("правило. " * 20000), encoding="utf-8")
    gguf = work / "src.gguf"
    gguf.write_bytes(b"x" * LIMIT_8MIB)
    text8 = "x" * LIMIT_8MIB
    data16 = b"x" * LIMIT_16MIB
    data25 = b"x" * LIMIT_25MIB
    b64_25 = base64.b64encode(data25)
    tree = work / "tree"
    for i in range(20):
        d = tree / f"d{i}"
        d.mkdir(parents=True)
        for j in range(10):
            (d / f"f{j}").write_text("x")
    logbig = work / "action.log"
    with open(logbig, "wb") as fh:
        block = b"2026-11-23 13:00:00 INFO padding-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n" * 500
        for _ in range((64 << 20) // len(block) + 1):
            fh.write(block)
    zipbig = work / "archive.zip"
    with zipfile.ZipFile(zipbig, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("data.bin", b"x" * (50 << 20))
    exited = subprocess.Popen(["/bin/true"])
    exited.wait()

    # --- операции, ровно как в телах обработчиков ---------------------------
    def write8_replace() -> None:                      # files.py:1139-1140 fs_write_text
        tmp = work / "e.tmp"
        tmp.write_text(text8, encoding="utf-8")
        os.replace(tmp, work / "eight.txt")

    def read16_b64() -> None:                          # files.py:1234 _render
        base64.b64encode((work / "d16.bin").read_bytes())

    (work / "d16.bin").write_bytes(data16)

    def session_backup() -> None:                      # web_whatsapp_session.py:48-52, из messaging.py:730
        src = work / "bk-src"
        src.mkdir(exist_ok=True)
        (src / "creds.json").write_text("{}")
        dst = work / f"bk-{time.perf_counter_ns()}"
        src.rename(dst)
        src.mkdir(mode=0o700)
        for child in dst.iterdir():
            child.unlink()
        dst.rmdir()

    tail_ns: dict[str, object] = {"Path": pathlib.Path}
    tail_src = pathlib.Path(__file__).resolve().parents[3] / "hermes_cli/web_routers/actions.py"
    text = tail_src.read_text(encoding="utf-8")
    start = text.find("def _tail_lines")
    end = text.find("\ndef _durable_completed_update_action_id")
    exec(  # noqa: S102 — собственная копия функции из actions.py, константы подставлены
        text[start:end]
        .replace("_ACTION_LOG_TAIL_MAX_BYTES", str(256 * 1024))
        .replace("_ACTION_LOG_TAIL_INITIAL_CHUNK_BYTES", str(8 * 1024))
        .replace("_ACTION_LOG_TAIL_MAX_CHUNK_BYTES", str(64 * 1024)),
        tail_ns,
    )
    tail_lines = tail_ns["_tail_lines"]

    def four_tails() -> None:                          # actions.py:394/403/404 (худший случай: 4 чтения)
        tail_lines(logbig, 2000)
        tail_lines(logbig, 2000)
        tail_lines(logbig, 2000)
        tail_lines(logbig, 2000)

    def git_root_walk() -> None:                       # files.py:243-255, из fs_git_root:1405
        d = work
        for _ in range(50):
            if (d / ".git").exists():
                return
            if d.parent == d:
                return
            d = d.parent

    rows: list[dict] = []

    def add(op: str, locator: str, note: str, fn, repeats: int, size_driven: bool = False) -> None:
        m = bench(fn, repeats)
        verdict = "оффлоад" if (size_driven or m["p50_ms"] >= P50_MS or m["p95_ms"] >= P95_MS) else "в цикле"
        rows.append({"op": op, "locator": locator, "note": note, "size_driven": size_driven,
                     "verdict": verdict, **m})

    # files.py:1083 fs_write_text — лимит 8 МиБ задаёт вызывающий
    add("str.encode 8 МиБ", "files.py:1138", "подсчёт размера тела", lambda: text8.encode("utf-8"), 10, True)
    add("Path.write_text 8 МиБ + os.replace", "files.py:1139-1140", "основная запись", write8_replace, 10, True)
    add("Path.stat / parent.is_dir", "files.py:1122,1134", "проверки перед записью",
        lambda: (small.stat(), existing_dir.is_dir()), 200)
    # audio.py:90 transcribe_audio_upload — лимит 25 МиБ
    add("base64.b64decode 25 МиБ", "audio.py:107", "разбор payload", lambda: base64.b64decode(b64_25, validate=True), 5, True)
    add("write 25 МиБ в NamedTemporaryFile", "audio.py:123", "временный файл", lambda: (work / "a.bin").write_bytes(data25), 5, True)
    # audio.py:299 speak_text / files.py:472 proxy_remote_media — 25 МиБ
    add("base64.b64encode 25 МиБ", "audio.py:348, files.py:509", "кодирование ответа",
        lambda: base64.b64encode(data25), 5, True)
    add("base64.b64encode 1 МиБ", "audio.py:348", "типичный ответ TTS", lambda: base64.b64encode(data25[:1 << 20]), 20, True)
    # files.py:1203 fs_read_data_url — лимит 16 МиБ, только удалённая ветка (1216)
    add("read_bytes + b64encode 16 МиБ", "files.py:1216", "удалённый backend", read16_b64, 10, True)
    # files.py:936 upload_managed_file_stream
    add("Path.mkdir(exist_ok) уже существует", "files.py:946, ops.py:608, ops.py:674",
        "подготовка каталога", lambda: existing_dir.mkdir(parents=True, exist_ok=True), 200)
    # local_models.py:796 local_models_delete — файл готовим заранее: мерим только unlink
    def unlink_8mib() -> None:
        (work / "u.gguf").write_bytes(b"x" * LIMIT_8MIB)
        t0 = time.perf_counter()
        (work / "u.gguf").unlink()
        unlink8_samples.append((time.perf_counter() - t0) * 1000.0)

    unlink8_samples: list[float] = []
    unlink8_stats: dict[str, float] = {}
    for _ in range(20):
        unlink_8mib()
    unlink8_sorted = sorted(unlink8_samples)
    unlink8_stats = {
        "p50_ms": round(statistics.median(unlink8_sorted), 4),
        "p95_ms": round(unlink8_sorted[int(round(0.95 * (len(unlink8_sorted) - 1)))], 4),
        "max_ms": round(unlink8_sorted[-1], 4),
        "n": len(unlink8_sorted),
    }
    rows.append({"op": "Path.unlink 8 МиБ", "locator": "local_models.py:803",
                 "note": "удаление файла модели", "size_driven": False,
                 "verdict": "оффлоад" if unlink8_stats["p50_ms"] >= P50_MS else "в цикле", **unlink8_stats})
    add("Path.unlink(missing_ok) нет файла", "ops.py:685, files.py:1142/1145",
        "уборка после ошибки", lambda: (work / "nope").unlink(missing_ok=True), 200)
    # local_models.py:1041 local_models_sideload
    add("os.link", "local_models.py:1052", "хардлинк модели",
        lambda: (os.link(gguf, work / "l.gguf") or (work / "l.gguf").unlink()), 50)
    add("os.symlink", "local_models.py:1055", "запасной путь", lambda: (os.symlink(gguf, work / "s.gguf") or (work / "s.gguf").unlink()), 50)
    # ops.py:666 run_import_upload
    add("zipfile.is_zipfile 50 МиБ", "ops.py:684", "проверка архива", lambda: zipfile.is_zipfile(zipbig), 50)
    # actions.py:386 get_action_status
    add("_tail_lines × 4 (лог 64 МиБ)", "actions.py:394,403,404", "хвост лога", four_tails, 20)
    add("Popen.wait на завершённом процессе", "actions.py:436", "сбор exit code", lambda: exited.wait(timeout=1), 200)
    # profiles.py:1124 open_profile_terminal_endpoint
    add("subprocess.Popen", "profiles.py:1130,1133,1139", "запуск терминала", lambda: subprocess.Popen(["/bin/true"]), 50)
    # profiles.py:1195 get_profile_soul (уже оффлоадится через _read_off_loop — контрольная точка)
    add("Path.read_text SOUL.md 200 КиБ", "profiles.py:1203", "контроль: уже в пуле", lambda: soul.read_text(encoding="utf-8"), 50)
    # messaging.py:701 start_whatsapp_onboarding — тело целиком
    add("creds.json exists", "messaging.py:717", "проверка привязки", lambda: (existing_dir / "creds.json").exists(), 200)
    add("str.replace на isoformat", "messaging.py:714", "строковый, не файловый", lambda: "2026-11-23T13:00:00+00:00".replace("+00:00", "Z"), 200)
    add("rename сессии + mkdir 0700", "messaging.py:730 → web_whatsapp_session.py:48-52",
        "резервная копия сессии", session_backup, 20)
    # метаданные (в цикле, но названы явно)
    add("Path.exists", "dashboard_ui.py:439, status.py:1008, ops.py:652, profiles.py:1419",
        "метаданные", lambda: small.exists(), 200)
    add("Path.is_file", "files.py:442, ops.py:631, dashboard_ui.py:439", "метаданные", lambda: small.is_file(), 200)
    add("обход до корня git (50 каталогов)", "files.py:1405", ".git в каждом родителе", git_root_walk, 50)
    add("rglob по 220 записям", "ops.py:841", "перечисление чекпоинтов", lambda: list(tree.rglob("*")), 20)

    out = {
        "tmpdir": str(work),
        "rule": {"p50_ms": P50_MS, "p95_ms": P95_MS,
                 "size_driven": "лимит размера тела задаёт вызывающий -> всегда оффлоад"},
        "ops": rows,
    }
    pathlib.Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["| Операция | Локатор | p50, мс | p95, мс | max, мс | n | Вердикт |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['op']} | `{r['locator']}` | {r['p50_ms']} | {r['p95_ms']} | {r['max_ms']} | {r['n']} | "
                     f"{r['verdict']}{' (размер)' if r['size_driven'] else ''} |")
    md = "\n".join(lines) + "\n"
    pathlib.Path(args.md).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
