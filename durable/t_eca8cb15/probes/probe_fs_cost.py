#!/usr/bin/env python3
"""Почему порог §1.3 именно такой: цена операций на этой машине.

    python3 durable/t_eca8cb15/probes/probe_fs_cost.py [--json out.json]

Отвечает на вопрос «какие обращения к файловой системе считаем достаточно
дорогими, чтобы выносить их из цикла событий». Всё считается здесь, не руками.

Колонки: медиана и максимум по 5–20 повторениям, в мкс/мс. Запись 8 МиБ —
потолок `_FS_TEXT_WRITE_MAX_BYTES` (`web_routers/files.py:129`); 25 МиБ — потолок
загрузки аудио (`audio.py:52`).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import tempfile
import time

REPEATS = 20


def bench(fn, repeats: int = REPEATS) -> dict:
    samples: list[float] = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000.0)  # ms
    return {"p50_ms": round(statistics.median(samples), 4),
            "max_ms": round(max(samples), 4),
            "n": repeats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None,
                                         prefix="probe-fs-cost-"))
    small = work / "small.txt"
    small.write_bytes(b"0" * 4096)
    payload8 = "x" * (8 * 1024 * 1024)
    payload25 = b"x" * (25 * 1024 * 1024)
    tree = work / "tree"
    for i in range(20):
        d = tree / f"d{i}"
        d.mkdir(parents=True)
        for j in range(10):
            (d / f"f{j}").write_text("x")

    def write8() -> None:
        tmp = work / "e.tmp"
        tmp.write_text(payload8, encoding="utf-8")
        os.replace(tmp, work / "eight.txt")

    def write25() -> None:
        (work / "25.bin").write_bytes(payload25)

    out = {
        "tmpdir": str(work),
        "threshold_ms": 1.0,
        "note": "порог: операции дороже 1 мс считаются блокирующими; "
                "stat/exists/is_file (~0,003 мс) — нет",
        "ops": {
            "stat_4k": bench(small.stat, 200),
            "exists": bench(small.exists, 200),
            "is_file": bench(small.is_file, 200),
            "read_text_4k": bench(lambda: small.read_text(), 200),
            "read_text_1MiB": bench(lambda: (work / "one.bin").write_bytes(b"y" * (1 << 20))
                                    or (work / "one.bin").read_bytes(), 20),
            "write_text_8MiB_plus_replace": bench(write8, 5),
            "write_bytes_25MiB": bench(write25, 5),
            "rglob_220_entries": bench(lambda: list(tree.rglob("*")), 10),
            "unlink": bench(lambda: (work / "u").write_text("x") or (work / "u").unlink(), 20),
        },
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.json:
        pathlib.Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
