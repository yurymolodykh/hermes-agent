"""Even load across the card keys, measured in busy seconds (not hand-outs).

The card body asks for one thing: over a day of dispatch, the seven card keys
must end up with comparable *occupied time* (``busy_seconds``), not merely a
comparable number of cards. These tests drive the real state machine
(``reserve_for_card`` / ``release_for_task`` / ``sweep``) rather than the
picker in isolation, because the metric is only meaningful if it is charged
from every place a slot disappears.

Fairness here is deliberately about *occupancy*: a key that served one
three-hour card was busier than a key that served four one-minute cards, and
picking by card count would hand the next long card to the key that already
carried the long one.

Time comes from a fake clock installed as the module's ``time`` name (not by
patching the global ``time`` module), so the Moscow-day rollover and the TTL
clamp run at real epochs without freezing anything else.
"""

from __future__ import annotations

import json
import time as real_time
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from agent import kanban_key_pool as kp

MSK = timezone(timedelta(hours=3))
CARD_KEYS = [2, 3, 4, 5, 6, 7, 8]


def msk(hh: int, mm: int = 0, day: int = 15) -> float:
    return datetime(2026, 3, day, hh, mm, tzinfo=MSK).timestamp()


class Clock:
    """Stands in for the module's ``time`` import: only ``time()`` is driven."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name):  # monotonic/sleep/strftime/... stay real
        return getattr(real_time, name)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A real pool: seven card keys, one card per key, isolated state file."""
    keys = [{"id": 1, "label": "background", "role": "background",
             "api_key": "KEY_BG_111"}]
    for kid in CARD_KEYS:
        keys.append({"id": kid, "label": f"account-{kid}", "role": "card",
                     "api_key": f"KEY_CARD_{kid}_222"})
    key_file = tmp_path / "kanban-keypool.json"
    key_file.write_text(json.dumps({
        "version": 1,
        "enabled": True,
        "base_url": "https://api.example.test/v1",
        "provider": "letovo",
        "background_key_id": 1,
        "max_cards_per_key": 1,
        "keys": keys,
    }), encoding="utf-8")
    state_file = tmp_path / "kanban-keypool.state.json"
    # Distinct names: a class body assigning ``key_file = key_file`` reads the
    # right-hand side as a class-local lookup and raises NameError.
    key_path, liveness_box = key_file, {"alive": True}

    monkeypatch.setenv(kp.ENV_KEYPOOL, str(key_file))
    monkeypatch.setenv(kp.ENV_KEYPOOL_STATE, str(state_file))
    monkeypatch.delenv(kp.ENV_MAX_CARDS_PER_KEY, raising=False)
    kp.reset_process_cache()

    clock = Clock(msk(9, 0))
    monkeypatch.setattr(kp, "time", clock)
    # The fake pids are not real processes; the sweep needs them to look dead.
    monkeypatch.setattr(kp, "_pid_alive", lambda pid: liveness_box["alive"])

    class Pool:
        keys = CARD_KEYS
        path = state_file
        key_file = key_path
        now = clock
        liveness = liveness_box

        @staticmethod
        def at(hh, mm=0, day=15):
            clock.now = msk(hh, mm, day)
            return clock.now

        @staticmethod
        def reserve(task_id):
            return kp.reserve_for_card(task_id, run_id=1, pid=1000)

        @staticmethod
        def release(task_id):
            return kp.release_for_task(task_id, pid=999, defer_if_holder=False)

        @staticmethod
        def state():
            return kp._read_state(state_file)

    yield Pool()
    kp.reset_process_cache()


def slot_of(state, kid):
    """The single slot record a state entry holds, in whichever form it is."""
    entry = state["reservations"][str(kid)]
    return entry["slots"][0] if "slots" in entry else entry


# ── accounting ─────────────────────────────────────────────────────────────


def test_busy_time_is_charged_when_the_slot_is_released(pool):
    """A card held for 20 minutes adds 20 minutes to its key's bucket."""
    pool.at(9, 0)
    reservation = pool.reserve("card-a")

    pool.at(9, 20)
    pool.release("card-a")

    record = pool.state()["usage"][str(reservation.key_id)]
    assert record["cards"] == 1
    assert record["busy_seconds"] == pytest.approx(20 * 60, abs=1)
    assert record["window"] == "2026-03-15"


def test_busy_time_is_clamped_to_the_ttl(pool):
    """A corrupt/clock-jumped ``reserved_at`` cannot invent hours of usage."""
    pool.at(9, 0)
    reservation = pool.reserve("card-b")
    state = pool.state()
    slot_of(state, reservation.key_id)["reserved_at"] = 1  # epoch
    pool.path.write_text(json.dumps(state), encoding="utf-8")

    pool.at(9, 5)
    pool.release("card-b")

    charged = pool.state()["usage"][str(reservation.key_id)]["busy_seconds"]
    assert charged == pytest.approx(kp._ttl_seconds(), abs=1)


def test_negative_elapsed_time_never_subtracts_usage(pool):
    """A clock that moved backwards charges 0, not a negative amount."""
    pool.at(12, 0)
    reservation = pool.reserve("card-c")
    pool.at(11, 0)  # earlier than the reservation
    pool.release("card-c")

    record = pool.state()["usage"][str(reservation.key_id)]
    assert record["busy_seconds"] == 0.0
    assert record["cards"] == 1


def test_release_is_not_charged_twice(pool):
    """Releasing an already-released card must not add a second charge."""
    pool.at(9, 0)
    reservation = pool.reserve("card-d")
    pool.at(9, 30)
    pool.release("card-d")
    once = dict(pool.state()["usage"][str(reservation.key_id)])

    pool.at(10, 0)
    assert pool.release("card-d") == []  # nothing left to release
    twice = pool.state()["usage"][str(reservation.key_id)]

    assert twice["busy_seconds"] == once["busy_seconds"]
    assert twice["cards"] == once["cards"] == 1


def test_sweep_charges_stale_slots_once(pool):
    """The sweep path charges too — a crashed card's time must not be lost."""
    pool.at(9, 0)
    reservation = pool.reserve("card-e")
    kid = reservation.key_id
    pool.liveness["alive"] = False  # the worker crashed

    pool.at(9, 30)
    assert kp.sweep()  # reaps the slot
    first = pool.state()["usage"][str(kid)]["busy_seconds"]

    pool.at(9, 40)
    kp.sweep()
    second = pool.state()["usage"][str(kid)]["busy_seconds"]

    assert first == pytest.approx(30 * 60, abs=1)
    assert second == first  # the slot is gone; nothing left to charge


# ── the Moscow day window ─────────────────────────────────────────────────


def test_usage_resets_when_the_moscow_day_rolls_over(pool):
    """Yesterday's imbalance must not keep steering today's rotation."""
    pool.at(23, 0, day=15)
    reservation = pool.reserve("card-f")
    pool.at(23, 50, day=15)
    pool.release("card-f")
    assert pool.state()["usage"][str(reservation.key_id)]["busy_seconds"] > 0

    # 00:05 the next Moscow day — the bucket belongs to the new date: empty.
    pool.at(0, 5, day=16)
    assert kp._read_state(pool.path)["usage"] == {}

    pool.reserve("card-g")
    pool.at(0, 10, day=16)
    pool.release("card-g")
    windows = {rec["window"] for rec in pool.state()["usage"].values()}
    assert windows == {"2026-03-16"}


def test_day_boundary_is_moscow_not_utc():
    """22:30 UTC is already 01:30 tomorrow in Moscow — the bucket must roll."""
    late_utc = datetime(2026, 3, 15, 22, 30, tzinfo=timezone.utc).timestamp()
    assert kp._usage_today(late_utc) == "2026-03-16"
    assert kp._usage_today(msk(23, 59, day=15)) == "2026-03-15"
    assert kp._usage_today(msk(0, 0, day=16)) == "2026-03-16"


def test_picker_ignores_yesterdays_bucket(pool):
    """A stale bucket must not bias today's pick in either direction."""
    state = {
        "version": 2,
        "reservations": {},
        "failed_keys": {},
        "rr_cursor": None,
        "usage": {"2": {"busy_seconds": 99999.0, "cards": 50, "window": "2026-03-14"}},
    }
    pool.path.write_text(json.dumps(state), encoding="utf-8")

    pool.at(9, 0, day=15)
    reservation = pool.reserve("card-today")
    # Stale bucket dropped → key #2 is a normal candidate and the ring starts
    # from the lowest id exactly as a fresh pool does.
    assert reservation.key_id == 2


# ── picker behaviour ───────────────────────────────────────────────────────


def test_forty_hand_outs_spread_evenly_over_seven_keys(pool):
    """40 equal-length cards over 7 keys → at most one card of spread."""
    pool.at(9, 0)
    for index in range(40):
        task = f"card-{index}"
        pool.reserve(task)
        pool.now.now += 300  # every card is five minutes long
        pool.release(task)

    usage = pool.state()["usage"]
    counts = [usage[str(kid)]["cards"] for kid in CARD_KEYS]
    assert sum(counts) == 40
    assert max(counts) - min(counts) <= 1, counts


def test_busy_time_itself_stays_even(pool):
    """The same 40 cards: busy seconds must end within one card's length.

    Equal card *counts* alone already pass the previous test; this one pins
    the metric the owner actually asked for.
    """
    pool.at(9, 0)
    for index in range(40):
        task = f"card-{index}"
        pool.reserve(task)
        pool.now.now += 300
        pool.release(task)

    usage = pool.state()["usage"]
    busy = [usage[str(kid)]["busy_seconds"] for kid in CARD_KEYS]
    assert max(busy) - min(busy) <= 300 + 1, busy


def test_key_that_carried_the_long_card_gets_the_next_one_last(pool):
    """Occupancy, not count: one 3h card outranks four 1-minute cards."""
    pool.at(9, 0)
    long_key = pool.reserve("long").key_id

    # Four short cards go round the remaining keys.
    for index in range(4):
        pool.now.now += 60
        pool.reserve(f"short-{index}")
        pool.now.now += 60
        pool.release(f"short-{index}")

    # The long card closes with three hours of occupancy on its key.
    pool.at(12, 0)
    pool.release("long")

    usage = pool.state()["usage"]
    assert usage[str(long_key)]["busy_seconds"] == pytest.approx(3 * 3600, abs=60)
    others = [kid for kid in CARD_KEYS if kid != long_key]
    assert max(usage.get(str(kid), {}).get("busy_seconds", 0.0) for kid in others) < 3 * 3600 - 60

    # The next card must not go to the key that just carried three hours.
    pool.at(12, 5)
    picks = []
    for index in range(len(others)):
        picks.append(pool.reserve(f"next-{index}").key_id)
        pool.now.now += 10
    assert long_key not in picks, picks


def test_within_the_tolerance_the_ring_keeps_turning(pool):
    """A ≤60 s difference is noise: round-robin decides, not the metric."""
    pool.at(9, 0)
    order = []
    for index in range(7):
        order.append(pool.reserve(f"card-{index}").key_id)
        pool.now.now += 5  # five seconds each — all inside the tolerance
        pool.release(f"card-{index}")
        pool.now.now += 5

    assert order == CARD_KEYS, order


# ── reporting and backward compatibility ──────────────────────────────────


def test_status_reports_busy_seconds_and_cards(pool):
    """`keypool status` must show the number the picker balances on."""
    pool.at(9, 0)
    reservation = pool.reserve("card-s")
    pool.at(9, 45)
    pool.release("card-s")

    status = kp.pool_status()
    row = next(r for r in status["keys"] if r["key_id"] == reservation.key_id)
    assert row["busy_seconds"] == pytest.approx(45 * 60, abs=1)
    assert row["cards"] == 1
    assert row["usage_window"] == "2026-03-15"
    # Every card key is present, busy or not.
    assert {r["key_id"] for r in status["keys"]} == set(CARD_KEYS)


def test_state_file_without_usage_still_loads(pool):
    """A v2 state written before this change reads exactly as before."""
    state = {"version": 2, "reservations": {}, "failed_keys": {}, "rr_cursor": 5}
    pool.path.write_text(json.dumps(state), encoding="utf-8")

    loaded = kp._read_state(pool.path)
    assert loaded["rr_cursor"] == 5
    assert loaded["usage"] == {}
    assert loaded["reservations"] == {}


def test_legacy_flat_reservation_survives_a_release(pool):
    """A v1/v2-flat reservation is still released, and now also accounted."""
    now = pool.at(9, 0)
    state = {
        "version": 2,
        "reservations": {
            "3": {
                "key_id": 3,
                "key_label": "k3",
                "task_id": "legacy-card",
                "run_id": 1,
                "pid": 1000,
                "host": "test",
                "reserved_at": now,
                "expires_at": int(now + 3600),
                "status": "active",
            }
        },
        "failed_keys": {},
        "rr_cursor": 3,
    }
    pool.path.write_text(json.dumps(state), encoding="utf-8")

    pool.at(9, 10)
    released = pool.release("legacy-card")

    assert [r["key_id"] for r in released] == [3]
    assert pool.state()["usage"]["3"]["busy_seconds"] == pytest.approx(600, abs=1)
