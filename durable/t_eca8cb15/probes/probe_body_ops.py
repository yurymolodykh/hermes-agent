#!/usr/bin/env python3
"""Цена конкретных операций из тел обработчиков §1.3 — основание для порога отбора.

    python3 durable/t_eca8cb15/probes/probe_body_ops.py [--json out.json]

Зачем: §1.3 решает, что уходит из цикла событий, а что остаётся. Решение
принимается по измеренной цене, а не по типу вызова (`mkdir` — не всегда дорого).
Каждая строка здесь — реальная операция из тела конкретного обработчика
(`web_routers/files.py:1083 fs_write_text`, `audio.py:90 transcribe_audio_upload`,
`local_models.py:1041 local_models_sideload`, `ops.py:601/666`, `actions.py:386`).

Порог: операция уходит в пул, если её медиана >= 1 мс ИЛИ её размер задаёт
вызывающий (лимит 8 МиБ у `fs_write_text`, 25 МиБ у аудио) — тогда считаем по
верхней границе лимита, а не по типичному файлу.
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

LIMIT_8MIB = 8 * 1024 * 1024       # web_routers/files.py:129 _FS_TEXT_WRITE_MAX_BYTES
LIMIT_25MIB = 25 * 1024 * 1024     # web_routers/audio.py:52 _MAX_TRANSCRIPTION_UPLOAD_BYTES


def bench(fn, repeats: int) -> dict:
    samples: list[float] = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000.0)
    return {"p50_ms": round(statistics.median(samples), 4),
            "max_ms": round(max(samples), 4), "n": repeats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(pathlib.Path(__file__).with_name("body_ops.json")))
    args = ap.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None, prefix="probe-body-ops-"))
    existing_dir = work / "existing"
    existing_dir.mkdir(parents=True)
    small = work / "small.txt"
    small.write_bytes(b"0" * 4096)
    gguf = work / "src.gguf"
    gguf.write_bytes(b"x" * (1 << 20))
    text8 = "x" * LIMIT_8MIB
    audio25 = b"x" * LIMIT_25MIB
    audio_b64 = base64.b64encode(audio25).decode()
    tree = work / "tree"
    for i in range(20):
        d = tree / f"d{i}"
        d.mkdir(parents=True)
        for j in range(10):
            (d / f"f{j}").write_text("x")

    def write8_replace() -> None:                     # files.py:1139-1140
        tmp = work / "e.tmp"
        tmp.write_text(text8, encoding="utf-8")
        os.replace(tmp, work / "eight.txt")

    def session_backup() -> None:                     # web_whatsapp_session.py:48-52
        src = work / "bk-src"
        src.mkdir(exist_ok=True)
        (src / "creds.json").write_text("{}")
        dst = work / f"bk-{time.perf_counter_ns()}"
        src.rename(dst)
        src.mkdir(mode=0o700)
        for child in dst.iterdir():
            child.unlink()
        dst.rmdir()

    def git_root_walk() -> None:                      # files.py:243-255
        d = pathlib.Path(__file__).resolve().parent
        for _ in range(50):
            if (d / ".git").exists():
                return
            if d.parent == d:
                return
            d = d.parent

    exited = subprocess.Popen(["/bin/true"])
    exited.wait()

    out = {
        "tmpdir": str(work),
        "threshold_ms": 1.0,
        "note": ">=1 мс или размер задаёт вызывающий (8 МиБ / 25 МиБ) -> оффлоад; "
                "метаданные (~0,003–0,06 мс) остаются в цикле",
        "ops": {
            # files.py:1083 fs_write_text
            "encode_8MiB": bench(lambda: text8.encode("utf-8"), 5),
            "write_text_8MiB_plus_replace": bench(write8_replace, 5),
            "stat_4k": bench(small.stat, 200),
            "parent_is_dir": bench(existing_dir.is_dir, 200),
            # audio.py:90 transcribe_audio_upload
            "b64decode_25MiB": bench(lambda: base64.b64decode(audio_b64, validate=True), 5),
            "write_bytes_25MiB": bench(lambda: (work / "25.bin").write_bytes(audio25), 5),
            # local_models.py:1041 / ops.py:601,666 / files.py:936
            "mkdir_exist_ok_existing": bench(lambda: existing_dir.mkdir(parents=True, exist_ok=True), 50),
            "os_link": bench(lambda: (os.link(gguf, work / "l.gguf") or (work / "l.gguf").unlink()), 20),
            "os_symlink": bench(lambda: (os.symlink(gguf, work / "s.gguf") or (work / "s.gguf").unlink()), 20),
            "unlink_missing_ok_missing": bench(lambda: (work / "nope").unlink(missing_ok=True), 50),
            # actions.py:386, profiles.py:1124
            "popen_only": bench(lambda: subprocess.Popen(["/bin/true"]), 30),
            "wait_on_exited_proc": bench(lambda: exited.wait(timeout=1), 50),
            # метаданные, вызываемые из async-тел (dashboard_ui.py:439, files.py:442/1389,
            # profiles.py:1395, status.py:1008, ops.py:620/652)
            "exists": bench(small.exists, 200),
            "is_file": bench(small.is_file, 200),
            "git_root_walk_50_dirs": bench(git_root_walk, 50),
            "rglob_220_entries": bench(lambda: list(tree.rglob("*")), 10),
            # messaging.py:701 start_whatsapp_onboarding — тело целиком
            "creds_json_exists": bench(lambda: (existing_dir / "creds.json").exists(), 200),
            "str_replace_on_isoformat": bench(lambda: "2026-11-23T13:00:00+00:00".replace("+00:00", "Z"), 200),
            "session_rename_plus_mkdir": bench(session_backup, 20),
        },
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    pathlib.Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
