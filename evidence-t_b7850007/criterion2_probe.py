"""Criterion 2 probes for card t_b7850007 — run on the production interpreter.

Usage:
    /home/hermes/.hermes/hermes-agent/.venv/bin/python evidence-t_b7850007/criterion2_probe.py

Nothing here writes to the live ``~/.hermes`` tree: the pool state/key files are
temp files and the window probe only *reads* config through ``_load_settings``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent import kanban_key_pool as kp  # noqa: E402
from hermes_cli import kanban_db_day_window as daywin  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
CARD_KEYS = [2, 3, 4, 5, 6, 7, 8]


def at(hh, mm, day=16):
    return datetime(2026, 3, day, hh, mm, tzinfo=MSK).timestamp()


def probe_window() -> int:
    live = daywin._load_settings()
    print("live _load_settings() ->", {
        "day_max_in_progress": live[0],
        "window": [t.strftime("%H:%M") for t in live[1]] if live[1] else None,
        "tz": getattr(live[2], "key", live[2]),
    })
    print("  (shipped default is unset = no daytime layer until the owner sets kanban.day_*)")

    # The card's contract: day_max_in_progress=4, window 08:00-18:00 Europe/Moscow.
    Moscow = ZoneInfo("Europe/Moscow")
    settings = (4, (daywin.dt_time(8, 0), daywin.dt_time(18, 0)), Moscow)
    day_cap, window, tz = settings
    base_cap = 7
    failures = 0
    print(f"\nbase_cap={base_cap}  day_cap={day_cap}  "
          f"window={window[0]:%H:%M}-{window[1]:%H:%M}  tz={getattr(tz, 'key', tz)}")
    for hh, mm, expected in [(7, 59, 7), (8, 0, 4), (17, 59, 4), (18, 0, 7)]:
        daywin.reset_state()
        got = daywin.resolve_day_window_cap(base_cap, now=at(hh, mm), settings=settings)
        mark = "OK " if got == expected else "FAIL"
        failures += got != expected
        print(f"  {mark} {hh:02d}:{mm:02d} Europe/Moscow -> {got} (expected {expected})")
    daywin.reset_state()
    return failures


class Clock:
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def probe_fairness(active_keys=4, hand_outs=40, minutes=5) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kp-criterion2-"))
    keys = [{"id": 1, "label": "background", "role": "background",
             "api_key": "KEY_BG_111"}]
    for kid in CARD_KEYS:
        keys.append({"id": kid, "label": f"account-{kid}", "role": "card",
                     "api_key": f"KEY_CARD_{kid}_222"})
    key_file = tmp / "kanban-keypool.json"
    key_file.write_text(json.dumps({
        "version": 1, "enabled": True, "base_url": "https://api.example.test/v1",
        "provider": "letovo", "background_key_id": 1,
        "max_cards_per_key": 1, "keys": keys,
    }), encoding="utf-8")
    state_file = tmp / "kanban-keypool.state.json"
    # Only the first `active_keys` card keys are usable; the rest are quarantined
    # away as "dead keys", which is how the pool excludes a key from `free`.
    dead = CARD_KEYS[active_keys:]
    state_file.write_text(json.dumps({
        "version": 2, "reservations": {}, "rr_cursor": None, "usage": {},
        "failed_keys": {str(k): {"reason": "dead", "quarantine_until": 0,
                                 "status_code": 401} for k in dead},
    }), encoding="utf-8")

    os.environ[kp.ENV_KEYPOOL] = str(key_file)
    os.environ[kp.ENV_KEYPOOL_STATE] = str(state_file)
    os.environ.pop(kp.ENV_MAX_CARDS_PER_KEY, None)
    kp.reset_process_cache()

    clock = Clock(at(9, 0))
    kp.time = clock          # type: ignore[assignment]
    kp._usage.time = clock   # type: ignore[assignment]
    kp._pid_alive = lambda pid: True  # type: ignore[assignment]

    try:
        for index in range(hand_outs):
            task = f"card-{index}"
            kp.reserve_for_card(task, run_id=1, pid=1000)
            clock.now += minutes * 60
            kp.release_for_task(task, pid=999, defer_if_holder=False)
        usage = json.loads(state_file.read_text(encoding="utf-8"))["usage"]
    finally:
        kp.reset_process_cache()
        for var in (kp.ENV_KEYPOOL, kp.ENV_KEYPOOL_STATE):
            os.environ.pop(var, None)

    live = CARD_KEYS[:active_keys]
    counts = {kid: int(usage[str(kid)]["cards"]) for kid in live}
    busy = {kid: round(float(usage[str(kid)]["busy_seconds"]), 1) for kid in live}
    spread = max(counts.values()) - min(counts.values())
    print(f"\n{hand_outs} hand-outs over {active_keys} of 7 keys "
          f"({minutes} min each; quarantined: {dead}):")
    for kid in live:
        print(f"  key #{kid}: cards={counts[kid]}  busy_seconds={busy[kid]}")
    print(f"  cards spread = {spread} (limit 1)  "
          f"total cards = {sum(counts.values())} / {hand_outs}")
    return 0 if spread <= 1 and sum(counts.values()) == hand_outs else 1


if __name__ == "__main__":
    print("=" * 72)
    print("1) day window on the production interpreter")
    print("=" * 72)
    w = probe_window()
    print()
    print("=" * 72)
    print("2) 40 hand-outs, 4 of 7 keys")
    print("=" * 72)
    f = probe_fairness()
    print()
    print(f"RESULT: window_failures={w} fairness_failures={f}")
    sys.exit(1 if (w or f) else 0)
