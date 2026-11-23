#!/usr/bin/env python3
"""Положение выключателя оффлоада: env → config → default, и какое имя ключа читается.

    python3 durable/t_eca8cb15/probes/probe_offload_switch.py [--json …]

Зачем. §4 и §6 записки утверждают, что откат — это одна настройка, и что читается именно
`dashboard.handler_offload`, а не `dashboard.parallelism`. Оба утверждения проверяются здесь,
на копии модуля из целевой ветки, без запуска шлюза и без записи в чужой профиль: каждая
комбинация собирается во временном `HERMES_HOME`.

Модуль берётся из git (`git show <rev>:hermes_cli/web_routers/handler_offload.py`), потому что
он живёт в целевой ветке и может отсутствовать в рабочем дереве карточки.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
MODULE_REL = "hermes_cli/web_routers/handler_offload.py"


def load_module(rev: str, dest: pathlib.Path):
    source = subprocess.run(
        ["git", "show", f"{rev}:{MODULE_REL}"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    dest.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_ho_probe", dest)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {dest}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(module, home: pathlib.Path, env_value: str | None) -> dict:
    """Settle a fresh switch against ``home`` and return its snapshot."""
    os.environ["HERMES_HOME"] = str(home)
    if env_value is None:
        os.environ.pop(module.ENV_KEY, None)
    else:
        os.environ[module.ENV_KEY] = env_value
    switch = module.HandlerOffload().install()
    snap = switch.snapshot()
    switch.shutdown()
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default="feature/multiuser-access", help="ревизия, откуда взять модуль")
    ap.add_argument("--json", help="куда записать JSON")
    args = ap.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(prefix="probe-offload-switch-"))
    module = load_module(args.rev, work / "handler_offload.py")

    cases = [
        ("default (ни ключа, ни env)", None, None),
        ("env HERMES_HANDLER_OFFLOAD=0", None, "0"),
        ("env HERMES_HANDLER_OFFLOAD=1", None, "1"),
        ("config dashboard.handler_offload=false", "dashboard:\n  handler_offload: false\n", None),
        ("config dashboard.handler_offload=true", "dashboard:\n  handler_offload: true\n", None),
        ("config true + workers=3", "dashboard:\n  handler_offload: true\n  handler_offload_workers: 3\n", None),
        ("config parallelism.enabled=false (ИМЯ, КОТОРОЕ НЕ ЧИТАЕТСЯ)",
         "dashboard:\n  parallelism:\n    enabled: false\n", None),
        ("config false, но env=1 (env сильнее)", "dashboard:\n  handler_offload: false\n", "1"),
    ]

    rows = []
    for label, yaml_text, env_value in cases:
        home = pathlib.Path(tempfile.mkdtemp(prefix="home-", dir=work))
        if yaml_text:
            (home / "config.yaml").write_text(yaml_text, encoding="utf-8")
        snap = snapshot(module, home, env_value)
        rows.append({"case": label, "config": (yaml_text or "").strip() or None,
                     "env": env_value, **snap})
        print(f"{label:58s} mode={snap['mode']:8s} enabled={snap['enabled']!s:5s} workers={snap['workers']}")

    out = {"rev": args.rev, "module": MODULE_REL, "cases": rows}
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
