"""A/B the handler offload with the SAME stand on the SAME box, toggling the kill switch.

``bench_curve.py``/``bench_loop.py`` inherit the parent environment (they only clear
``HERMES_HOME``), so exporting ``HERMES_HANDLER_OFFLOAD`` in this process flips the server
under test between:

  off → ``inline``   : the pre-change behaviour (blocking call on the event loop)
  on  → ``threaded`` : the shipped behaviour (bounded worker pool)

Alternating the pair several times is what separates the change from the box's background
load: absolute numbers drift between runs, the per-pair ratio does not.

Usage: python3 ab_offload.py --reps 2 --out durable/t_a61becbc/ab-curve.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = os.environ.get("HERMES_PYTHON") or sys.executable


def _run(script: str, out_path: Path, env_offload: str, extra: list[str]) -> dict:
    env = dict(os.environ)
    env["HERMES_HANDLER_OFFLOAD"] = env_offload
    env.pop("HERMES_HOME", None)
    proc = subprocess.run([PY, str(HERE / script), "--out", str(out_path), *extra],
                          env=env, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise SystemExit(f"{script} (offload={env_offload}) failed rc={proc.returncode}\n"
                         f"{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return json.loads(out_path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default="bench_curve.py")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", required=True)
    ap.add_argument("extra", nargs="*", help="extra args passed to the stand (e.g. --sessions 400)")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="t-a61-ab-"))
    runs: list[dict] = []
    for rep in range(args.reps):
        for mode, flag in (("inline", "0"), ("threaded", "1")):
            out_path = tmp / f"{Path(args.script).stem}-{mode}-{rep}.json"
            data = _run(args.script, out_path, flag, args.extra)
            runs.append({"rep": rep, "mode": mode, "data": data})
            print(f"[rep {rep}] {args.script} mode={mode} done", flush=True)

    # ── reduce ────────────────────────────────────────────────────────────────
    if "curve" in args.script:
        summary: dict = {}
        for mode in ("inline", "threaded"):
            rows = [r["data"]["routes"] for r in runs if r["mode"] == mode]
            summary[mode] = {
                route: {
                    "n8_p50_ms": [row[route]["8"]["p50_ms"] for row in rows],
                    "n8_max_ms": [row[route]["8"]["max_ms"] for row in rows],
                    "n8_p50_median": round(statistics.median(
                        [row[route]["8"]["p50_ms"] for row in rows]), 1),
                    "n8_max_median": round(statistics.median(
                        [row[route]["8"]["max_ms"] for row in rows]), 1),
                }
                for route in rows[0]
            }
        result = {"kind": "curve", "reps": args.reps, "per_mode": summary,
                  "per_rep": [{"rep": r["rep"], "mode": r["mode"],
                               "routes": {k: {"p50": v["8"]["p50_ms"], "max": v["8"]["max_ms"]}
                                          for k, v in r["data"]["routes"].items()}}
                              for r in runs]}
    else:
        summary = {}
        for mode in ("inline", "threaded"):
            rows = [r["data"]["routes"] for r in runs if r["mode"] == mode]
            summary[mode] = {
                route: {
                    "loop_p99_ms": [row[route]["loop_lag"]["p99_ms"] for row in rows],
                    "loop_max_ms": [row[route]["loop_lag"]["max_ms"] for row in rows],
                    "over_50ms": [row[route]["loop_lag"]["over_50ms"] for row in rows],
                    "lat_p50_ms": [row[route]["lat_p50_ms"] for row in rows],
                    "loop_p99_median": round(statistics.median(
                        [row[route]["loop_lag"]["p99_ms"] for row in rows]), 2),
                    "loop_max_median": round(statistics.median(
                        [row[route]["loop_lag"]["max_ms"] for row in rows]), 2),
                }
                for route in rows[0]
            }
        result = {"kind": "loop", "reps": args.reps, "per_mode": summary,
                  "per_rep": [{"rep": r["rep"], "mode": r["mode"], "routes": r["data"]["routes"]}
                              for r in runs]}

    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result["per_mode"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
