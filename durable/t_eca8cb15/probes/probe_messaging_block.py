#!/usr/bin/env python3
"""Цена тела `start_whatsapp_onboarding` (messaging.py:701): что там реально блокирует.

    python3 durable/t_eca8cb15/probes/probe_messaging_block.py

Проверяющий указал, что `.replace` в теле (строка 714) — строковый метод
(`datetime.isoformat().replace("+00:00","Z")`), а не замена файла. Скрипт
показывает, какие вызовы тела действительно трогают диск и сколько они стоят.
"""

from __future__ import annotations

import json
import os
import pathlib
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

REPEATS = 50


def bench(fn, repeats: int = REPEATS) -> dict:
    s: list[float] = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        s.append((time.perf_counter() - t) * 1000.0)
    return {"p50_ms": round(statistics.median(s), 4), "max_ms": round(max(s), 4)}


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None,
                                       prefix="probe-whatsapp-"))
    session = tmp / "platforms/whatsapp/session"
    session.mkdir(parents=True)
    (session / "creds.json").write_text(json.dumps({"me": {"id": "1@s.whatsapp.net"}}))
    lock_dir = tmp / "status"
    lock_dir.mkdir()
    (lock_dir / "whatsapp-session.json").write_text(
        json.dumps({"scope": "whatsapp-session", "pid": os.getpid()}))

    out = {"tmpdir": str(tmp), "ops": {}}
    # строковый replace, которым §1.3 первой редакции называла «запись файла»
    out["ops"]["str_replace_on_isoformat"] = bench(lambda: (
        "2026-11-23T13:00:00+00:00".replace("+00:00", "Z")))
    out["ops"]["creds_json_exists"] = bench(lambda: (session / "creds.json").exists())
    out["ops"]["creds_json_read_text"] = bench(
        lambda: (session / "creds.json").read_text(encoding="utf-8-sig"))
    out["ops"]["lstat_plus_isdir_plus_iterdir"] = bench(
        lambda: (session.lstat(), session.is_dir(), next(session.iterdir(), None)))
    out["ops"]["write_and_unlink_lock_file"] = bench(
        lambda: ((lock_dir / "x.lock").write_text("{}") or (lock_dir / "x.lock").unlink()), 20)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    pathlib.Path(__file__).with_name("messaging_block.json").write_text(text + "\n",
                                                                       encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
