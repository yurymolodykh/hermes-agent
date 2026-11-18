"""Overhead of the boundary, measured per command, on this host.

The question the criterion asks is not "is the shim fast" but "does enabling the boundary make
the terminal tool noticeably slower". So the comparison is the SAME workload with
``acl.enabled=false`` and ``acl.enabled=true`` through ``LocalEnvironment.execute`` — the path an
agent uses — not ``/bin/true`` in isolation.

Reported: median and minimum over N runs, plus the delta. A median is used because the minimum
hides the scheduling cost that shows up in a real spawn, and a single run on this host varies by
tens of percent under load.

Run: python3 bench_overhead.py [N]
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(os.environ.get("ACL_REPO") or ".")
PY = sys.executable

BODY = r'''
import json, os, sys, statistics, time
sys.path.insert(0, "@@REPO@@")
from tools.environments.local import LocalEnvironment

env = LocalEnvironment(cwd="@@WS@@")
N = @@N@@

def bench(cmd, n):
    # one warm-up so the measurement is of steady state, not of the first import/spawn
    env.execute("true", timeout=30)
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = env.execute(cmd, timeout=60)
        dt = (time.perf_counter() - t0) * 1000.0
        if r.get("returncode") != 0:
            times.append(None)
        else:
            times.append(dt)
    good = [t for t in times if t is not None]
    return {
        "n": n,
        "ok": len(good),
        "median_ms": round(statistics.median(good), 2) if good else None,
        "min_ms": round(min(good), 2) if good else None,
        "max_ms": round(max(good), 2) if good else None,
    }

out = {
    "true": bench("true", N),
    "echo": bench("echo hi", N),
    "python_version": bench("@@PY@@ -c 'pass'", max(5, N // 3)),
}
print(json.dumps(out))
'''


def run_case(acl_on: bool, n: int) -> dict:
    home = Path(tempfile.mkdtemp(prefix=f"bench-{'on' if acl_on else 'off'}-"))
    (home / "config.yaml").write_text(f"acl:\n  enabled: {'true' if acl_on else 'false'}\n",
                                      encoding="utf-8")
    ws = Path(tempfile.mkdtemp(prefix="bench-ws-"))
    script = (BODY.replace("@@REPO@@", str(REPO.resolve()))
                  .replace("@@WS@@", str(ws))
                  .replace("@@N@@", str(n))
                  .replace("@@PY@@", PY))
    path = home / "bench.py"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([PY, str(path)], capture_output=True, text=True, timeout=1800, check=False,
                          cwd=str(REPO), env={**os.environ, "HERMES_HOME": str(home)})
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        data = {"error": proc.stderr[-2000:]}
    data["acl"] = acl_on
    return data


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    off = run_case(False, n)
    on = run_case(True, n)

    print(json.dumps({"acl_off": off, "acl_on": on, "iterations": n}, indent=2,
                     ensure_ascii=False))

    print("\n== per-command overhead (median)")
    rows = []
    for key in ("true", "echo", "python_version"):
        a = (off.get(key) or {}).get("median_ms")
        b = (on.get(key) or {}).get("median_ms")
        if a is None or b is None:
            rows.append((key, a, b, None))
            print(f"{key}: incomplete off={a} on={b}")
            continue
        delta = round(b - a, 2)
        ratio = round(b / a, 2) if a else None
        rows.append((key, a, b, delta))
        print(f"{key}: off {a} ms -> on {b} ms  delta +{delta} ms  ({ratio}x)")

    print("\nREFERENCE: the plan's probe measured a native shim at +0.41 ms on /bin/true.")
    deltas = [r[3] for r in rows if r[3] is not None]
    worst = max(deltas) if deltas else None
    print(f"worst delta across commands: +{worst} ms")
    verdict = worst is not None and worst < 10.0
    print("VERDICT:", "acceptable (<10 ms per command)" if verdict else "TOO SLOW — needs attention")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
