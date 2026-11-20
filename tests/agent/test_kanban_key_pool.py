"""Per-card API-key reservation (mission ``key-pool-per-card``).

Card t_96a0e866 acceptance criteria — the tests below are organised to match
them one-for-one so a reviewer can check coverage by name:

* **(а)** two concurrent starts get DIFFERENT keys
        → :class:`TestNoDoubleAssignment`
* **(б)** background never gets a card key and a card never gets the
        background key → :class:`TestBackgroundIsolation`
* **(в)** release on complete / block / process death / TTL
        → :class:`TestRelease`
* **(г)** fallback to the single-key scheme when no keys exist
        → :class:`TestZeroConfigFallback`
* **(д)** rollback → :class:`TestRollback`

Plus the behaviours the card body demands explicitly but does not letter:
no silent mid-run key swap (401/403/429), quarantine, and the audit surface
that lets the owner prove the split (``kanban keypool check``).

Hermetic by construction: every test points ``HERMES_KANBAN_KEYPOOL`` and
``HERMES_KANBAN_KEYPOOL_STATE`` at a temp dir, so nothing touches the real
``~/…/kanban-keypool.json`` and no real provider is called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent import kanban_key_pool as kp

BASE_URL = "https://llm.letovo.site/openai"

pytestmark = pytest.mark.no_isolate


# ── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_pool_state():
    """Every test starts with no memoised binding and no env leakage."""
    saved = {
        k: os.environ.get(k)
        for k in (
            kp.ENV_KEYPOOL,
            kp.ENV_KEYPOOL_STATE,
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            # The full dispatcher envelope. ``as_card()`` sets these, and the
            # pool's card branch requires them (``is_dispatcher_worker_env``),
            # so they must be scrubbed like every other worker identity var.
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_WORKSPACE",
            "HERMES_PROFILE",
            "HERMES_SESSION_SOURCE",
            "HERMES_KANBAN_MAX_RUNTIME_SECONDS",
            "HERMES_KANBAN_KEYPOOL_QUARANTINE_SECONDS",
        )
    }
    for key in saved:
        os.environ.pop(key, None)
    kp.reset_process_cache()
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    kp.reset_process_cache()


def write_keys(tmp_path: Path, *, card_keys: int = 9, enabled: bool = True,
               base_url: str = BASE_URL,
               max_cards_per_key: int = 1) -> Path:
    """Write a key file with one background key plus ``card_keys`` card keys.

    ``max_cards_per_key`` defaults to **1** here on purpose, and that is test
    (д) of the K4 card applied to the whole file: the entire pre-slot suite
    below was written against exclusive keys, so running it at ``1`` proves by
    execution that the slot implementation reproduces the old behaviour exactly
    instead of merely claiming it. The slot tests set ``2`` explicitly
    (:class:`TestMaxCardsPerKey`), and the production default of 2 is proved by
    :meth:`TestMaxCardsPerKey.test_default_limit_is_two_without_the_field`.
    """
    keys = [{"id": 1, "label": "background", "role": "background",
             "api_key": "KEY_BG_111"}]
    for i in range(card_keys):
        kid = i + 2
        keys.append({
            "id": kid,
            "label": f"account-{kid}",
            "role": "card",
            "api_key": f"KEY_CARD_{kid}_222",
        })
    path = tmp_path / "kanban-keypool.json"
    path.write_text(json.dumps({
        "version": 1,
        "enabled": enabled,
        "base_url": base_url,
        "provider": "letovo",
        "background_key_id": 1,
        "max_cards_per_key": max_cards_per_key,
        "keys": keys,
    }), encoding="utf-8")
    os.environ[kp.ENV_KEYPOOL] = str(path)
    os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "kanban-keypool.state.json")
    kp.reset_process_cache()
    return path


def as_card(task_id: str, run_id: int = 1) -> None:
    """Become a genuine dispatcher-spawned card worker.

    A card is NOT identified by ``HERMES_KANBAN_TASK`` alone — see
    ``agent/kanban_stop.py`` and ``agent/delegation_context.py``. The pool's
    card branch requires the full dispatcher envelope, so the helper sets the
    same keys ``hermes_cli.kanban_db._build_worker_env`` sets in production.
    ``as_bare_task_env()`` is the opposite fixture: a stale task id with no
    envelope, which is a chat process, not a card.
    """
    os.environ["HERMES_KANBAN_TASK"] = task_id
    os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)
    os.environ["HERMES_KANBAN_DB"] = str(
        Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
        / "kanban-test.db"
    )
    os.environ["HERMES_KANBAN_WORKSPACE"] = str(
        Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
        / "kanban-test-ws"
    )
    os.environ["HERMES_PROFILE"] = "03-software-engineer"
    os.environ["HERMES_SESSION_SOURCE"] = "kanban"


def as_bare_task_env(task_id: str = "t_dead_card") -> None:
    """A stale ``HERMES_KANBAN_TASK`` with NO dispatcher envelope.

    This is the long-lived gateway/api_server process that inherited an old
    worker's task id (``agent/kanban_stop.py`` phantom-loop regression
    ``t_6972f101``, ``gateway/session_context.py:531``). ``kanban_stop``
    reports ``is_dispatcher_worker_env() is False`` for it, so the key pool
    must treat it as background — never as a card.
    """
    os.environ["HERMES_KANBAN_TASK"] = task_id
    for key in (
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_PROFILE",
        "HERMES_SESSION_SOURCE",
    ):
        os.environ.pop(key, None)


def as_background() -> None:
    for key in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_PROFILE",
        "HERMES_SESSION_SOURCE",
    ):
        os.environ.pop(key, None)


def runtime(api_key: str = "SHARED_ENV_KEY", base_url: str = BASE_URL) -> dict:
    return {
        "provider": "custom",
        "api_mode": "chat_completions",
        "base_url": base_url,
        "api_key": api_key,
        "source": "custom_provider:letovo",
    }


@pytest.fixture
def live_pid():
    """Factory for pids that are genuinely alive for the duration of the test.

    Production reservations always belong to a running worker, so a reservation
    made with a made-up pid is legitimately reaped as a corpse by the very next
    call. Tests that need a reservation to *persist* must therefore use real
    live pids; this fixture spawns sleeping children and reaps them on teardown.
    """
    children: list[subprocess.Popen] = []
    procs: dict[int, subprocess.Popen] = {}

    def _make() -> int:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        children.append(proc)
        procs[proc.pid] = proc
        # Give the interpreter a moment to exist as a process.
        for _ in range(200):
            if kp._pid_alive(proc.pid):
                return proc.pid
            time.sleep(0.01)
        raise AssertionError("child never became alive")

    def _kill(pid: int) -> None:
        """Kill a spawned child AND reap it.

        On POSIX a killed child stays a zombie — still ``psutil.pid_exists`` —
        until its parent waits on it, so "dead" has to mean "reaped". On the
        fork's Windows host the same call is a plain kill.
        """
        proc = procs.get(pid)
        if proc is not None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:  # pragma: no cover
                pass
            return
        for _ in range(400):  # pragma: no cover - unknown pid
            if not kp._pid_alive(pid):
                return
            time.sleep(0.01)

    _make.kill = _kill  # type: ignore[attr-defined]
    yield _make
    for proc in children:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # pragma: no cover
            pass


# ── (а) no double assignment ───────────────────────────────────────────────


class TestNoDoubleAssignment:
    def test_sequential_reservations_get_distinct_keys(self, tmp_path, live_pid):
        write_keys(tmp_path)
        first = kp.reserve_for_card("t_a", 1, pid=live_pid())
        second = kp.reserve_for_card("t_b", 2, pid=live_pid())
        third = kp.reserve_for_card("t_c", 3, pid=live_pid())

        assert first.key_id != second.key_id != third.key_id
        assert len({first.key_id, second.key_id, third.key_id}) == 3
        # Lowest-free bookkeeping: deterministic, human-explainable.
        assert [first.key_id, second.key_id, third.key_id] == [2, 3, 4]

    def test_eight_concurrent_cards_get_eight_distinct_keys(self, tmp_path, live_pid):
        """The owner's ceiling is 8 concurrent cards — all must differ."""
        write_keys(tmp_path, card_keys=9)
        reservations = [
            kp.reserve_for_card(f"t_{i}", i, pid=live_pid()) for i in range(8)
        ]
        key_ids = [r.key_id for r in reservations]
        assert len(set(key_ids)) == 8
        assert set(key_ids) == {2, 3, 4, 5, 6, 7, 8, 9}

    def test_parallel_processes_never_share_a_key(self, tmp_path):
        """The real race: N processes reserving in the same second.

        Run as real subprocesses so the cross-process lock is actually
        exercised (``msvcrt`` on Windows, ``fcntl`` on POSIX) — a thread-based
        test would pass even with no locking at all.
        """
        write_keys(tmp_path, card_keys=6)
        script = textwrap.dedent(
            f"""
            import json, os, sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
            from agent import kanban_key_pool as kp
            task, run_id = sys.argv[1], int(sys.argv[2])
            os.environ["HERMES_KANBAN_TASK"] = task
            os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)
            # Synchronise the start so the reservations truly overlap.
            deadline = time.time() + 10
            marker = os.environ["START_MARKER"]
            while not os.path.exists(marker) and time.time() < deadline:
                time.sleep(0.005)
            res = kp.reserve_for_card(task, run_id)
            print(json.dumps({{"task": task, "key_id": res.key_id}}), flush=True)
            # STAY ALIVE: a real worker keeps its reservation for the whole
            # card. Exiting here would (correctly) make every reservation a
            # reapable corpse and the test would measure the reaper, not the
            # race.
            stop = os.environ["STOP_MARKER"]
            deadline = time.time() + 60
            while not os.path.exists(stop) and time.time() < deadline:
                time.sleep(0.02)
            """
        )
        script_path = tmp_path / "reserve_child.py"
        script_path.write_text(script, encoding="utf-8")
        marker = tmp_path / "go"
        stop = tmp_path / "stop"
        env = dict(os.environ)
        env["START_MARKER"] = str(marker)
        env["STOP_MARKER"] = str(stop)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

        procs = [
            subprocess.Popen(
                [sys.executable, str(script_path), f"t_par_{i}", str(i)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(6)
        ]
        marker.write_text("go", encoding="utf-8")
        results, errors = [], []
        try:
            for proc in procs:
                # Read the reservation line while the child is still holding it.
                line = proc.stdout.readline()
                if not line.strip().startswith("{"):
                    errors.append(proc.stderr.read())
                    continue
                results.append(json.loads(line.strip()))
        finally:
            stop.write_text("stop", encoding="utf-8")
            for proc in procs:
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()

        assert not errors, errors
        assert len(results) == 6
        key_ids = [r["key_id"] for r in results]
        assert len(set(key_ids)) == 6, f"duplicate keys handed out: {key_ids}"
        assert 1 not in key_ids, "the background key was handed to a card"

    def test_same_process_same_task_reuses_its_key(self, tmp_path):
        """Repeated resolution inside one worker must not take a second key."""
        write_keys(tmp_path)
        first = kp.reserve_for_card("t_a", 1, pid=os.getpid())
        again = kp.reserve_for_card("t_a", 1, pid=os.getpid())
        assert again.key_id == first.key_id
        assert again.reused is True
        # Someone else's reservation must not have been disturbed.
        assert len(kp.pool_status()["reservations"]) == 1

    def test_sibling_process_of_the_same_card_adopts_its_key(
        self, tmp_path, live_pid
    ):
        """A second process of the SAME card must not consume a second account.

        The mission rule is one card = one key for the whole run. A delegated
        helper, a `hermes` subprocess or a retried worker resolves the runtime
        out-of-process with the same ``HERMES_KANBAN_TASK``; before this was
        pinned, each of them took the next free key and an 8-card ceiling
        could burn twice the pool.
        """
        write_keys(tmp_path, card_keys=3)
        owner, sibling = live_pid(), live_pid()
        first = kp.reserve_for_card("t_a", 1, pid=owner)
        adopted = kp.reserve_for_card("t_a", 1, pid=sibling)
        assert adopted.key_id == first.key_id, "same card took two keys"
        assert adopted.reused is True
        assert len(kp.pool_status()["reservations"]) == 1

        # A different card still gets its own, distinct key.
        other = kp.reserve_for_card("t_b", 2, pid=live_pid())
        assert other.key_id != first.key_id

    def test_sibling_exit_detaches_without_freeing_the_cards_key(
        self, tmp_path, live_pid
    ):
        write_keys(tmp_path)
        owner, sibling = live_pid(), live_pid()
        res = kp.reserve_for_card("t_a", 1, pid=owner)
        kp.reserve_for_card("t_a", 1, pid=sibling)

        # The helper process exits: it releases nothing, it just detaches.
        assert kp.release_for_pid(pid=sibling) == []
        held = kp.pool_status()["reservations"]
        assert [r["task_id"] for r in held] == ["t_a"]
        assert held[0]["key_id"] == res.key_id
        assert sibling not in (held[0].get("attached_pids") or [])

    def test_reservation_survives_the_owner_pid_when_a_sibling_is_alive(
        self, tmp_path, live_pid
    ):
        """The card's key stays taken while ANY of its processes is alive."""
        write_keys(tmp_path, card_keys=1)
        owner, sibling = live_pid(), live_pid()
        kp.reserve_for_card("t_a", 1, pid=owner)
        kp.reserve_for_card("t_a", 1, pid=sibling)

        # Kill the owning pid only; the sibling keeps the card running.
        live_pid.kill(owner)
        assert kp._pid_alive(owner) is False
        assert kp.sweep() == [], "a live sibling must keep the card's key"
        assert len(kp.pool_status()["reservations"]) == 1

    def test_reservation_is_released_once_the_last_sibling_dies(
        self, tmp_path, live_pid
    ):
        write_keys(tmp_path, card_keys=1)
        owner, sibling = live_pid(), live_pid()
        kp.reserve_for_card("t_a", 1, pid=owner)
        kp.reserve_for_card("t_a", 1, pid=sibling)
        for pid in (owner, sibling):
            live_pid.kill(pid)
        dropped = kp.sweep()
        assert [d["task_id"] for d in dropped] == ["t_a"]
        assert kp.pool_status()["reservations"] == []

    def test_pool_exhaustion_raises_and_never_borrows_the_background_key(
        self, tmp_path, live_pid
    ):
        write_keys(tmp_path, card_keys=1)
        held = live_pid()
        kp.reserve_for_card("t_a", 1, pid=held)
        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_b", 2, pid=held)
        assert "never handed to a card" in str(exc.value)

    def test_reservation_survives_a_state_reload(self, tmp_path, live_pid):
        """The reservation is on disk, not in memory: it survives a restart."""
        write_keys(tmp_path)
        kp.reserve_for_card("t_a", 1, pid=live_pid())
        kp.reset_process_cache()  # simulate a fresh gateway/worker process
        status = kp.pool_status()
        assert [r["key_id"] for r in status["reservations"]] == [2]
        assert status["reservations"][0]["task_id"] == "t_a"


# ── K4: up to `max_cards_per_key` cards on one key (slots) ─────────────────
#
# The whole file above runs at ``max_cards_per_key: 1`` (see ``write_keys``),
# which is criterion (д) applied to every legacy assertion at once. This class
# is the slot side: 2 by default, and each letter of the K4 card has its own
# test. Both halves must stay green — 1 identical to the old behaviour, 2
# genuinely sharing a key without ever over-subscribing it.


class TestMaxCardsPerKey:
    # ── (а) two cards on one key ──────────────────────────────────────────
    #
    # The tests below that need two cards on the SAME key ask for exactly one
    # card key (``card_keys=1``). Before the rotation (card K4-Р) they relied
    # on ``free[0]`` doubling up on #2 while eight other keys sat idle — that
    # packing IS the defect the owner reported, so it can no longer be the way
    # a slot test reaches its setup. With one key the rotation has nowhere to
    # go and the slot semantics are exercised directly.
    def test_two_cards_share_one_key_and_both_are_visible(self, tmp_path, live_pid):
        """(а) Two cards land on the SAME key and both show up in the status."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        assert kp.load_config().max_cards_per_key == 2

        first = kp.reserve_for_card("t_a", 1, pid=live_pid())
        second = kp.reserve_for_card("t_b", 2, pid=live_pid())

        assert first.key_id == second.key_id == 2, (
            f"the second card could not use the free slot on #{first.key_id} "
            f"when it was the only card key: it went to #{second.key_id}"
        )
        assert first.api_key == second.api_key

        status = kp.pool_status()
        rows = status["reservations"]
        assert sorted((r["task_id"], r["key_id"]) for r in rows) == [
            ("t_a", 2), ("t_b", 2)
        ]
        assert [r["stale"] for r in rows] == [False, False]
        # Requirement 6: the per-key occupancy view names both cards.
        occ = {row["key_id"]: row for row in status["keys"]}
        assert occ[2]["used"] == 2
        assert occ[2]["limit"] == 2
        assert sorted(occ[2]["task_ids"]) == ["t_a", "t_b"]
        assert status["max_cards_per_key"] == 2

    def test_sibling_adopts_its_own_cards_slot_not_the_neighbours(
        self, tmp_path, live_pid
    ):
        """The new hazard sharing a key introduces: slot mix-ups between cards.

        Two cards sit on key #2. A delegated helper of card A joins the pool as
        a second process of A. It must attach to A's slot — attaching to B's
        would (a) put A's traffic on a lease B will end, and (b) leave A's own
        slot looking ownerless, so A's death would not be reaped.
        """
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        owner_a, owner_b, child_a = live_pid(), live_pid(), live_pid()
        kp.reserve_for_card("t_a", 1, pid=owner_a)
        kp.reserve_for_card("t_b", 2, pid=owner_b)

        adopted = kp.reserve_for_card("t_a", 1, pid=child_a)
        assert adopted.key_id == 2 and adopted.reused is True

        entry = json.loads(
            Path(os.environ[kp.ENV_KEYPOOL_STATE]).read_text(encoding="utf-8")
        )["reservations"]["2"]
        by_task = {s["task_id"]: s for s in entry["slots"]}
        assert len(entry["slots"]) == 2, "a sibling must not add a third slot"
        assert by_task["t_a"]["attached_pids"] == [child_a]
        assert by_task["t_a"]["pid"] == owner_a, "the original owner stays owner"
        assert by_task["t_b"].get("attached_pids") in (None, []), (
            "card A's helper attached to card B's slot"
        )

        # Card A's owner dying is NOT enough to free the slot: its helper lives.
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for slot in state["reservations"]["2"]["slots"]:
            if slot["task_id"] == "t_a":
                slot["pid"] = _unused_pid()
        state_file.write_text(json.dumps(state), encoding="utf-8")
        dropped = kp.sweep()
        assert dropped == [], "a live attached helper must keep the slot alive"
        remaining = {
            r["task_id"] for r in kp.pool_status()["reservations"]
        }
        assert remaining == {"t_a", "t_b"}

    def test_both_cards_keep_their_own_leases(self, tmp_path, live_pid):
        """Sharing a key must not merge the two leases into one field."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        pid_a, pid_b = live_pid(), live_pid()
        kp.reserve_for_card("t_a", 1, pid=pid_a)
        kp.reserve_for_card("t_b", 2, pid=pid_b)
        state = json.loads(Path(os.environ[kp.ENV_KEYPOOL_STATE]).read_text(encoding="utf-8"))
        entry = state["reservations"]["2"]
        assert len(entry["slots"]) == 2
        assert sorted(s["pid"] for s in entry["slots"]) == sorted([pid_a, pid_b])
        assert sorted(s["task_id"] for s in entry["slots"]) == ["t_a", "t_b"]

    # ── (б) a full key is not over-subscribed ─────────────────────────────
    def test_a_third_card_on_a_full_single_key_is_refused(self, tmp_path, live_pid):
        """Two slots on the only card key: the third card gets the clear error.

        The rotation spreads cards across keys (see
        :class:`TestRoundRobinKeyChoice`); what is pinned here is the ceiling
        itself — a key never accepts a third card, and the message names the
        occupancy instead of a bare "no key".
        """
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        keys = [kp.reserve_for_card(f"t_{i}", i, pid=live_pid()).key_id for i in range(2)]
        assert keys == [2, 2], keys
        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_3", 3, pid=live_pid())
        assert "#2: 2/2" in str(exc.value), str(exc.value)

    def test_eight_cards_fit_at_two_slots_per_key(self, tmp_path, live_pid):
        """The owner's capacity claim: the eighth card is no longer refused.

        With 7 card keys × 2 slots the ceiling is 14. The rotation spreads the
        eight cards over all seven keys, and no key goes above its limit.
        """
        write_keys(tmp_path, card_keys=7, max_cards_per_key=2)
        reserved = [kp.reserve_for_card(f"t_{i}", i, pid=live_pid()) for i in range(8)]
        assert len(reserved) == 8, "the eighth card must not be refused"
        assert 1 not in [r.key_id for r in reserved]
        per_key: dict = {}
        for r in reserved:
            per_key[r.key_id] = per_key.get(r.key_id, 0) + 1
        assert max(per_key.values()) <= 2, per_key
        status = kp.pool_status()
        assert status["enabled"] is True
        assert sum(row["used"] for row in status["keys"]) == 8
        assert all(row["used"] <= row["limit"] for row in status["keys"])

    # ── (в) release frees exactly one slot ────────────────────────────────
    def test_release_frees_one_slot_and_keeps_the_other_card(self, tmp_path, live_pid):
        """(в) The whole point: one card ending must not cancel the other."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        kp.reserve_for_card("t_a", 1, pid=live_pid())
        kp.reserve_for_card("t_b", 2, pid=live_pid())

        released = kp.release_for_task("t_a")
        assert [r["task_id"] for r in released] == ["t_a"]
        assert released[0]["key_id"] == 2
        assert released[0]["released_reason"] == "released"

        rows = kp.pool_status()["reservations"]
        assert [(r["task_id"], r["key_id"]) for r in rows] == [("t_b", 2)], (
            "releasing one card must free exactly one slot, not the whole key"
        )
        # The freed slot is genuinely reusable by a third card.
        assert kp.reserve_for_card("t_c", 3, pid=live_pid()).key_id == 2
        assert sorted(
            r["task_id"] for r in kp.pool_status()["reservations"]
        ) == ["t_b", "t_c"]

    def test_process_exit_releases_only_the_exiting_cards_slot(
        self, tmp_path, live_pid
    ):
        write_keys(tmp_path, card_keys=9, max_cards_per_key=2)
        pid_a, pid_b = live_pid(), live_pid()
        kp.reserve_for_card("t_a", 1, pid=pid_a)
        kp.reserve_for_card("t_b", 2, pid=pid_b)

        dropped = kp.release_for_pid(pid_a)
        assert [d["task_id"] for d in dropped] == ["t_a"]
        assert dropped[0]["released_reason"] == "process_exit"
        assert [r["task_id"] for r in kp.pool_status()["reservations"]] == ["t_b"]

    def test_a_dead_card_on_a_shared_key_does_not_reap_the_live_one(
        self, tmp_path, live_pid
    ):
        """Staleness is per slot: reaping the key would cancel a live card."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        dead_pid, live = live_pid(), live_pid()
        kp.reserve_for_card("t_dead", 1, pid=dead_pid)
        kp.reserve_for_card("t_live", 2, pid=live)
        assert [r["key_id"] for r in kp.pool_status()["reservations"]] == [2, 2]

        # Make the first card's owner vanish without touching the second's:
        # rewriting the stored pid is the only way to simulate a death without
        # racing a real child process.
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for slot in state["reservations"]["2"]["slots"]:
            if slot["task_id"] == "t_dead":
                slot["pid"] = _unused_pid()
        state_file.write_text(json.dumps(state), encoding="utf-8")

        dropped = kp.sweep()
        assert [d["task_id"] for d in dropped] == ["t_dead"]
        assert dropped[0]["released_reason"] == "pid_gone"
        rows = kp.pool_status()["reservations"]
        assert [(r["task_id"], r["key_id"]) for r in rows] == [("t_live", 2)]

    def test_renewal_extends_only_the_callers_slot(self, tmp_path, live_pid, monkeypatch):
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        monkeypatch.setenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", "60")
        pid_a, pid_b = live_pid(), live_pid()
        kp.reserve_for_card("t_a", 1, pid=pid_a)
        kp.reserve_for_card("t_b", 2, pid=pid_b)

        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for slot in state["reservations"]["2"]["slots"]:
            slot["expires_at"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")

        assert kp.renew_for_card("t_a", 2, pid=pid_a) is True
        after = json.loads(state_file.read_text(encoding="utf-8"))
        leases = {
            s["task_id"]: int(s["expires_at"])
            for s in after["reservations"]["2"]["slots"]
        }
        assert leases["t_a"] > int(time.time()), "the caller's lease was not extended"
        assert leases["t_b"] < int(time.time()), (
            "renewing one card extended the other card's lease as well"
        )

    # ── (г) every slot busy → an error that names the occupancy ───────────
    def test_full_pool_error_names_occupancy_per_key(self, tmp_path, live_pid):
        """(г) Two keys × 2 slots, five cards: the fifth must explain itself."""
        write_keys(tmp_path, card_keys=2, max_cards_per_key=2)
        for i in range(4):
            kp.reserve_for_card(f"t_{i}", i, pid=live_pid())

        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_overflow", 9, pid=live_pid())
        message = str(exc.value)
        assert "#2: 2/2" in message, message
        assert "#3: 2/2" in message, message
        assert "never handed to a card" in message, message
        # Actionable, not just descriptive.
        assert "max_cards_per_key" in message, message

    def test_quarantine_removes_a_key_and_says_so_in_the_error(
        self, tmp_path, live_pid
    ):
        """(ж, second half) A dead key is not silently counted as capacity."""
        write_keys(tmp_path, card_keys=2, max_cards_per_key=2)
        owner = live_pid()
        kp.reserve_for_card("t_a", 1, pid=owner)
        assert kp.mark_key_failed(
            "t_a", 401, reason="auth_rejected", pid=owner
        ) is not None
        kp.reserve_for_card("t_b", 2, pid=live_pid())
        kp.reserve_for_card("t_c", 3, pid=live_pid())

        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_d", 4, pid=live_pid())
        message = str(exc.value)
        assert "Quarantined" in message, message
        assert "#2" in message, message

    # ── (д) limit 1 == the old exclusive behaviour ────────────────────────
    def test_limit_one_reproduces_exclusive_keys(self, tmp_path, live_pid):
        """(д) The explicit, named proof; the whole file runs at 1 as well."""
        write_keys(tmp_path, card_keys=9, max_cards_per_key=1)
        first = kp.reserve_for_card("t_a", 1, pid=live_pid())
        second = kp.reserve_for_card("t_b", 2, pid=live_pid())
        assert (first.key_id, second.key_id) == (2, 3)
        # One card per key: releasing it takes the key record with it.
        kp.release_for_task("t_a")
        state = json.loads(Path(os.environ[kp.ENV_KEYPOOL_STATE]).read_text(encoding="utf-8"))
        assert "2" not in state["reservations"]
        assert [r["task_id"] for r in kp.pool_status()["reservations"]] == ["t_b"]

    def test_limit_one_is_written_as_the_legacy_flat_record(self, tmp_path, live_pid):
        """Rolling restart safety: a 1-slot key still writes the v1 shape."""
        write_keys(tmp_path, card_keys=9, max_cards_per_key=1)
        kp.reserve_for_card("t_a", 1, pid=live_pid())
        state = json.loads(Path(os.environ[kp.ENV_KEYPOOL_STATE]).read_text(encoding="utf-8"))
        entry = state["reservations"]["2"]
        assert "slots" not in entry, "a single-card key must stay readable by the old build"
        assert entry["task_id"] == "t_a"
        assert entry["key_id"] == 2

    def test_default_limit_is_two_without_the_field(self, tmp_path, live_pid):
        """The production default is 2 — an older key file gets the new limit."""
        path = tmp_path / "kanban-keypool.json"
        path.write_text(json.dumps({
            "version": 1,
            "base_url": BASE_URL,
            "background_key_id": 1,
            "keys": [
                {"id": 1, "role": "background", "api_key": "KEY_BG_111"},
                {"id": 2, "role": "card", "api_key": "KEY_CARD_2_222"},
                {"id": 3, "role": "card", "api_key": "KEY_CARD_3_222"},
            ],
        }), encoding="utf-8")
        os.environ[kp.ENV_KEYPOOL] = str(path)
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "s.json")
        kp.reset_process_cache()

        assert kp.load_config().max_cards_per_key == 2
        assert kp.pool_status()["max_cards_per_key"] == 2
        first = kp.reserve_for_card("t_a", 1, pid=live_pid())
        second = kp.reserve_for_card("t_b", 2, pid=live_pid())
        # Two card keys exist, so the rotation spreads the two cards; the
        # point of the test is that the default limit is 2, not that they share.
        assert (first.key_id, second.key_id) == (2, 3)
        assert kp.pool_status()["max_cards_per_key"] == 2

    def test_env_overrides_the_file_limit(self, tmp_path, live_pid, monkeypatch):
        write_keys(tmp_path, card_keys=9, max_cards_per_key=2)
        monkeypatch.setenv(kp.ENV_MAX_CARDS_PER_KEY, "1")
        kp.reset_process_cache()
        assert kp.load_config().max_cards_per_key == 1
        kp.reserve_for_card("t_a", 1, pid=live_pid())
        assert kp.reserve_for_card("t_b", 2, pid=live_pid()).key_id == 3

    def test_a_bad_limit_value_does_not_silently_raise_capacity(self, tmp_path):
        """An ambiguous limit must disable the pool, not guess a bigger one."""
        path = tmp_path / "kanban-keypool.json"
        path.write_text(json.dumps({
            "version": 1,
            "base_url": BASE_URL,
            "background_key_id": 1,
            "max_cards_per_key": "two",
            "keys": [
                {"id": 1, "role": "background", "api_key": "KEY_BG_111"},
                {"id": 2, "role": "card", "api_key": "KEY_CARD_2_222"},
            ],
        }), encoding="utf-8")
        os.environ[kp.ENV_KEYPOOL] = str(path)
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "s.json")
        kp.reset_process_cache()
        assert kp.is_enabled() is False
        assert kp.pool_status()["inactive_reason"] == "unusable_key_file"

    # ── (е) a legacy state file reads without loss ────────────────────────
    def test_legacy_state_file_without_slots_is_read_losslessly(self, tmp_path):
        """(е) An old state file (one flat record per key) must survive."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        legacy = {
            "version": 1,
            "reservations": {
                "2": {
                    "key_id": 2,
                    "key_label": "account-2",
                    "task_id": "t_old",
                    "run_id": 7,
                    "pid": os.getpid(),
                    "host": "probe",
                    "reserved_at": int(time.time()) - 60,
                    "expires_at": int(time.time()) + 600,
                    "status": "active",
                }
            },
            "failed_keys": {},
        }
        state_file.write_text(json.dumps(legacy), encoding="utf-8")
        kp.reset_process_cache()

        rows = kp.pool_status()["reservations"]
        assert len(rows) == 1
        assert rows[0]["task_id"] == "t_old"
        assert rows[0]["key_id"] == 2
        assert rows[0]["run_id"] == 7
        assert rows[0]["stale"] is False

        # The legacy card counts against the limit: one slot is now free.
        assert kp.pool_status()["keys"][0]["used"] == 1
        nxt = kp.reserve_for_card("t_new", 8, pid=os.getpid())
        assert nxt.key_id == 2, "the legacy reservation's free slot was not reused"

        # Both cards are now on the key and the file carries both.
        assert sorted(
            r["task_id"] for r in kp.pool_status()["reservations"]
        ) == ["t_new", "t_old"]

    def test_legacy_state_file_still_honours_a_dead_owner(self, tmp_path):
        """A scattered legacy record must be reaped, not trusted forever."""
        write_keys(tmp_path, card_keys=9, max_cards_per_key=2)
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state_file.write_text(json.dumps({
            "version": 1,
            "reservations": {"2": {
                "key_id": 2, "key_label": "account-2", "task_id": "t_ghost",
                "pid": _unused_pid(), "expires_at": int(time.time()) + 600,
                "status": "active",
            }},
            "failed_keys": {},
        }), encoding="utf-8")
        kp.reset_process_cache()
        dropped = kp.sweep()
        assert [d["task_id"] for d in dropped] == ["t_ghost"]
        assert kp.pool_status()["reservations"] == []

    # ── (ж) background key stays out of the pool under any shortage ───────
    def test_background_key_is_never_used_under_a_full_slot_shortage(
        self, tmp_path, live_pid
    ):
        """(ж) Key #1 is not in card_ids at all, so it cannot be a fallback."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        for i in range(2):
            kp.reserve_for_card(f"t_{i}", i, pid=live_pid())
        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_overflow", 9, pid=live_pid())
        assert "never handed to a card" in str(exc.value)
        assert 1 not in [r["key_id"] for r in kp.pool_status()["reservations"]]

        # And a card resolution through the runtime hook fails the same way.
        kp.reset_process_cache()
        as_card("t_runtime_overflow", 42)
        with pytest.raises(kp.KeyPoolError):
            kp.apply_card_key(runtime())
        assert kp.background_key().key_id == 1

    # ── quarantine covers the whole key (requirement 7) ───────────────────
    def test_a_failed_key_leaves_circulation_for_its_second_slot(
        self, tmp_path, live_pid
    ):
        """A rejected credential must not serve the key's other slot either."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        other_pid = live_pid()
        kp.reserve_for_card("t_a", 1, pid=os.getpid())
        kp.reserve_for_card("t_b", 2, pid=other_pid)
        assert [r["key_id"] for r in kp.pool_status()["reservations"]] == [2, 2]

        kp.mark_key_failed("t_a", 401, reason="auth_rejected", pid=os.getpid())

        # The failing card keeps its own slot (marked bad) — a live card must
        # never lose the key it is still running on (defect D1) — and the
        # innocent card keeps its own untouched.
        rows = kp.pool_status()["reservations"]
        assert [(r["task_id"], r["key_id"], r["status"]) for r in rows] == [
            ("t_a", 2, "bad"),
            ("t_b", 2, "active"),
        ]
        # The key is quarantined, so neither the bad slot nor the second slot
        # is handed out again — with no other card key the next card must be
        # refused, not squeezed in.
        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_c", 3, pid=live_pid())
        assert "Quarantined" in str(exc.value), str(exc.value)
        assert [f["key_id"] for f in kp.pool_status()["failed_keys"]] == [2]

    # ── operator surfaces ─────────────────────────────────────────────────
    def test_check_verdict_is_ok_for_two_cards_on_one_key(self, tmp_path, capsys):
        """Two cards on one key is CORRECT now — the verdict must not cry wolf."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        kp.reserve_for_card("t_a", 1, pid=os.getpid())
        kp.reserve_for_card("t_b", 2, pid=os.getpid())
        from hermes_cli.subcommands.kanban_keypool import _cmd_check

        class _Args:
            json = True

        assert _cmd_check(kp, _Args(), True) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "ok"
        assert payload["card_count"] == 2
        assert payload["distinct_keys"] == 1
        assert payload["max_cards_per_key"] == 2
        assert payload["over_limit_keys"] == []
        assert payload["tasks_on_multiple_keys"] == {}

    def test_check_reports_an_over_limit_key(self, tmp_path, capsys):
        """The new failure mode: three cards on one key at limit 2."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=2)
        for i in range(3):
            if i < 2:
                kp.reserve_for_card(f"t_{i}", i, pid=os.getpid())
        from hermes_cli.subcommands.kanban_keypool import _cmd_check

        class _Args:
            json = True

        # Force the over-subscription the reservation path would never allow,
        # so the verdict is proven to detect it rather than just stay quiet.
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["reservations"]["2"]["slots"].append({
            "task_id": "t_smuggled", "run_id": 99, "pid": os.getpid(),
            "expires_at": int(time.time()) + 600, "status": "active",
        })
        state_file.write_text(json.dumps(state), encoding="utf-8")

        assert _cmd_check(kp, _Args(), True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "OVER_LIMIT"
        assert payload["over_limit_keys"] == [
            {"key_id": 2, "limit": 2, "task_ids": ["t_0", "t_1", "t_smuggled"]}
        ]

    def test_status_text_shows_per_key_occupancy(self, tmp_path, live_pid, capsys):
        write_keys(tmp_path, card_keys=3, max_cards_per_key=2)
        kp.reserve_for_card("t_a", 1, pid=os.getpid())
        kp.reserve_for_card("t_b", 2, pid=live_pid())
        from hermes_cli.subcommands.kanban_keypool import _cmd_status

        assert _cmd_status(kp, False) == 0
        out = capsys.readouterr().out
        assert "key #2 (account-2): 1/2" in out, out
        assert "key #3 (account-3): 1/2" in out, out
        assert "key #4 (account-4): 0/2" in out, out
        assert "card limit per key: 2" in out, out
        assert "KEY_CARD_" not in out and "KEY_BG_" not in out

    def test_status_never_leaks_secrets_in_the_keys_view(self, tmp_path):
        write_keys(tmp_path, card_keys=3, max_cards_per_key=2)
        kp.reserve_for_card("t_a", 1, pid=os.getpid())
        kp.reserve_for_card("t_b", 2, pid=os.getpid())
        blob = json.dumps(kp.pool_status(), ensure_ascii=False)
        assert "KEY_CARD_" not in blob
        assert "KEY_BG_" not in blob


# ── K4-Р: rotation — least-loaded key, round-robin tie-break ──────────────
#
# The owner's complaint (2026-11-08): «чтобы ключи постоянно ротировались, а не
# всегда использовались первые 4». Before this class, ``reserve_for_card`` took
# ``free[0]`` — the lowest id with any free slot — so cards kept landing on #2
# until its two slots were full, and #3..#10 sat idle.
#
# The whole file above still runs at ``max_cards_per_key: 1``, where the new
# rule degenerates to the same lowest-free sequence (#2 → #3 → #4 …), which is
# why those legacy assertions stay green. The tests below use 2 slots on
# purpose: at two slots a "first free key" rule doubles up on #2, while the
# rotation must hand the second card #3.


class TestRoundRobinKeyChoice:
    """Two rules: least-loaded key first, round-robin among equals."""

    def test_two_cards_spread_to_two_keys_instead_of_doubling_up(
        self, tmp_path, live_pid
    ):
        """The owner's headline: the second card must NOT join #2's second slot.

        With nine card keys and two slots each, ``free[0]`` gave #2, #2, #3 …
        The rotation gives #2, #3, #4 … — one card per key while keys are
        available, which is what "ключи постоянно ротируются" means.
        """
        write_keys(tmp_path, card_keys=9, max_cards_per_key=2)
        first = kp.reserve_for_card("t_a", 1, pid=live_pid())
        second = kp.reserve_for_card("t_b", 2, pid=live_pid())

        assert (first.key_id, second.key_id) == (2, 3), (
            f"the second card took #{second.key_id} instead of the next key in "
            f"the rotation — free[0] behaviour is back"
        )

    def test_eight_cards_spread_over_all_seven_card_keys(self, tmp_path, live_pid):
        """8 cards, 7 keys, 2 slots: every key is used, none above its limit."""
        write_keys(tmp_path, card_keys=7, max_cards_per_key=2)
        reserved = [kp.reserve_for_card(f"t_{i}", i, pid=live_pid()) for i in range(8)]

        per_key: dict = {}
        for r in reserved:
            per_key[r.key_id] = per_key.get(r.key_id, 0) + 1
        assert set(per_key) == {2, 3, 4, 5, 6, 7, 8}, (
            f"the rotation left a card key unused: {per_key}"
        )
        assert max(per_key.values()) <= 2, per_key
        assert 1 not in per_key, "the background key was handed to a card"
        status = kp.pool_status()
        assert sum(row["used"] for row in status["keys"]) == 8
        assert all(row["used"] <= row["limit"] for row in status["keys"])

    def test_the_ring_advances_and_the_cursor_survives_a_restart(
        self, tmp_path, live_pid
    ):
        """#2 → #3 → #4 …, and the cursor is on disk, not in process memory."""
        write_keys(tmp_path, card_keys=9, max_cards_per_key=2)
        ids = [kp.reserve_for_card(f"t_{i}", i, pid=live_pid()).key_id for i in range(4)]
        assert ids == [2, 3, 4, 5], ids

        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state.get("rr_cursor") == 5, (
            f"the last handed-out key was not persisted: {state.get('rr_cursor')!r}"
        )

        kp.reset_process_cache()  # a new process must continue the same ring
        assert kp.reserve_for_card("t_9", 9, pid=live_pid()).key_id == 6

    def test_the_cursor_lives_in_the_reservation_state_file(self, tmp_path, live_pid):
        """One state file, not two: the cursor sits next to ``reservations``."""
        write_keys(tmp_path, card_keys=9, max_cards_per_key=2)
        kp.reserve_for_card("t_a", 1, pid=live_pid())

        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["rr_cursor"] == 2
        assert "2" in state["reservations"]
        # No second file was invented next to it.
        assert [p.name for p in state_file.parent.iterdir() if "cursor" in p.name] == []
        # The operator surface shows it too: "which key is next" must be
        # answerable without reading the state file by hand.
        assert kp.pool_status()["rr_cursor"] == 2

    def test_a_freed_slot_pulls_the_rotation_back_to_the_least_loaded_key(
        self, tmp_path, live_pid
    ):
        """A key that emptied is the least loaded again — and wins the next card.

        Five keys, one card on each. Ending the card on #4 leaves #4 at 0/2
        while #2 keeps one card, so the rotation must come back to #4. The old
        ``free[0]`` rule returned #2 here (lowest id with a free slot), which
        is exactly the "keys are not rotating" symptom the owner reported.
        """
        write_keys(tmp_path, card_keys=5, max_cards_per_key=2)
        held = {}
        for kid in range(2, 7):
            held[kid] = kp.reserve_for_card(f"t_{kid}", kid, pid=live_pid()).key_id
        assert sorted(held.values()) == [2, 3, 4, 5, 6], held
        assert kp.release_for_task("t_4")[0]["key_id"] == 4

        nxt = kp.reserve_for_card("t_new", 9, pid=live_pid())
        assert nxt.key_id == 4, (
            f"the emptied key #4 stayed idle and the card went to #{nxt.key_id} "
            f"— the least-loaded rule is not in force"
        )

    def test_a_quarantined_key_is_skipped_even_when_it_is_least_loaded(
        self, tmp_path, live_pid
    ):
        """Exclusion happens before the choice: a dead key is not capacity.

        Setup pins the two rules at once. #2 is emptied by a release (so the
        least-loaded rule would take it) and #3 is the cursor's next key; then
        #3 is quarantined. The rotation must skip the dead key AND pass over
        the lowest id — landing on #4. ``free[0]`` would have taken #2.
        """
        write_keys(tmp_path, card_keys=5, max_cards_per_key=2)
        owner = live_pid()
        kp.reserve_for_card("t_a", 1, pid=owner)      # #2
        rejected = kp.reserve_for_card("t_b", 2, pid=owner)   # #3
        assert rejected.key_id == 3

        kp.release_for_task("t_a")                     # #2 → 0/2
        assert kp.mark_key_failed(
            "t_b", 401, reason="auth_rejected", pid=owner
        ) is not None                                   # #3 → 0/2, poisoned

        assert [f["key_id"] for f in kp.pool_status()["failed_keys"]] == [3]
        nxt = kp.reserve_for_card("t_d", 4, pid=live_pid())
        assert nxt.key_id not in (2, 3), (
            f"#3 is quarantined and #2 was passed over by the cursor, yet the "
            f"card went to #{nxt.key_id}"
        )
        assert nxt.key_id == 4, f"the ring should land on #4, not #{nxt.key_id}"

    def test_reuse_does_not_move_the_cursor(self, tmp_path, live_pid):
        """One card = one key: an adopted reservation is not a new turn of the ring."""
        write_keys(tmp_path, card_keys=9, max_cards_per_key=2)
        pid = live_pid()
        first = kp.reserve_for_card("t_a", 1, pid=pid)
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        before = json.loads(state_file.read_text(encoding="utf-8"))["rr_cursor"]

        again = kp.reserve_for_card("t_a", 1, pid=pid)

        after = json.loads(state_file.read_text(encoding="utf-8"))["rr_cursor"]
        assert again.key_id == first.key_id
        assert again.reused is True
        assert (before, after) == (first.key_id, first.key_id)
        assert len(kp.pool_status()["reservations"]) == 1


# ── (б) background / card isolation ────────────────────────────────────────


class TestBackgroundIsolation:
    def test_background_resolution_uses_key_one(self, tmp_path):
        write_keys(tmp_path)
        as_background()
        out = kp.apply_card_key(runtime())
        assert out["api_key"] == "KEY_BG_111"
        assert out["key_pool"]["key_id"] == 1
        assert out["key_pool"]["role"] == "background"

    def test_card_resolution_never_returns_the_background_key(self, tmp_path):
        write_keys(tmp_path, card_keys=1)
        as_card("t_a", 1)
        out = kp.apply_card_key(runtime())
        assert out["api_key"] != "KEY_BG_111"
        assert out["key_pool"]["role"] == "card"
        assert out["key_pool"]["key_id"] != 1

    def test_card_never_gets_key_one_even_when_it_is_the_only_free_one(
        self, tmp_path, live_pid
    ):
        """Exhaustion must fail, not degrade onto the background account."""
        write_keys(tmp_path, card_keys=1)
        held = live_pid()
        kp.reserve_for_card("t_first", 1, pid=held)
        kp.reset_process_cache()
        as_card("t_second", 2)
        with pytest.raises(kp.KeyPoolError):
            kp.apply_card_key(runtime())
        # And the shared env key was NOT used as a quieter fallback either.
        assert kp.background_key().key_id == 1

    def test_background_and_card_disagree_by_construction(self, tmp_path):
        write_keys(tmp_path)
        as_card("t_a", 1)
        card_key = kp.apply_card_key(runtime())["api_key"]
        as_background()
        background_key = kp.apply_card_key(runtime())["api_key"]
        assert card_key != background_key

    def test_card_keys_never_include_the_background_id(self, tmp_path):
        write_keys(tmp_path, card_keys=4)
        cfg = kp.load_config()
        assert cfg.background_key_id == 1
        assert 1 not in cfg.card_key_ids
        assert cfg.card_key_ids == [2, 3, 4, 5]

    def test_auxiliary_resolution_inherits_the_reserved_key(self, tmp_path):
        """Aux clients read ``agent.api_key`` → ``set_runtime_main``.

        Proven here at the contract level: the value the main agent stores is
        the resolved ``api_key``, so every consumer of ``set_runtime_main``'s
        snapshot sees the same reserved key without its own reservation.

        (Review N4: the "delegated" half of this name used to be a lie — the
        body never entered ``delegated_child_context()``. The real delegated
        cases are pinned in :class:`TestDelegatedChildOfACard`; what remains
        here is the in-process auxiliary resolution, named for what it is.)
        """
        write_keys(tmp_path)
        as_card("t_a", 1)
        resolved = kp.apply_card_key(runtime())
        agent_api_key = resolved.get("api_key")  # what agent_init assigns
        assert agent_api_key.startswith("KEY_CARD_")

        # A second resolution in the same process (aux client resolving its own
        # runtime) must not take a different key.
        again = kp.apply_card_key(runtime())
        assert again["api_key"] == agent_api_key
        assert len(kp.pool_status()["reservations"]) == 1


# ── (ж) a delegated child of a card IS the card (review N3) ────────────────


class TestDelegatedChildOfACard:
    """Review N3: N1's gate sent every ``delegate_task`` child to the background.

    ``design/F2-design-and-implementation.md`` («Одна карточка = один ключ»)
    files a delegated helper under "другой процесс той же карточки": it adopts
    the card's reservation (``attached_pids``) and keeps the card's "never hop
    mid-run" discipline. ``is_dispatcher_owned_worker_context()`` answers a
    different question ("does this execution *own* the card?") — correct for
    ``kanban_stop``, wrong as the pool's context half. It exported three
    defects:

    * layer 2 — ``resolve_runtime_provider`` inside the child resolved to the
      background key, so card work ran on key #1 and an 8-card host put every
      card's children on the background account;
    * layer 3 — ``active_card_binding()`` returned ``None`` inside the child,
      so ``try_activate_fallback`` / ``recover_with_credential_pool`` stopped
      blocking and a 401/429 in a child silently moved it to another account's
      key — cancelling the reservation;
    * the cron case must NOT regress: a cron job fired inside a worker is not
      the card's work, and its child must stay on the background key.
    """

    def test_delegated_child_resolves_to_the_cards_key(self, tmp_path):
        """Layer 2: the child inherits the card's key, not the background key."""
        write_keys(tmp_path)
        as_card("t_worker", 1)

        from agent.delegation_context import delegated_child_context

        card_key = kp.apply_card_key(runtime())["api_key"]
        assert card_key.startswith("KEY_CARD_")

        with delegated_child_context():
            child_out = kp.apply_card_key(runtime())
        assert child_out["api_key"] == card_key
        assert child_out["key_pool"]["role"] == "card"
        # One card = one key: the child took no second reservation.
        status = kp.pool_status()
        assert [r["key_id"] for r in status["reservations"]] == [
            child_out["key_pool"]["key_id"]
        ]

    def test_delegated_child_keeps_the_binding_for_its_consumers(self, tmp_path):
        """Layer 3 accessor: ``active_card_binding`` is visible inside the child."""
        write_keys(tmp_path)
        as_card("t_worker", 1)
        kp.apply_card_key(runtime())

        from agent.delegation_context import (
            delegated_child_context,
            is_dispatcher_owned_worker_context,
        )

        with delegated_child_context():
            # The coarse predicate stays False — kanban_stop relies on that and
            # the pool must not "fix" it by widening the shared predicate.
            assert is_dispatcher_owned_worker_context() is False
            # The pool's narrow fence must not treat the child as foreign.
            # Asserted through the pool's own probe, not the named predicate:
            # the live tree (release/t_b0291572-owui-origin-binding) ships the
            # _NON_DISPATCHER_OWNED_CONTEXT state without that accessor, and
            # importing it here turned a correct pool red (K7).
            assert kp._narrow_non_dispatcher_owned() is False
            binding = kp.active_card_binding()
        assert binding is not None
        assert binding.role == "card"
        assert binding.task_id == "t_worker"

    def test_delegated_child_cannot_fall_back_to_another_key(self, tmp_path):
        """Layer 3, the dangerous half: no silent mid-run key hop in a child."""
        write_keys(tmp_path)
        as_card("t_worker", 1)
        card_key = kp.apply_card_key(runtime())["api_key"]

        from agent.chat_completion_helpers import try_activate_fallback
        from agent.delegation_context import delegated_child_context

        class _ChildAgent:
            """A delegated child holding the card key and the parent's chain."""

            _immutable_route = None
            _model_explicitly_selected = False
            provider = "custom"
            model = "deepseek-v4-flash"
            base_url = BASE_URL
            api_key = card_key
            _explicit_model_unavailable = False
            _explicit_model_unavailable_reason = ""

            def _buffer_status(self, _msg):
                pass

        child = _ChildAgent()
        with delegated_child_context():
            blocked = try_activate_fallback(child)

        assert blocked is False
        assert child.api_key == card_key, "a delegated child never re-keys mid-run"
        # The fork marked ``_explicit_model_unavailable`` so ITS retry loop stopped
        # asking; this tree has no such attribute and no consumer for it. Same
        # substance, tree-native proof: the guard returned before touching the
        # chain (the fake has no ``_fallback_index``, so a fallback attempt would
        # have raised AttributeError) and the route is untouched.
        assert child.provider == "custom"
        assert child.model == "deepseek-v4-flash"

    def test_delegated_child_401_marks_the_cards_key(self, tmp_path):
        """D3a preserved: a sibling of the card MAY report the key dead.

        Before the fix ``active_card_binding()`` was ``None`` in the child, so
        its 401 returned early and the dead key went unrecorded — the next card
        received it again.
        """
        write_keys(tmp_path)
        as_card("t_worker", 1)
        kp.apply_card_key(runtime())

        from agent import agent_runtime_helpers as helpers
        from agent.delegation_context import delegated_child_context

        class _Agent:
            provider = "custom"
            base_url = BASE_URL
            api_key = kp.current_binding().api_key
            _credential_pool = None
            _credential_pool_entry_id = None

            def _swap_credential(self, entry):  # pragma: no cover - must not run
                raise AssertionError("a card never swaps credentials mid-run")

        with delegated_child_context():
            recovered, _ = helpers.recover_with_credential_pool(
                _Agent(), status_code=401, has_retried_429=False,
            )

        assert recovered is False
        failures = kp.pool_status()["failed_keys"]
        assert [f["key_id"] for f in failures] == [kp.current_binding().key_id]

    def test_cron_child_of_a_worker_stays_on_the_background_key(self, tmp_path):
        """The nested case the naive "delegated ⇒ card" fix broke.

        A cron job fired inside a worker is not the card's work; its delegated
        child is even further from it. Both must stay on the background key.
        """
        write_keys(tmp_path)
        as_card("t_worker", 1)
        kp.apply_card_key(runtime())

        from agent.delegation_context import (
            delegated_child_context,
            non_dispatcher_owned_context,
        )

        with non_dispatcher_owned_context():
            with delegated_child_context():
                out = kp.apply_card_key(runtime())
                binding = kp.active_card_binding()
        assert out["api_key"] == "KEY_BG_111"
        assert out["key_pool"]["role"] == "background"
        assert binding is None


# ── (е) who counts as a card: dispatcher envelope + owned context ──────────


class TestCardIdentityGate:
    """Review N1: the card branch must not turn on for a bare task-id env.

    The repository states three times that ``HERMES_KANBAN_TASK`` alone is not
    proof of a worker (``agent/kanban_stop.py`` two fences,
    ``agent/delegation_context.py``). The pool ignored all of them, so:

    * a long-lived gateway/chat process that inherited a stale task id took a
      CARD key and held it on the TTL — background work on a card account,
      inverting owner requirement #3;
    * a cron job fired in-process from a worker (``cronjob(action="run")``,
      wrapped by ``cron/scheduler.py`` in ``non_dispatcher_owned_context``)
      resolved to the worker's card key, and a 401 from that unrelated job
      then quarantined the LIVE card's key via ``mark_current_key_failed``.
    """

    def test_bare_task_env_without_envelope_is_background(self, tmp_path):
        write_keys(tmp_path, card_keys=3)
        as_bare_task_env("t_dead_card")

        # The repo's own fence agrees this is not a worker.
        from agent.kanban_stop import is_dispatcher_worker_env

        assert is_dispatcher_worker_env() is False

        out = kp.apply_card_key(runtime())
        assert out["api_key"] == "KEY_BG_111"
        assert out["key_pool"]["role"] == "background"
        assert out["key_pool"]["key_id"] == 1
        assert kp.pool_status()["reservations"] == [], (
            "a stale task id must not occupy a card key for the whole TTL"
        )

    def test_cron_inside_a_worker_resolves_to_the_background_key(
        self, tmp_path
    ):
        """Full envelope, but ``non_dispatcher_owned_context()`` → not a card."""
        write_keys(tmp_path, card_keys=3)
        as_card("t_worker", 1)
        from agent.delegation_context import non_dispatcher_owned_context
        from agent.kanban_stop import is_dispatcher_worker_env

        # The env-only fence still says "worker" here — which is exactly why
        # one predicate is not enough.
        assert is_dispatcher_worker_env() is True

        with non_dispatcher_owned_context():
            out = kp.apply_card_key(runtime())
        assert out["api_key"] == "KEY_BG_111"
        assert out["key_pool"]["role"] == "background"
        assert kp.pool_status()["reservations"] == []

    def test_cron_401_does_not_quarantine_the_workers_card_key(
        self, tmp_path
    ):
        """The damage half of N1(b): a stranger's 401 burned the card's key."""
        write_keys(tmp_path, card_keys=3)
        as_card("t_worker", 1)
        card_out = kp.apply_card_key(runtime())
        card_key = card_out["api_key"]
        assert card_key.startswith("KEY_CARD_")

        from agent.delegation_context import non_dispatcher_owned_context
        from agent import agent_runtime_helpers as helpers

        class _Agent:
            provider = "custom"
            base_url = BASE_URL
            api_key = "KEY_BG_111"
            _credential_pool = None
            _credential_pool_entry_id = None

            def _swap_credential(self, entry):  # pragma: no cover
                raise AssertionError("never swap")

        with non_dispatcher_owned_context():
            helpers.recover_with_credential_pool(
                _Agent(), status_code=401, has_retried_429=False,
            )

        # Before the fix this read ``failed_keys == [card_key_id]`` while the
        # card was still the one holding it, and the card's next resolution
        # raised ``KeyPoolError``.
        assert kp.pool_status()["failed_keys"] == []
        assert kp.current_binding().status == "active"
        # The card itself still works and still holds its own key.
        again = kp.apply_card_key(runtime())
        assert again["api_key"] == card_key

    def test_mark_current_key_failed_is_context_scoped_on_its_own(
        self, tmp_path
    ):
        """Pin the accessor directly, not only through the helper's guard.

        ``recover_with_credential_pool`` short-circuits on
        ``active_card_binding()`` before it ever calls
        ``mark_current_key_failed``, so a test that goes only through the
        helper leaves the quarantine path itself unverified: reverting
        ``mark_current_key_failed`` to the raw ``current_binding()`` (a
        mutation the corrected probe applies) still passed. This test calls
        the quarantine entry point in the foreign context and requires it to
        refuse on its own.
        """
        write_keys(tmp_path, card_keys=3)
        as_card("t_worker", 1)
        kp.apply_card_key(runtime())
        held = kp.current_binding()
        assert held is not None and held.status == "active"

        from agent.delegation_context import non_dispatcher_owned_context

        with non_dispatcher_owned_context():
            assert kp.active_card_binding() is None
            assert kp.mark_current_key_failed(401, reason="foreign_job") is None

        # Nothing was quarantined and the card's own key is intact.
        assert kp.pool_status()["failed_keys"] == []
        assert kp.current_binding().status == "active"
        assert kp.mark_current_key_failed(401, reason="own_job") is not None

    def test_card_process_predicate_needs_both_halves(self, tmp_path):
        """``is_card_process`` is the conjunction, and says so on both sides."""
        write_keys(tmp_path, card_keys=3)
        from agent.delegation_context import (
            enter_non_dispatcher_owned_context,
            exit_non_dispatcher_owned_context,
        )

        as_background()
        assert kp.is_card_process() is False

        as_bare_task_env("t_dead")
        assert kp.is_card_process() is False, "bare envelope is not a card"

        as_card("t_real", 1)
        assert kp.is_card_process() is True

        token = enter_non_dispatcher_owned_context()
        try:
            assert kp.is_card_process() is False, "in-process cron is not the card"
        finally:
            exit_non_dispatcher_owned_context(token)
        assert kp.is_card_process() is True, "context flag must be restored"


class TestLiveTreeIdentityProbe:
    """K7: the pool must decide the card identity on the tree it is shipped into.

    The live tree (``release/t_b0291572-owui-origin-binding``) ships
    ``_NON_DISPATCHER_OWNED_CONTEXT`` plus the ``non_dispatcher_owned_context``
    / ``enter_`` / ``exit_`` helpers, but NOT a named
    ``is_non_dispatcher_owned_context()`` accessor. The pool imported that name
    inside a bare ``except Exception: return False``, so on the live tree every
    card silently degraded to "background" and ran on key #1 — one shared key
    for the whole board, and no reservation at all.
    """

    def test_probe_resolves_when_the_named_accessor_is_absent(
        self, tmp_path, monkeypatch
    ):
        """Tier 2: the ContextVar answers when the accessor does not exist."""
        write_keys(tmp_path, card_keys=3)
        from agent import delegation_context as dc

        # Reproduce the live tree exactly: state present, accessor gone.
        monkeypatch.delattr(dc, "is_non_dispatcher_owned_context", raising=False)
        assert not hasattr(dc, "is_non_dispatcher_owned_context")

        assert kp._narrow_non_dispatcher_owned() is False
        with dc.non_dispatcher_owned_context():
            assert kp._narrow_non_dispatcher_owned() is True

    def test_card_gets_a_card_key_when_the_accessor_is_absent(
        self, tmp_path, monkeypatch
    ):
        """The defect itself: the card branch kept working on the live tree."""
        write_keys(tmp_path, card_keys=3)
        from agent import delegation_context as dc

        monkeypatch.delattr(dc, "is_non_dispatcher_owned_context", raising=False)

        as_card("t_live", 1)
        out = kp.apply_card_key(runtime())
        assert out["api_key"].startswith("KEY_CARD_"), (
            "on the live tree the card must still take a card key, not key #1"
        )
        assert out["key_pool"]["role"] == "card"
        assert len(kp.pool_status()["reservations"]) == 1

    def test_unresolvable_probe_fails_closed(self, tmp_path, monkeypatch):
        """Neither tier resolves -> raise, never answer "background"."""
        write_keys(tmp_path, card_keys=3)
        from agent import delegation_context as dc

        monkeypatch.delattr(dc, "is_non_dispatcher_owned_context", raising=False)
        monkeypatch.delattr(dc, "_NON_DISPATCHER_OWNED_CONTEXT", raising=False)

        with pytest.raises(kp.KeyPoolError):
            kp._narrow_non_dispatcher_owned()

    def test_cron_still_resolves_to_background_without_the_accessor(
        self, tmp_path, monkeypatch
    ):
        """K7 must not regress N1(b) on the live tree: cron != the card."""
        write_keys(tmp_path, card_keys=3)
        from agent import delegation_context as dc

        monkeypatch.delattr(dc, "is_non_dispatcher_owned_context", raising=False)

        as_card("t_live2", 1)
        with dc.non_dispatcher_owned_context():
            out = kp.apply_card_key(runtime())
        assert out["api_key"] == "KEY_BG_111"
        assert kp.pool_status()["reservations"] == []


# ── (в) release ────────────────────────────────────────────────────────────


class TestRelease:
    def test_release_for_task_frees_the_key(self, tmp_path):
        write_keys(tmp_path)
        res = kp.reserve_for_card("t_a", 1, pid=os.getpid())
        assert kp.pool_status()["reservations"]
        released = kp.release_for_task("t_a")
        assert [r["key_id"] for r in released] == [res.key_id]
        assert kp.pool_status()["reservations"] == []
        # The freed key is genuinely reusable: with the rotation the ring moves
        # on to the next least-loaded key (#2 is at 0/1 and wins again only
        # when it is the cursor's successor — for the single-key case below the
        # freed key is the only one, so the card must land back on it).
        write_keys(tmp_path, card_keys=1)
        res = kp.reserve_for_card("t_c", 3, pid=os.getpid())
        kp.release_for_task("t_c")
        nxt = kp.reserve_for_card("t_b", 2, pid=os.getpid())
        assert nxt.key_id == res.key_id

    def test_release_is_task_scoped_not_pid_scoped(self, tmp_path, live_pid):
        """Terminal transitions free the key whoever performs them.

        A reviewer or an operator profile closing/blocking a card must return
        that card's key to the pool; otherwise it stays reserved until the TTL
        backstop and the pool shrinks. Safety comes from the task id match —
        the next test pins that a *different* task's key is untouched.
        """
        write_keys(tmp_path)
        holder = live_pid()
        res = kp.reserve_for_card("t_a", 1, pid=holder)
        released = kp.release_for_task("t_a", pid=os.getpid())
        assert [r["key_id"] for r in released] == [res.key_id]
        assert kp.pool_status()["reservations"] == []

    def test_release_never_touches_another_task(self, tmp_path, live_pid):
        write_keys(tmp_path)
        kp.reserve_for_card("t_mine", 1, pid=live_pid())
        other = kp.reserve_for_card("t_other", 2, pid=live_pid())
        assert kp.release_for_task("t_mine") != []
        remaining = [r["task_id"] for r in kp.pool_status()["reservations"]]
        assert remaining == ["t_other"]
        assert other.key_id in [r["key_id"] for r in kp.pool_status()["reservations"]]

    def test_release_for_pid_frees_only_this_process(self, tmp_path, live_pid):
        write_keys(tmp_path)
        mine, other = live_pid(), live_pid()
        kp.reserve_for_card("t_mine", 1, pid=mine)
        kp.reserve_for_card("t_other", 2, pid=other)
        released = kp.release_for_pid(pid=mine)
        assert [r["task_id"] for r in released] == ["t_mine"]
        remaining = [r["task_id"] for r in kp.pool_status()["reservations"]]
        assert remaining == ["t_other"]

    def test_dead_pid_reservation_is_reaped(self, tmp_path):
        """A worker that dies without releasing loses its key on next reserve."""
        write_keys(tmp_path, card_keys=1)
        dead = _unused_pid()
        kp.reserve_for_card("t_dead", 1, pid=dead)
        # Sanity: the pid really is not alive.
        assert kp._pid_alive(dead) is False
        fresh = kp.reserve_for_card("t_alive", 2, pid=os.getpid())
        assert fresh.key_id == 2  # the only card key, recovered from the corpse
        remaining = [r["task_id"] for r in kp.pool_status()["reservations"]]
        assert remaining == ["t_alive"]

    def test_ttl_never_reaps_a_live_pid(self, tmp_path, monkeypatch):
        """A live card keeps its key even past the lease (review defect D2).

        The lease is a backstop for a worker that vanished, not a budget that
        revokes a working card's key. Before the fix, an expired `expires_at`
        let the NEXT card take the SAME key while the first was still running —
        two live runs on one account, the exact thing this mission prevents.
        This test rewinds the expiry of a reservation whose pid is alive (this
        process) and proves the key is not handed out again.
        """
        write_keys(tmp_path, card_keys=1)
        monkeypatch.setenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", "1")
        res = kp.reserve_for_card("t_a", 1, pid=os.getpid())
        assert res.key_id == 2

        # Rewind the stored expiry instead of sleeping.
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for entry in state["reservations"].values():
            entry["expires_at"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")

        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_b", 2, pid=os.getpid())
        assert "never handed to a card" in str(exc.value)

        # The live card's reservation is intact and is not reported stale.
        held = kp.pool_status()["reservations"]
        assert [(r["task_id"], r["key_id"], r["stale"]) for r in held] == [
            ("t_a", 2, False)
        ]

    def test_ttl_expiry_reaps_a_reservation_with_no_usable_pid(
        self, tmp_path, monkeypatch
    ):
        """With no pid to probe, the lease is the only evidence left — TTL decides."""
        write_keys(tmp_path, card_keys=1)
        monkeypatch.setenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", "1")
        kp.reserve_for_card("t_a", 1, pid=os.getpid())

        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for entry in state["reservations"].values():
            entry["pid"] = 0          # unrecorded owner
            entry["expires_at"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")

        nxt = kp.reserve_for_card("t_b", 2, pid=os.getpid())
        assert nxt.key_id == 2
        remaining = [r["task_id"] for r in kp.pool_status()["reservations"]]
        assert remaining == ["t_b"]

    def test_resolution_renews_the_lease(self, tmp_path, monkeypatch):
        """Every successful resolution extends the live card's lease (D2, other half)."""
        write_keys(tmp_path, card_keys=1)
        monkeypatch.setenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", "60")
        as_card("t_a", 1)
        kp.apply_card_key(runtime())

        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        before = int(next(iter(state["reservations"].values()))["expires_at"])
        for entry in state["reservations"].values():
            entry["expires_at"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")

        kp.reset_process_cache()          # fresh process, same card (adopts)
        kp.apply_card_key(runtime())
        after_state = json.loads(state_file.read_text(encoding="utf-8"))
        after = int(next(iter(after_state["reservations"].values()))["expires_at"])
        assert after > int(time.time()), "the lease was not extended"
        assert after > before - 10

    def test_renewal_refuses_a_foreign_reservation(self, tmp_path, live_pid):
        """Renewing is the one mutation that could extend someone else's key."""
        write_keys(tmp_path, card_keys=2)
        holder = live_pid()
        res = kp.reserve_for_card("t_other", 1, pid=holder)
        assert kp.renew_for_card("t_other", res.key_id, pid=os.getpid()) is False
        assert kp.renew_for_card("t_mine", res.key_id, pid=holder) is False
        assert kp.renew_for_card("t_other", res.key_id, pid=holder) is True

    def test_sweep_reports_what_it_reaped(self, tmp_path):
        write_keys(tmp_path)
        kp.reserve_for_card("t_dead", 1, pid=_unused_pid())
        dropped = kp.sweep()
        assert [d["task_id"] for d in dropped] == ["t_dead"]
        assert dropped[0]["released_reason"] == "pid_gone"
        assert kp.pool_status()["reservations"] == []

    def test_complete_task_by_the_key_holder_defers_to_process_exit(
        self, tmp_path, monkeypatch
    ):
        """The worker that completes the card keeps its key for its last turn.

        A kanban worker emits one more model turn after ``kanban_complete``
        (the final summary message). Freeing the key at completion time would
        let another card reserve the same account while this process is still
        using it — the exact overlap the mission forbids. The release therefore
        happens on process exit; the PID/TTL backstop covers a hang.
        """
        write_keys(tmp_path)
        _allow_any_assignee(monkeypatch)
        from hermes_cli import kanban_db as kb

        conn = _connect(tmp_path / "kanban.db")
        try:
            if _init_db_takes_conn(kb):
                kb.init_db(conn)
            else:
                kb.init_db()
            task_id = kb.create_task(conn, title="probe")
            as_card(task_id, 7)
            kp.apply_card_key(runtime())
            held = [r["task_id"] for r in kp.pool_status()["reservations"]]
            assert held == [task_id]

            assert kb.complete_task(conn, task_id, summary="done") is True
            still = kp.pool_status()["reservations"]
            assert [r["task_id"] for r in still] == [task_id], (
                "the key holder must keep its key until the process exits"
            )

            # Process exit is the real release point.
            assert kp.release_for_pid() != []
            assert kp.pool_status()["reservations"] == []
        finally:
            conn.close()

    def test_complete_task_by_a_foreign_process_releases_immediately(
        self, tmp_path, monkeypatch, live_pid
    ):
        """A reviewer / operator CLI frees the worker's key on completion.

        This process is NOT the card's key holder, so there is no further turn
        of its own to protect: the key must go back at once instead of lingering
        until the TTL backstop.
        """
        write_keys(tmp_path)
        _allow_any_assignee(monkeypatch)
        from hermes_cli import kanban_db as kb

        conn = _connect(tmp_path / "kanban.db")
        try:
            if _init_db_takes_conn(kb):
                kb.init_db(conn)
            else:
                kb.init_db()
            task_id = kb.create_task(conn, title="probe")
            # The card's worker: another live process, not this one.
            kp.reserve_for_card(task_id, 7, pid=live_pid())
            assert len(kp.pool_status()["reservations"]) == 1

            assert kb.complete_task(conn, task_id, summary="done") is True
            assert kp.pool_status()["reservations"] == []
        finally:
            conn.close()

    def test_block_task_by_a_foreign_process_releases_immediately(
        self, tmp_path, monkeypatch, live_pid
    ):
        write_keys(tmp_path)
        _allow_any_assignee(monkeypatch)
        from hermes_cli import kanban_db as kb

        conn = _connect(tmp_path / "kanban.db")
        try:
            if _init_db_takes_conn(kb):
                kb.init_db(conn)
            else:
                kb.init_db()
            task_id = kb.create_task(conn, title="probe")
            kp.reserve_for_card(task_id, 3, pid=live_pid())
            assert len(kp.pool_status()["reservations"]) == 1

            assert kb.block_task(conn, task_id, reason="needs input",
                                 kind="needs_input") is True
            assert kp.pool_status()["reservations"] == []
        finally:
            conn.close()


# ── (г) zero-configuration fallback ────────────────────────────────────────


class TestZeroConfigFallback:
    def test_no_key_file_means_no_pool_and_untouched_runtime(self, tmp_path):
        os.environ[kp.ENV_KEYPOOL] = str(tmp_path / "absent.json")
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "absent.state.json")
        kp.reset_process_cache()

        assert kp.is_enabled() is False
        assert kp.load_config() is None
        assert kp.background_key() is None

        as_card("t_a", 1)
        out = kp.apply_card_key(runtime())
        assert out["api_key"] == "SHARED_ENV_KEY"
        assert "key_pool" not in out

    def test_single_key_file_is_inert(self, tmp_path):
        """Fewer than two keys → the pool must not engage at all (review D5).

        The original test was named ``…_is_inert`` but pinned the opposite: a
        file with only key #1 made every card resolution raise
        "key pool has no card keys" while ``is_enabled`` said True. The owner's
        requirement 5 is "without keys the behaviour is exactly today's", so a
        file that has nothing to distribute must leave the runtime untouched.
        """
        path = tmp_path / "kanban-keypool.json"
        path.write_text(json.dumps({
            "version": 1,
            "base_url": BASE_URL,
            "background_key_id": 1,
            "keys": [{"id": 1, "role": "background", "api_key": "ONLY_KEY"}],
        }), encoding="utf-8")
        os.environ[kp.ENV_KEYPOOL] = str(path)
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "s.json")
        kp.reset_process_cache()

        assert kp.is_enabled() is False
        status = kp.pool_status()
        assert status["inactive_reason"] == "no_card_keys"
        assert status["card_key_ids"] == []
        assert status["key_count"] == 1

        as_card("t_a", 1)
        out = kp.apply_card_key(runtime())
        assert out["api_key"] == "SHARED_ENV_KEY"
        assert "key_pool" not in out

        # An explicit reservation still explains itself instead of pretending
        # the pool is merely contended.
        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_a", 1)
        assert "not in force (no_card_keys)" in str(exc.value)

    def test_two_keys_without_a_card_role_is_inert(self, tmp_path):
        """A second key that is not role=card distributes nothing → inert."""
        path = tmp_path / "kanban-keypool.json"
        path.write_text(json.dumps({
            "version": 1,
            "base_url": BASE_URL,
            "background_key_id": 1,
            "keys": [
                {"id": 1, "role": "background", "api_key": "KEY_BG_111"},
                {"id": 2, "role": "background", "api_key": "KEY_2"},
            ],
        }), encoding="utf-8")
        os.environ[kp.ENV_KEYPOOL] = str(path)
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "s.json")
        kp.reset_process_cache()
        # load_config rejects a second background key outright, so this file is
        # unusable rather than merely empty — either way the pool is off.
        assert kp.is_enabled() is False
        as_card("t_a", 1)
        assert kp.apply_card_key(runtime())["api_key"] == "SHARED_ENV_KEY"

    def test_key_file_without_a_base_url_never_applies(self, tmp_path):
        """Review D6: an unnamed endpoint would leak the key to any host."""
        write_keys(tmp_path, base_url="")
        cfg = kp.load_config()
        assert cfg is not None
        assert cfg.inactive_reason == "no_base_url"
        assert kp.is_enabled() is False

        as_card("t_a", 1)
        for url in (
            BASE_URL,
            "https://api.openai.com/v1",
            "https://api.anthropic.com",
            "https://openrouter.ai/api/v1",
        ):
            out = kp.apply_card_key(runtime("SHARED_ENV_KEY", base_url=url))
            assert out["api_key"] == "SHARED_ENV_KEY", url
            assert "key_pool" not in out, url

    def test_malformed_key_file_degrades_to_unpooled(self, tmp_path):
        path = tmp_path / "kanban-keypool.json"
        path.write_text("{ this is not json", encoding="utf-8")
        os.environ[kp.ENV_KEYPOOL] = str(path)
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "s.json")
        kp.reset_process_cache()

        assert kp.load_config() is None
        as_card("t_a", 1)
        assert kp.apply_card_key(runtime())["api_key"] == "SHARED_ENV_KEY"

    def test_other_endpoints_are_left_alone(self, tmp_path):
        """The pool owns one endpoint; a fallback/secondary keeps its key."""
        write_keys(tmp_path)
        as_card("t_a", 1)
        other = runtime("OTHER_KEY", base_url="https://api.other-host.example/v1")
        out = kp.apply_card_key(other)
        assert out["api_key"] == "OTHER_KEY"
        assert "key_pool" not in out

    def test_callable_api_key_is_left_alone(self, tmp_path):
        """A ``key_cmd``/Entra bearer is minted per request — not a pooled key."""
        write_keys(tmp_path)
        as_card("t_a", 1)
        cb = lambda: "minted"
        out = kp.apply_card_key({**runtime(), "api_key": cb})
        assert out["api_key"] is cb

    def test_absent_pool_leaves_resolve_runtime_provider_identical(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: the public resolver returns the same key as before."""
        os.environ[kp.ENV_KEYPOOL] = str(tmp_path / "absent.json")
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "absent.state.json")
        kp.reset_process_cache()

        from hermes_cli import runtime_provider as rp

        monkeypatch.setattr(
            rp, "_resolve_runtime_provider_impl",
            lambda **kw: {"provider": "custom", "base_url": BASE_URL,
                          "api_key": "SHARED_ENV_KEY", "api_mode": "chat_completions"},
        )
        as_card("t_a", 1)
        out = rp.resolve_runtime_provider(requested="custom")
        assert out["api_key"] == "SHARED_ENV_KEY"
        assert "key_pool" not in out


# ── (д) rollback ───────────────────────────────────────────────────────────


class TestRollback:
    def test_off_then_on(self, tmp_path):
        write_keys(tmp_path)
        assert kp.is_enabled() is True

        marker = kp.set_enabled(False)
        assert marker.exists()
        assert kp.is_enabled() is False
        as_card("t_a", 1)
        assert kp.apply_card_key(runtime())["api_key"] == "SHARED_ENV_KEY"

        kp.set_enabled(True)
        assert kp.is_enabled() is True
        out = kp.apply_card_key(runtime())
        assert out["api_key"].startswith("KEY_CARD_")

    def test_env_override_off_wins(self, tmp_path):
        write_keys(tmp_path)
        os.environ[kp.ENV_KEYPOOL] = "off"
        kp.reset_process_cache()
        assert kp.is_enabled() is False

    def test_status_text_names_the_env_override_not_the_marker(self, tmp_path, capsys):
        """D7: "off" must say WHY, and the three causes must stay distinct.

        The status branch used to test ``status["env_override"]`` truthiness,
        so ``HERMES_KANBAN_KEYPOOL=off`` and the rollback sentinel both took
        the env branch — and the marker case printed "disabled by env" for a
        rollback that was not an env override at all. No test asserted the
        *text*, so the mutation survived; this one does.
        """
        from hermes_cli.subcommands.kanban_keypool import _cmd_status

        write_keys(tmp_path)
        os.environ[kp.ENV_KEYPOOL] = "off"
        kp.reset_process_cache()
        assert _cmd_status(kp, False) == 0
        out = capsys.readouterr().out
        assert "disabled by env" in out, out
        assert str(kp.ENV_KEYPOOL) in out, out
        assert "rollback sentinel" not in out, out

        # Same, via the rollback sentinel: a different sentence.
        os.environ[kp.ENV_KEYPOOL] = str(tmp_path / "kanban-keypool.json")
        kp.reset_process_cache()
        kp.set_enabled(False)
        assert _cmd_status(kp, False) == 0
        out = capsys.readouterr().out
        assert "rollback sentinel" in out, out
        assert "disabled by env" not in out, out
        kp.set_enabled(True)

    def test_enabled_false_in_file(self, tmp_path):
        write_keys(tmp_path, enabled=False)
        assert kp.is_enabled() is False
        as_card("t_a", 1)
        assert kp.apply_card_key(runtime())["api_key"] == "SHARED_ENV_KEY"

    def test_rollback_does_not_yank_a_live_reservation(self, tmp_path):
        """A running card keeps its key — that is the whole point of a hold."""
        write_keys(tmp_path)
        res = kp.reserve_for_card("t_live", 1, pid=os.getpid())
        kp.set_enabled(False)
        status = kp.pool_status()
        assert status["enabled"] is False
        assert [r["key_id"] for r in status["reservations"]] == [res.key_id]

    def test_cli_off_and_on_round_trip(self, tmp_path):
        write_keys(tmp_path)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        root = str(Path(__file__).resolve().parents[2])
        off = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", "keypool", "off", "--json"],
            env=env, capture_output=True, text=True, cwd=root, timeout=180, check=False,
        )
        assert off.returncode == 0, off.stderr
        assert json.loads(off.stdout)["enabled"] is False

        on = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", "keypool", "on", "--json"],
            env=env, capture_output=True, text=True, cwd=root, timeout=180, check=False,
        )
        assert on.returncode == 0, on.stderr
        assert json.loads(on.stdout)["enabled"] is True


# ── no silent mid-run swap + quarantine ────────────────────────────────────


class TestNoSilentKeySwap:
    def test_401_marks_the_key_bad_and_blocks_further_resolution(self, tmp_path):
        write_keys(tmp_path)
        as_card("t_a", 1)
        kp.apply_card_key(runtime())

        marked = kp.mark_current_key_failed(401, reason="auth_rejected")
        assert marked is not None
        assert marked["failure"]["status_code"] == 401
        assert marked["failure"]["degradation"] == "card_kept_its_key_then_failed"

        # The same process must now refuse to resolve — not silently pick
        # another account.
        with pytest.raises(kp.KeyPoolError) as exc:
            kp.apply_card_key(runtime())
        assert "never switches keys mid-run" in str(exc.value)

    def test_dead_key_is_quarantined_for_the_next_task(self, tmp_path):
        write_keys(tmp_path, card_keys=2)
        kp.reserve_for_card("t_a", 1, pid=4001)
        # Mark through the public API on behalf of that pid.
        kp.mark_key_failed("t_a", 401, reason="auth_rejected", pid=4001)

        nxt = kp.reserve_for_card("t_b", 2, pid=4002)
        assert nxt.key_id == 3, "a 401 key must not be handed to the next task"

        status = kp.pool_status()
        assert [f["key_id"] for f in status["failed_keys"]] == [2]

    def test_429_quarantine_expires(self, tmp_path, monkeypatch):
        """A 429 keeps the key out until its window elapses, then lets it back.

        The window here is deliberately LONG (1h), not 1 second. It used to be
        ``1``, which made the test a race against the wall clock: the refusal
        below has to happen inside the window, and a single ``os.fsync`` in
        ``_write_state`` stalling for a second under load (Windows + antivirus,
        a busy suite) pushed the check past the expiry — the key was then
        legitimately free and no refusal could occur. That produced a flake
        unrelated to the pool's logic (measured: 1 failure in 8 full-suite
        runs; never reproducible in isolation). The second half of the test
        already proves expiry by rewinding ``quarantine_until``, so nothing is
        lost by removing the dependency on how fast the machine is.
        """
        write_keys(tmp_path, card_keys=2)
        monkeypatch.setenv("HERMES_KANBAN_KEYPOOL_QUARANTINE_SECONDS", "3600")
        kp.reserve_for_card("t_a", 1, pid=4101)
        kp.mark_key_failed("t_a", 429, reason="rate_limited", pid=4101)

        # card_keys=2 is essential here: with a single card key the refusal
        # below is indistinguishable from "no keys left at all", and a build
        # that does not quarantine a 429 at all would still pass. With two
        # keys the refusal can only mean the 429'd key #2 was held back, and
        # the recovery assertion can only mean it came back.
        moved = kp.reserve_for_card("t_b", 2, pid=4102)
        assert moved.key_id == 3, "a 429'd key must not be handed out"

        # Rewind the quarantine instead of sleeping.
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for record in state["failed_keys"].values():
            record["quarantine_until"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")

        again = kp.reserve_for_card("t_c", 3, pid=4103)
        assert again.key_id == 2

    def test_quarantine_does_not_free_a_live_cards_key(self, tmp_path, live_pid):
        """D1: at limit 1 quarantine used to delete the LIVE card's only slot.

        Production runs ``max_cards_per_key = 1``, so a key holds exactly one
        slot. ``mark_key_failed`` used to drop that slot; ``kept`` was then
        empty and the whole reservation record vanished. As soon as
        ``quarantine_until`` elapsed the key returned to circulation and was
        handed to a second card while the first was still working on it —
        the "one card = one key" invariant inverted.

        Existing tests could not catch this: they reserved with dead pids
        (4001/4101/4201) and/or used ``max_cards_per_key=2``. Here the owner
        is a REAL live pid and the limit is the production 1.
        """
        write_keys(tmp_path, card_keys=2, max_cards_per_key=1)
        owner = live_pid()
        held = kp.reserve_for_card("t_live", 162, pid=owner)
        assert held.key_id == 2

        marked = kp.mark_key_failed("t_live", 429, reason="rate_limited", pid=owner)
        assert marked is not None

        # The live card's slot must survive the quarantine, in the ``bad``
        # state so it still counts against the key's capacity.
        rows = kp.pool_status()["reservations"]
        assert [(r["task_id"], r["key_id"], r["status"]) for r in rows] == [
            ("t_live", 2, "bad"),
        ]
        key_row = next(k for k in kp.pool_status()["keys"] if k["key_id"] == 2)
        assert (key_row["used"], key_row["limit"]) == (1, 1), (
            "the quarantined key is still fully occupied by its live card"
        )

        # While the window lasts: the second card goes to the other key.
        nxt = kp.reserve_for_card("t_next", 163, pid=live_pid())
        assert nxt.key_id == 3

        # The decisive half — the window EXPIRES. The key is no longer
        # quarantined, but its only slot belongs to a card that is still
        # alive, so the key must stay unavailable; it may not be re-issued.
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for record in state["failed_keys"].values():
            record["quarantine_until"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")
        kp.reset_process_cache()

        with pytest.raises(kp.KeyPoolError) as exc:
            kp.reserve_for_card("t_third", 164, pid=live_pid())
        assert "#2: 1/1" in str(exc.value), str(exc.value)

        # And the card that owns the key still resolves to it itself.
        again = kp.reserve_for_card("t_live", 162, pid=owner)
        assert (again.key_id, again.reused) == (2, True)

        # Requirement 7 is untouched: a *new* card is kept off the rejected
        # key for the whole window, which is proven above (it went to #3).

    def test_the_quarantined_slot_is_freed_by_the_cards_own_release(
        self, tmp_path, live_pid
    ):
        """The backstop for the kept slot: the card's release, not the timer."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=1)
        owner = live_pid()
        kp.reserve_for_card("t_live", 1, pid=owner)
        kp.mark_key_failed("t_live", 429, reason="rate_limited", pid=owner)

        released = kp.release_for_task("t_live", pid=owner)
        assert [r["key_id"] for r in released] == [2]
        assert kp.pool_status()["reservations"] == []

        # Once the window lapses the key is legitimately free again and the
        # next card gets it — the release, not the quarantine, is what frees it.
        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for record in state["failed_keys"].values():
            record["quarantine_until"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")
        assert kp.reserve_for_card("t_after", 2, pid=live_pid()).key_id == 2

    def test_the_quarantined_slot_is_freed_when_the_live_owner_dies(
        self, tmp_path, live_pid
    ):
        """PID backstop: a killed worker must not pin the key forever."""
        write_keys(tmp_path, card_keys=1, max_cards_per_key=1)
        owner = live_pid()
        kp.reserve_for_card("t_live", 1, pid=owner)
        kp.mark_key_failed("t_live", 429, reason="rate_limited", pid=owner)

        live_pid.kill(owner)

        state_file = Path(os.environ[kp.ENV_KEYPOOL_STATE])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for record in state["failed_keys"].values():
            record["quarantine_until"] = int(time.time()) - 5
        state_file.write_text(json.dumps(state), encoding="utf-8")

        assert kp.reserve_for_card("t_after", 2, pid=live_pid()).key_id == 2

    def test_clear_quarantine(self, tmp_path):
        write_keys(tmp_path, card_keys=1)
        kp.reserve_for_card("t_a", 1, pid=4201)
        kp.mark_key_failed("t_a", 401, reason="revoked", pid=4201)
        with pytest.raises(kp.KeyPoolError):
            kp.reserve_for_card("t_b", 2, pid=4202)

        cleared = kp.clear_quarantine()
        assert [c["key_id"] for c in cleared] == [2]
        assert kp.reserve_for_card("t_c", 3, pid=4203).key_id == 2

    def test_pool_rotation_is_blocked_for_a_card(self, tmp_path, monkeypatch):
        """The 429/401 pool-rotation escape hatch is closed (agent layer)."""
        write_keys(tmp_path)
        as_card("t_a", 1)
        kp.apply_card_key(runtime())

        from agent import agent_runtime_helpers as helpers

        class _Pool:
            provider = "custom:letovo"

        class _Agent:
            provider = "custom"
            base_url = BASE_URL
            api_key = "KEY_CARD_2_222"
            _credential_pool = _Pool()
            _credential_pool_entry_id = None

            def _swap_credential(self, entry):  # pragma: no cover - must not run
                raise AssertionError("a card must never swap credentials mid-run")

        recovered, _ = helpers.recover_with_credential_pool(
            _Agent(), status_code=429, has_retried_429=False,
        )
        assert recovered is False
        assert kp.current_binding().status == "bad"
        failures = kp.pool_status()["failed_keys"]
        assert failures and failures[0]["status_code"] == 429

    def test_fallback_chain_is_blocked_for_a_card(self, tmp_path):
        write_keys(tmp_path)
        as_card("t_a", 1)
        kp.apply_card_key(runtime())

        from agent.chat_completion_helpers import try_activate_fallback

        class _Agent:
            _immutable_route = None
            _model_explicitly_selected = False
            provider = "custom"
            model = "deepseek-v4-flash"
            _explicit_model_unavailable = False
            _explicit_model_unavailable_reason = ""

            def _buffer_status(self, _msg):
                pass

        agent = _Agent()
        assert try_activate_fallback(agent) is False
        # Same substance as the fork's ``_explicit_model_unavailable`` marker,
        # tree-native: the guard returned before the chain was touched (the fake
        # has no ``_fallback_index``) and the route is untouched.
        assert agent.provider == "custom"
        assert agent.model == "deepseek-v4-flash"


# ── review defects D1 / D3a / D3b (fix card t_f6c4396f) ────────────────────


class TestFailClosedOnPoolErrors:
    """D1: a card must fail loudly, never ride the shared key.

    The original resolver swallowed every non-``KeyPoolError`` exception and
    returned the unpooled runtime, and set ``_binding_attempted`` *before* the
    reservation — so the second resolution in the same process handed the card
    the shared ``.env`` key. Both halves are pinned here.
    """

    def test_plumbing_error_on_a_card_raises_instead_of_degrading(
        self, tmp_path, monkeypatch
    ):
        """A crash inside the pool (ENOSPC, lock timeout) must not share a key."""
        write_keys(tmp_path)
        as_card("t_a", 1)

        def _boom(*_a, **_kw):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(kp, "_write_state", _boom)
        with pytest.raises(kp.KeyPoolError) as exc:
            kp.apply_card_key(runtime())
        assert "refusing to run the card on the shared key" in str(exc.value)

    def test_plumbing_error_via_the_public_resolver_raises(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: `resolve_runtime_provider` must not swallow it either."""
        write_keys(tmp_path)
        as_card("t_a", 1)

        from hermes_cli import runtime_provider as rp

        monkeypatch.setattr(
            rp, "_resolve_runtime_provider_impl",
            lambda **kw: {"provider": "custom", "base_url": BASE_URL,
                          "api_key": "SHARED_ENV_KEY", "api_mode": "chat_completions"},
        )
        monkeypatch.setattr(
            kp, "_write_state",
            lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space")),
        )
        with pytest.raises(kp.KeyPoolError):
            rp.resolve_runtime_provider(requested="custom")

    def test_second_resolution_after_exhaustion_does_not_share_the_key(
        self, tmp_path, live_pid
    ):
        """The exact D1 reproduction: two calls, second must not degrade."""
        write_keys(tmp_path, card_keys=1)
        held = live_pid()
        kp.reserve_for_card("t_other", 1, pid=held)   # the only card key is gone
        as_card("t_a", 1)

        with pytest.raises(kp.KeyPoolError):
            kp.apply_card_key(runtime())
        # Before the fix this returned the SHARED_ENV_KEY runtime.
        with pytest.raises(kp.KeyPoolError):
            kp.apply_card_key(runtime())
        assert kp.current_binding() is None, "a failed card must not be bound"

    def test_plumbing_error_is_memoised_so_a_retry_cannot_take_a_new_key(
        self, tmp_path, monkeypatch
    ):
        """D1a': the remembered error is what makes a retry safe.

        Without the memo the second resolution re-enters ``reserve_for_card``.
        With the first attempt's key already released as failed, that retry
        could hand the card a *different* account mid-run — precisely the
        silent hop the reservation exists to forbid. The memo turns every
        later call into the same error.
        """
        write_keys(tmp_path)
        as_card("t_a", 1)

        monkeypatch.setattr(
            kp, "_write_state",
            lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space")),
        )
        with pytest.raises(kp.KeyPoolError) as first:
            kp.apply_card_key(runtime())

        # Heal the plumbing: a retry would now succeed if nothing were memoised.
        # (Not calling reset_process_cache — that clears the memo, which is the
        # very thing under test.)
        monkeypatch.undo()

        with pytest.raises(kp.KeyPoolError) as second:
            kp.apply_card_key(runtime())

        assert str(second.value) == str(first.value), (
            "the retry must repeat the original failure, not attempt a new "
            "reservation that could land on a different account"
        )
        assert kp.current_binding() is None

    def test_background_still_degrades_gracefully(self, tmp_path, monkeypatch):
        """The forgiving path stays: background work must never be bricked."""
        write_keys(tmp_path)
        as_background()
        monkeypatch.setattr(
            kp, "background_key",
            lambda: (_ for _ in ()).throw(OSError(28, "No space")),
        )
        # A background caller is not the feature's subject; the resolver's own
        # `except Exception` covers it and the unpooled runtime is returned.
        from hermes_cli import runtime_provider as rp

        monkeypatch.setattr(
            rp, "_resolve_runtime_provider_impl",
            lambda **kw: {"provider": "custom", "base_url": BASE_URL,
                          "api_key": "SHARED_ENV_KEY", "api_mode": "chat_completions"},
        )
        out = rp.resolve_runtime_provider(requested="custom")
        assert out["api_key"] == "SHARED_ENV_KEY"


class TestSiblingCanMarkTheKey:
    """D3a: the process that holds an adopted key may report it dead."""

    def test_sibling_marks_the_card_key_bad(self, tmp_path, live_pid):
        write_keys(tmp_path, card_keys=2)
        owner, sibling = live_pid(), live_pid()
        res = kp.reserve_for_card("t_a", 1, pid=owner)
        kp.reserve_for_card("t_a", 1, pid=sibling)     # adopt, same key

        marked = kp.mark_key_failed(
            "t_a", 401, reason="auth_rejected", pid=sibling
        )
        assert marked is not None, "an adopted sibling could not quarantine its key"
        assert marked["failure"]["reported_by"] == "sibling"
        assert marked["failure"]["owner_pid"] == owner
        assert marked["failure"]["pid"] == sibling

        # The dead key is out of circulation for the next card.
        nxt = kp.reserve_for_card("t_b", 2, pid=live_pid())
        assert nxt.key_id != res.key_id

    def test_unrelated_process_cannot_mark_the_key(self, tmp_path, live_pid):
        """Safety half: a stranger's pid must not poison a card's key."""
        write_keys(tmp_path, card_keys=2)
        owner = live_pid()
        res = kp.reserve_for_card("t_a", 1, pid=owner)

        stranger = live_pid()
        assert kp.mark_key_failed("t_a", 401, reason="x", pid=stranger) is None
        # Right pid, wrong task: still refused.
        assert kp.mark_key_failed("t_b", 401, reason="x", pid=owner) is None
        held = kp.pool_status()["reservations"]
        assert [(r["task_id"], r["key_id"]) for r in held] == [("t_a", res.key_id)]
        assert kp.pool_status()["failed_keys"] == []

    def test_mark_current_key_failed_uses_this_process_pid(
        self, tmp_path, monkeypatch
    ):
        """A binding adopted from another pid still marks on its own behalf."""
        write_keys(tmp_path, card_keys=2)
        as_card("t_a", 1)
        kp.apply_card_key(runtime())

        real = os.getpid
        monkeypatch.setattr(os, "getpid", lambda: 999999)
        assert kp.mark_current_key_failed(401, reason="auth_rejected") is None
        monkeypatch.setattr(os, "getpid", real)
        assert kp.mark_current_key_failed(401, reason="auth_rejected") is not None


class TestNoCredentialPoolIsStillMarked:
    """D3b: production custom endpoints resolve with ``_credential_pool=None``.

    The guard used to sit *after* the ``pool is None`` early return, so on the
    only configuration that matters (letovo custom endpoint, no credential
    pool entries) a 401 was never attributed to the card's key: no quarantine,
    no audit record, and the same dead key was handed to the next card.
    """

    def test_401_with_no_credential_pool_marks_the_card_key(
        self, tmp_path, monkeypatch
    ):
        write_keys(tmp_path, card_keys=2)
        as_card("t_a", 1)
        kp.apply_card_key(runtime())

        from agent import agent_runtime_helpers as helpers

        class _Agent:
            provider = "custom"
            base_url = BASE_URL
            api_key = "KEY_CARD_2_222"
            _credential_pool = None          # letovo's real shape
            _credential_pool_entry_id = None

            def _swap_credential(self, entry):  # pragma: no cover - must not run
                raise AssertionError("a card must never swap credentials mid-run")

        recovered, retried = helpers.recover_with_credential_pool(
            _Agent(), status_code=401, has_retried_429=False,
        )
        assert recovered is False
        assert retried is False

        assert kp.current_binding().status == "bad"
        failures = kp.pool_status()["failed_keys"]
        assert [f["status_code"] for f in failures] == [401]
        assert failures[0]["reason"] == "credential_rotation_blocked"

        # And the dead key does not come back for the next card.
        nxt = kp.reserve_for_card("t_b", 2, pid=os.getpid())
        assert nxt.key_id != 2

    def test_non_card_still_returns_early_with_no_pool(self, tmp_path):
        """The guard must not change behaviour for unpooled/background callers."""
        write_keys(tmp_path)
        as_background()
        from agent import agent_runtime_helpers as helpers

        class _Agent:
            provider = "custom"
            base_url = BASE_URL
            api_key = "SHARED_ENV_KEY"
            _credential_pool = None
            _credential_pool_entry_id = None

        assert helpers.recover_with_credential_pool(
            _Agent(), status_code=401, has_retried_429=False,
        ) == (False, False)
        assert kp.pool_status()["failed_keys"] == []


# ── owner verification surface ─────────────────────────────────────────────


class TestAcceptanceSurface:
    def test_status_never_leaks_api_keys(self, tmp_path):
        write_keys(tmp_path)
        kp.reserve_for_card("t_a", 1, pid=os.getpid())
        kp.mark_key_failed("t_b_missing", 401, reason="x", pid=1)
        blob = json.dumps(kp.pool_status(), ensure_ascii=False)
        assert "KEY_CARD_" not in blob
        assert "KEY_BG_" not in blob

    def test_check_reports_ok_for_distinct_keys(self, tmp_path, capsys):
        write_keys(tmp_path)
        for i in range(3):
            kp.reserve_for_card(f"t_{i}", i, pid=os.getpid())
        from hermes_cli.subcommands.kanban_keypool import _cmd_check

        class _Args:
            json = True

        assert _cmd_check(kp, _Args(), True) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "ok"
        assert out["card_count"] == 3
        assert out["distinct_keys"] == 3
        assert out["background_leaked_to_cards"] == []

    def test_check_says_so_when_nothing_is_running(self, tmp_path, capsys):
        write_keys(tmp_path)
        from hermes_cli.subcommands.kanban_keypool import _cmd_check

        class _Args:
            json = True

        _cmd_check(kp, _Args(), True)
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "no_active_cards"


class TestStatusExplainsAnUnusableKeyFile:
    """Review N2: a broken key file must not print a bare ``DISABLED``.

    Nine malformed shapes (invalid JSON, duplicate id, empty ``api_key``, role
    outside ``{background,card}``, wrong ``background_key_id`` …) all collapse
    to ``inactive_reason == "unusable_key_file"``. ``no_card_keys`` /
    ``no_base_url`` / ``config_disabled`` each had their own ``elif``; this one
    fell through, so the operator's typo in a freshly issued 10-key file read
    exactly like a deliberate shutdown.
    """

    # Each entry is (label, file body) — one per shape the reviewer exercised.
    SHAPES = [
        ("broken json", "{ this is not json"),
        (
            "duplicate id",
            {
                "version": 1,
                "base_url": BASE_URL,
                "background_key_id": 1,
                "keys": [
                    {"id": 1, "role": "background", "api_key": "KEY_BG_111"},
                    {"id": 2, "role": "card", "api_key": "KEY_CARD_2"},
                    {"id": 2, "role": "card", "api_key": "KEY_CARD_2B"},
                ],
            },
        ),
        (
            "empty api_key",
            {
                "version": 1,
                "base_url": BASE_URL,
                "background_key_id": 1,
                "keys": [
                    {"id": 1, "role": "background", "api_key": "KEY_BG_111"},
                    {"id": 2, "role": "card", "api_key": "   "},
                ],
            },
        ),
        (
            "bad role",
            {
                "version": 1,
                "base_url": BASE_URL,
                "background_key_id": 1,
                "keys": [
                    {"id": 1, "role": "background", "api_key": "KEY_BG_111"},
                    {"id": 2, "role": "worker", "api_key": "KEY_CARD_2"},
                ],
            },
        ),
        (
            "background_key_id missing from keys",
            {
                "version": 1,
                "base_url": BASE_URL,
                "background_key_id": 7,
                "keys": [
                    {"id": 1, "role": "background", "api_key": "KEY_BG_111"},
                    {"id": 2, "role": "card", "api_key": "KEY_CARD_2"},
                ],
            },
        ),
    ]

    @pytest.mark.parametrize("label,body", SHAPES, ids=[s[0] for s in SHAPES])
    def test_status_names_the_reason_instead_of_a_bare_disabled(
        self, tmp_path, capsys, label, body
    ):
        path = tmp_path / "kanban-keypool.json"
        path.write_text(
            body if isinstance(body, str) else json.dumps(body), encoding="utf-8"
        )
        os.environ[kp.ENV_KEYPOOL] = str(path)
        os.environ[kp.ENV_KEYPOOL_STATE] = str(tmp_path / "s.json")
        kp.reset_process_cache()

        status = kp.pool_status()
        assert status["enabled"] is False, label
        assert status["inactive_reason"] == "unusable_key_file", label

        from hermes_cli.subcommands.kanban_keypool import _cmd_status

        assert _cmd_status(kp, False) == 0
        out = capsys.readouterr().out
        assert "unusable" in out.lower(), (label, out)
        # The reason must be actionable, not just a mood: it names what to fix.
        low = out.lower()
        assert any(word in low for word in ("schema", "role", "duplicate", "api_key")), (
            label,
            out,
        )
        # And no secret leaked through the new branch.
        assert "KEY_CARD_" not in out and "KEY_BG_" not in out

    def test_status_does_not_claim_unusable_when_the_reason_is_other(
        self, tmp_path, capsys
    ):
        """Guard against over-broad wording: distinct causes stay distinct."""
        write_keys(tmp_path, base_url="")
        from hermes_cli.subcommands.kanban_keypool import _cmd_status

        _cmd_status(kp, False)
        out = capsys.readouterr().out
        assert "base_url" in out
        assert "unusable" not in out.lower()


# ── small helpers ──────────────────────────────────────────────────────────


def _unused_pid() -> int:
    """Return a pid that is definitely not running.

    Spawn and immediately reap a child — its pid is free (on POSIX it can be
    recycled, but not within the microseconds this test runs, and on Windows
    psutil reports the reaped handle as gone).
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _allow_any_assignee(monkeypatch) -> None:
    """Skip the "profile must be installed" check on this host.

    ``create_task`` rejects a named assignee whose profile directory does not
    exist, and ``create_task(assignee=None)`` normalises to ``01-orchestrator``.
    Neither is installed in the hermetic test home, and the assignee identity is
    irrelevant to what these tests measure (key release on a lifecycle
    transition) — so neutralise just that validation.
    """
    from hermes_cli import kanban_db as kb

    if hasattr(kb, "_validate_assignee_profile"):
        monkeypatch.setattr(kb, "_validate_assignee_profile", lambda a: a or None)
    # This tree's ``create_task`` does not validate the assignee profile at all
    # (no ``_validate_assignee_profile`` hook) — nothing to neutralise.


def _connect(db_path):
    """``hermes_cli.kanban_db.connect`` in the fork; a sibling module here."""
    from hermes_cli import kanban_db as kb

    opener = getattr(kb, "connect", None)
    if opener is not None:
        return opener(db_path)
    from hermes_cli import kanban_db_connect as kbc

    return kbc.connect(db_path)


def _init_db_takes_conn(kb) -> bool:
    import inspect

    try:
        params = inspect.signature(kb.init_db).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False
    return "conn" in params


class TestExcludedFromCards:
    """The ``excluded_from_cards`` reservation must be enforced and explained.

    Context (card t_df206816, defect B): the flag was read by
    ``KeyPoolConfig.card_key_ids`` but shipped with **zero** tests — a grep for
    ``excluded_from_cards`` under ``tests/`` returned nothing. Nothing proved
    that a marked key is not handed to a card, that an already-holding card
    keeps it, or that the status view names the exclusion instead of silently
    dropping the key from ``card keys``.
    """

    def _write(self, tmp_path: Path, *, excluded: bool = True,
               max_cards_per_key: int = 1) -> Path:
        keys = [
            {"id": 1, "label": "background", "role": "background",
             "api_key": "KEY_BG_111"},
            {"id": 2, "label": "account-2", "role": "card",
             "api_key": "KEY_CARD_2_222"},
            {"id": 8, "label": "aux-compression", "role": "card",
             "api_key": "KEY_CARD_8_222",
             "excluded_from_cards": excluded},
        ]
        path = tmp_path / "kanban-keypool.json"
        path.write_text(json.dumps({
            "version": 1,
            "enabled": True,
            "base_url": BASE_URL,
            "provider": "letovo",
            "background_key_id": 1,
            "max_cards_per_key": max_cards_per_key,
            "keys": keys,
        }), encoding="utf-8")
        os.environ[kp.ENV_KEYPOOL] = str(path)
        os.environ[kp.ENV_KEYPOOL_STATE] = str(
            tmp_path / "kanban-keypool.state.json"
        )
        kp.reset_process_cache()
        return path

    def test_excluded_key_is_absent_from_card_key_ids(self, tmp_path):
        self._write(tmp_path)
        cfg = kp.load_config()
        assert cfg.card_key_ids == [2]
        assert cfg.excluded_card_key_ids == [8]

    def test_flag_absent_means_not_excluded(self, tmp_path):
        """Default is inclusive: a plain card key stays distributable."""
        self._write(tmp_path, excluded=False)
        cfg = kp.load_config()
        assert cfg.card_key_ids == [2, 8]
        assert cfg.excluded_card_key_ids == []

    def test_excluded_key_is_never_handed_to_a_card(self, tmp_path):
        """The decisive assertion: capacity pressure must not leak key #8."""
        self._write(tmp_path)
        # Give #2 a deadline-free occupant so #8 would be the ONLY free key if
        # the flag were ignored — the exact misrouting the flag exists to stop.
        held = os.getpid()
        kp.reserve_for_card("t_occupant", 1, pid=held)
        kp.reset_process_cache()
        as_card("t_second", 2)
        with pytest.raises(kp.KeyPoolError):
            kp.apply_card_key(runtime())

    def test_excluded_key_still_counts_in_key_count(self, tmp_path):
        """Exclusion is a distribution rule, not a deletion from the file."""
        self._write(tmp_path)
        status = kp.pool_status()
        assert status["key_count"] == 3
        assert 8 in status["excluded_card_key_ids"]
        assert 8 not in status["card_key_ids"]

    def test_status_names_the_exclusion_and_its_label(self, tmp_path):
        """Defect A: the reason must be visible, not left to a manual diff."""
        self._write(tmp_path)
        status = kp.pool_status()
        assert status["excluded_card_key_ids"] == [8]
        assert status["excluded_card_key_labels"] == {"8": "aux-compression"}

    def test_status_carries_no_api_key_for_excluded_keys(self, tmp_path):
        self._write(tmp_path)
        blob = json.dumps(kp.pool_status(), ensure_ascii=False)
        assert "KEY_CARD_8_222" not in blob
        assert "KEY_CARD_2_222" not in blob
        assert "KEY_BG_111" not in blob

    def test_cmd_status_prints_the_reason_line(self, tmp_path, capsys):
        self._write(tmp_path)
        from hermes_cli.subcommands.kanban_keypool import _cmd_status

        assert _cmd_status(kp, False) == 0
        out = capsys.readouterr().out
        assert "excluded from cards" in out
        assert "#8" in out
        assert "aux-compression" in out
