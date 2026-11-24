#!/usr/bin/env python3
"""Re-measure the minute calibration behind ``agent/kanban_card_sizing.py``.

The rule's p50/p80 tables are constants on purpose (no DB read at runtime), so the only
thing keeping them honest is that the measurement is re-runnable in one command. This
script is that command; it prints the table the module docstring quotes, plus the sha1 of
the row set so a re-run can be compared byte-for-byte with the recorded one.

Run from the repo root:

    python3 measure-card-sizing-calibration.py [--hi "2026-11-23 21:30"]

The window is closed at both ends and the upper bound must lie in the past: an open or
future bound lets a run still in flight at the edge join a later measurement, which is
exactly the drift (754 -> 763 -> 766 -> 776) the pinned window exists to stop.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent.kanban_card_sizing import (
    CALIBRATION_P50_MINUTES,
    CALIBRATION_P80_MINUTES,
    count_acceptance_items,
)

DEFAULT_DB = "/home/hermes/.hermes/kanban.db"
DEFAULT_LO = "2026-11-17 00:00"
DEFAULT_HI = "2026-11-23 21:30"

SQL = """SELECT tr.started_at, tr.ended_at, t.body
  FROM task_runs AS tr
  JOIN tasks AS t ON t.id = tr.task_id
 WHERE tr.started_at >= :lo
   AND tr.started_at <  :hi
   AND tr.ended_at IS NOT NULL
   AND tr.ended_at <= :hi
   AND tr.ended_at - tr.started_at > 0
 ORDER BY tr.id"""


def unix(stamp: str) -> int:
    """'YYYY-MM-DD HH:MM' read as UTC."""
    moment = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=dt.UTC)
    return int(moment.timestamp())


def pct(values: list[int], p: int) -> int:
    """Nearest-rank percentile: sorted(v)[max(1, ceil(p/100 * n)) - 1]."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100.0 * len(ordered))) - 1]


def whole_minutes(seconds: int) -> int:
    """Seconds to whole minutes, half up."""
    return math.floor(seconds / 60.0 + 0.5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--lo", default=DEFAULT_LO)
    ap.add_argument("--hi", default=DEFAULT_HI)
    args = ap.parse_args(argv)

    lo, hi = unix(args.lo), unix(args.hi)
    now = dt.datetime.now(dt.UTC)
    if hi >= now.timestamp():
        print(
            f"refusing: upper bound {args.hi} UTC is not in the past "
            f"(now {now:%Y-%m-%d %H:%M} UTC); "
            "an open window is not reproducible",
            file=sys.stderr,
        )
        return 2

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = list(con.execute(SQL, {"lo": lo, "hi": hi}))
    if not rows:
        print("no rows in the window", file=sys.stderr)
        return 1

    buckets: dict[int, list[int]] = {}
    for started, ended, body in rows:
        buckets.setdefault(count_acceptance_items(body), []).append(ended - started)

    unrealised = [x for v in buckets.values() for x in v]
    print(f"window {args.lo} .. {args.hi} UTC, both ends closed, n={len(rows)}")
    print(f"row-set sha1 {hashlib.sha1(repr(rows).encode('utf-8')).hexdigest()[:16]}")
    over_60 = round(100 * sum(x > 3600 for x in unrealised) / len(unrealised))
    over_50 = round(100 * sum(x > 3000 for x in unrealised) / len(unrealised))
    print(
        f"whole sample: median {whole_minutes(pct(unrealised, 50))} min, "
        f"p80 {whole_minutes(pct(unrealised, 80))} min, >60 {over_60} %, >50 {over_50} %"
    )
    print("items | n   | p50 s | p50 min | p80 s | p80 min | >60 % | module p50/p80")
    drift = 0
    for items in sorted(buckets):
        v = buckets[items]
        p50, p80 = pct(v, 50), pct(v, 80)
        p50m, p80m = whole_minutes(p50), whole_minutes(p80)
        module = (
            f"{CALIBRATION_P50_MINUTES.get(items, '-')}/"
            f"{CALIBRATION_P80_MINUTES.get(items, '-')}"
        )
        quoted = items in CALIBRATION_P50_MINUTES
        if quoted and (
            p50m != CALIBRATION_P50_MINUTES[items]
            or p80m != CALIBRATION_P80_MINUTES[items]
        ):
            drift += 1
            module += "  <- MISMATCH"
        print(
            f"{items:5d} | {len(v):3d} | {p50:5d} | {p50m:7d} | {p80:5d} | {p80m:7d} "
            f"| {round(100 * sum(x > 3600 for x in v) / len(v)):4d}% | {module}"
        )
    b7 = [x for k, v in buckets.items() if k >= 7 for x in v]
    if b7:
        print(
            f"7+: n={len(b7)} p50={pct(b7, 50) / 60:.1f} p80={pct(b7, 80) / 60:.1f} "
            "(held at the six-item point on purpose: fan-out parents measure lower)"
        )
    if drift:
        print(
            f"\n{drift} quoted bucket(s) disagree with the module — update the constants"
        )
        return 1
    print("\nall quoted buckets match the module constants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
