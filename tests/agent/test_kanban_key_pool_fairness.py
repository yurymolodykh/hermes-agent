"""Even load across the card keys, measured in busy seconds (not hand-outs).

The card body asks for one thing: over a day of dispatch, the seven card keys
must end up with comparable *occupied time* (``busy_seconds``), not merely a
comparable number of cards. These tests drive the real state machine
(``_read_state`` / ``_pick_key`` / ``_account_usage`` and the release paths)
rather than the picker in isolation, because the metric is only meaningful if
it is charged from every place a slot disappears.

Fairness here is deliberately about *occupancy*: a key that served one
three-hour card was busier than a key that served four one-minute cards, and
picking by card count would hand the next long card to the key that already
carried the long one.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from agent import kanban_key_pool as kp

MSK = timezone(timedelta(hours=3))
CARD_KEYS = [2, 3, 4, 5, 6, 7, 8]


def msk(hh: int, mm: int = 0, day: int = 15) -> float:
    return datetime(2026, 3, day, hh, mm, tzinfo=MSK).timestamp()


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A real pool: seven card keys, one card per key, isolated state file."""
    key_file = tmp_path / "keys.yaml"
    lines = ["version: 1", "background_key_id: 1", "max_cards_per_key: 1", "keys:"]
    lines.append("  - {id: 1, label: bg, api_key: sk-bg, role: background}")
    for kid in CARD_KEYS:
        lines.append(f"  - {{id: {kid}, label: k{kid}, api_key: sk-{kid}, role: card}}")
    key_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state_file = tmp_path / "state.json"

    monkeypatch.setenv(kp.ENV_KEYPOOL, str(key_file))
    monkeypatch.setenv(kp.ENV_KEYPOOL_STATE, str(state_file))
    monkeypatch.delenv(kp.ENV_MAX_CARDS_PER_KEY, raising=False)
    kp.reset_process_cache()

    class Pool:
        keys = CARD_KEYS
        path = state_file
        key_file = key_file

        @staticmethod
        def reserve(task_id, now, cards=1):
            return kp.reserve_for_card(
                task_id=task_id, run_id=f"run-{task_id}", pid=1000, host="test",
                now=now, cards=cards,
            )

        @staticmethod
        def release(task_id, now):
            return kp.release_for_task(task_id, reason="completed", now=now)

        @staticmethod
        def state():
            return kp._read_state(state_file)

    yield Pool()
    kp.reset_process_cache()


# ── accounting ─────────────────────────────────────────────────────────────


def test_busy_time_is_charged_when_the_slot_is_released(pool):
    """A card held for 20 minutes adds 20 minutes to its key's bucket."""
    reservation = pool.reserve("card-a", now=msk(9, 0))
    assert reservation is not None
    kid = reservation.key_id

    pool.release("card-a", now=msk(9, 20))

    record = pool.state()["usage"][str(kid)]
    assert record["cards"] == 1
    assert record["busy_seconds"] == pytest.approx(20 * 60, abs=1)
    assert record["window"] == "2026-03-15"


def test_busy_time_is_clamped_to_the_ttl(pool):
    """A corrupt/clock-jumped ``reserved_at`` cannot invent hours of usage."""
    reservation = pool.reserve("card-b", now=msk(9, 0))
    kid = reservation.key_id
    state = pool.state()
    state["reservations"][str(kid)][0]["reserved_at"] = 1  # epoch
    pool.path.write_text(json.dumps(state), encoding="utf-8")

    pool.release("card-b", now=msk(9, 5))

    assert pool.state()["usage"][str(kid)]["busy_seconds"] <= kp._ttl_seconds()


def test_negative_elapsed_time_never_subtracts_usage(pool):
    """A clock that moved backwards charges 0, not a negative amount."""
    reservation = pool.reserve("card-c", now=msk(12, 0))
    kid = reservation.key_id
    pool.release("card-c", now=msk(11, 0))  # earlier than the reservation

    assert pool.state()["usage"][str(kid)]["busy_seconds"] == 0.0
    assert pool.state()["usage"][str(kid)]["cards"] == 1


def test_release_is_not_charged_twice(pool):
    """Releasing an already-released card must not add a second charge."""
    reservation = pool.reserve("card-d", now=msk(9, 0))
    kid = reservation.key_id
    pool.release("card-d", now=msk(9, 30))
    once = pool.state()["usage"][str(kid)]

    pool.release("card-d", now=msk(10, 0))  # nothing left to release
    twice = pool.state()["usage"][str(kid)]

    assert twice["busy_seconds"] == once["busy_seconds"]
    assert twice["cards"] == once["cards"] == 1


def test_sweep_charges_stale_slots_once(pool):
    """The sweep path charges too — a crashed card's time must not be lost."""
    reservation = pool.reserve("card-e", now=msk(9, 0))
    kid = reservation.key_id
    later = msk(9, 0) + kp._ttl_seconds() + 60

    kp.sweep(now=later)
    first = pool.state()["usage"][str(kid)]["busy_seconds"]
    kp.sweep(now=later + 60)
    second = pool.state()["usage"][str(kid)]["busy_seconds"]

    assert first > 0
    assert second == first  # the slot is gone; nothing left to charge


# ── the day window ─────────────────────────────────────────────────────────


def test_usage_resets_when_the_moscow_day_rolls_over(pool):
    """Yesterday's imbalance must not keep steering today's rotation."""
    reservation = pool.reserve("card-f", now=msk(23, 0, day=15))
    kid = reservation.key_id
    pool.release("card-f", now=msk(23, 50, day=15))
    assert pool.state()["usage"][str(kid)]["busy_seconds"] > 0

    # 00:05 next day — the bucket belongs to the new date and is empty.
    state = kp._read_state(pool.path)
    assert state["usage"] == {}

    reservation = pool.reserve("card-g", now=msk(0, 5, day=16))
    pool.release("card-g", now=msk(0, 10, day=16))
    assert pool.state()["usage"][str(reservation.key_id)]["window"] == "2026-03-16"


def test_day_boundary_is_moscow_not_utc(pool):
    """22:30 UTC is already 01:30 tomorrow in Moscow — the bucket must roll."""
    reservation = pool.reserve("card-h", now=msk(20, 0, day=15))
    kid = reservation.key_id
    pool.release("card-h", now=msk(23, 30, day=15))
    assert str(kid) in pool.state()["usage"]

    late_utc = datetime(2026, 3, 15, 22, 30, tzinfo=timezone.utc).timestamp()
    assert kp._usage_today(late_utc) == "2026-03-16"
    assert kp._read_state(pool.path)["usage"] == {}


# ── picker behaviour ───────────────────────────────────────────────────────


def test_forty_hand_outs_spread_evenly_over_equivalent_keys(pool):
    """40 equal-length cards over 7 keys → at most one card of spread."""
    now = msk(9, 0)
    for index in range(40):
        task = f"card-{index}"
        reservation = pool.reserve(task, now=now)
        assert reservation is not None
        now += 300  # every card is five minutes long
        pool.release(task, now=now)

    usage = pool.state()["usage"]
    counts = [usage[str(kid)]["cards"] for kid in CARD_KEYS]
    assert sum(counts) == 40
    assert max(counts) - min(counts) <= 1, counts


def test_busy_time_itself_stays_even(pool):
    """The same 40 cards: busy seconds must end up within one card's length.

    Equal card *counts* alone would already pass the previous test; this one
    pins the metric the owner actually asked for.
    """
    now = msk(9, 0)
    for index in range(40):
        task = f"card-{index}"
        pool.reserve(task, now=now)
        now += 300
        pool.release(task, now=now)

    usage = pool.state()["usage"]
    busy = [usage[str(kid)]["busy_seconds"] for kid in CARD_KEYS]
    assert max(busy) - min(busy) <= 300 + 1, busy


def test_key_with_the_long_card_gets_the_next_one_last(pool):
    """Occupancy, not count: one 3h card outranks four 1-minute cards."""
    now = msk(9, 0)

    # Key picked first carries a long card.
    first = pool.reserve("long", now=now)
    long_key = first.key_id

    # Four short cards go round the remaining keys.
    for index in range(4):
        task = f"short-{index}"
        now += 60
        pool.reserve(task, now=now)
        now += 60
        pool.release(task, now=now)

    # Close the long card: three hours of occupancy on one key.
    pool.release("long", now=msk(12, 0))
    usage = pool.state()["usage"]
    assert usage[str(long_key)]["busy_seconds"] == pytest.approx(3 * 3600, abs=60)

    others = [kid for kid in CARD_KEYS if kid != long_key]
    short_busy = [usage.get(str(kid), {}).get("busy_seconds", 0.0) for kid in others]
    assert max(short_busy) < 3 * 3600 - 60

    # The next card must not go to the key that just carried three hours.
    now = msk(12, 5)
    picks = []
    for index in range(len(others)):
        reservation = pool.reserve(f"next-{index}", now=now)
        assert reservation is not None
        picks.append(reservation.key_id)
        now += 10
    assert long_key not in picks, picks


def test_within_the_tolerance_the_ring_keeps_turning(pool):
    """A ≤60s difference is noise: round-robin still decides, not the metric."""
    now = msk(9, 0)
    order = []
    for index in range(7):
        task = f"card-{index}"
        reservation = pool.reserve(task, now=now)
        order.append(reservation.key_id)
        now += 5  # every card lasts five seconds — all inside the tolerance
        pool.release(task, now=now)
        now += 5

    assert order == CARD_KEYS, order


def test_picker_ignores_yesterdays_bucket(pool):
    """A stale bucket must not bias today's pick in either direction."""
    state = {
        "version": 2,
        "reservations": {},
        "failed_keys": {},
        "rr_cursor": None,
        "usage": {
            "2": {"busy_seconds": 99999.0, "cards": 50, "window": "2026-03-14"},
        },
    }
    pool.path.write_text(json.dumps(state), encoding="utf-8")

    reservation = pool.reserve("card-today", now=msk(9, 0, day=15))
    # Stale bucket dropped → key #2 is a normal candidate, and the ring starts
    # from the lowest id exactly as a fresh pool does.
    assert reservation.key_id == 2


def test_status_reports_busy_seconds_and_cards(pool):
    """`keypool status` must show the number the picker balances on."""
    reservation = pool.reserve("card-s", now=msk(9, 0))
    kid = reservation.key_id
    pool.release("card-s", now=msk(9, 45))

    status = kp.pool_status()
    row = next(r for r in status["keys"] if r["key_id"] == kid)
    assert row["busy_seconds"] == pytest.approx(45 * 60, abs=1)
    assert row["cards"] == 1
    assert row["usage_window"] == "2026-03-15"
    # Every card key is present, busy or not.
    assert {r["key_id"] for r in status["keys"]} == set(CARD_KEYS)


def test_state_file_without_usage_still_loads(pool):
    """A v2 state written before this change reads exactly as before."""
    state = {
        "version": 2,
        "reservations": {},
        "failed_keys": {},
        "rr_cursor": 5,
    }
    pool.path.write_text(json.dumps(state), encoding="utf-8")

    loaded = kp._read_state(pool.path)
    assert loaded["rr_cursor"] == 5
    assert loaded["usage"] == {}
    assert loaded["reservations"] == {}
