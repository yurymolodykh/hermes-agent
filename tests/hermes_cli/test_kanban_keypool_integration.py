"""Dispatcher + exit-contract integration for the per-card key pool.

``tests/agent/test_kanban_key_pool.py`` (ported with the mechanism) proves the
pool module itself and the two no-silent-hop guards. This file proves the two
things only THIS tree's shape can break:

* the dispatch lane defers a card when every card key is taken instead of
  spawning a worker that dies at key resolution — and does nothing at all when
  no pool is configured;
* a card worker whose key resolution raised ``KeyPoolError`` exits with the
  EXISTING transient-provider sentinel (``KANBAN_RATE_LIMIT_EXIT_CODE``, 75) so
  the dispatcher requeues the card without spending its retry budget — and a
  non-worker keeps the untouched exit-1 contract.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

# Collection-time import: ``cli`` pulls hermes_bootstrap, which the test home
# guard must not see from inside a test body.
import cli

BASE_URL = "https://llm.example.test/openai"


@pytest.fixture()
def kanban_home(monkeypatch):
    """Fresh HERMES_HOME + kanban modules rebound to it."""
    test_home = tempfile.mkdtemp(prefix="kanban_keypool_dispatch_test_")
    os.makedirs(os.path.join(test_home, "profiles", "alpha"), exist_ok=True)
    with open(os.path.join(test_home, "profiles", "alpha", "config.yaml"), "w") as fh:
        fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Only the board modules are rebound: purging every ``hermes_cli`` module
    # would hand other test modules a fresh ``runtime_provider`` that their
    # monkeypatches no longer reach.
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli.kanban_db") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    # The module purge discards the conftest's memory-sample neutralizer;
    # re-apply it so host memory pressure cannot cap this tick.
    monkeypatch.setattr(kanban_db_dispatch, "_system_memory_sample", dict)
    monkeypatch.delenv("HERMES_KANBAN_KEYPOOL", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_KEYPOOL_STATE", raising=False)
    return kanban_db, kanban_db_connect, kanban_db_dispatch


@pytest.fixture()
def live_pid():
    """Factory for genuinely live child pids (a reservation needs one)."""
    children: list[subprocess.Popen] = []

    def _make() -> int:
        from agent import kanban_key_pool as kp

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        children.append(proc)
        for _ in range(200):
            if kp._pid_alive(proc.pid):
                return proc.pid
            time.sleep(0.01)
        raise AssertionError("child never became alive")

    yield _make
    for proc in children:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # pragma: no cover
            pass


def _pool(monkeypatch, tmp_path, card_keys=1, max_cards_per_key=1):
    """Write a pool with *card_keys* card keys at *tmp_path* and enable it."""
    keys = [{"id": 1, "label": "background", "role": "background", "api_key": "KEY_BG"}]
    for _id in range(2, 2 + card_keys):
        keys.append({"id": _id, "label": f"account-{_id}", "role": "card",
                     "api_key": f"KEY_CARD_{_id}"})
    path = tmp_path / "kanban-keypool.json"
    path.write_text(json.dumps({
        "version": 1, "enabled": True, "base_url": BASE_URL, "provider": "letovo",
        "background_key_id": 1, "max_cards_per_key": max_cards_per_key, "keys": keys,
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_KEYPOOL", str(path))
    monkeypatch.setenv("HERMES_KANBAN_KEYPOOL_STATE", str(tmp_path / "kanban-keypool.state.json"))
    from agent import kanban_key_pool as kp

    kp.reset_process_cache()
    return kp


def _ready_card(kb, kbc, title="probe"):
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        return kb.create_task(conn, title=title, assignee="alpha")


def _status(kb, kbc, task_id):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, task_id).status


def _run_tick(kbd, kbc, spawned: list):
    """One real tick whose spawner is faked at module level.

    ``spawn_fn`` stays ``None`` on purpose: this is the production spawn path
    (the embedded dispatcher never passes a ``spawn_fn``). The custom-spawner
    path is covered separately below — it must obey the SAME key gate.
    """
    pids = itertools.count(4242)

    def _fake_default_spawn(*args, **kwargs):
        spawned.append(args[0].id if args else None)
        return next(pids)

    original = kbd._default_spawn
    kbd._default_spawn = _fake_default_spawn
    try:
        with kbc.connect_closing() as conn:
            return kbd.dispatch_once(conn)
    finally:
        kbd._default_spawn = original


def test_dispatch_defers_a_card_when_every_card_key_is_taken(
    kanban_home, monkeypatch, tmp_path, live_pid
):
    kb, kbc, kbd = kanban_home
    kp = _pool(monkeypatch, tmp_path, card_keys=1)
    task_id = _ready_card(kb, kbc)
    # The only card key is held by a live worker of another card.
    kp.reserve_for_card("t_occupant", 1, pid=live_pid())
    kp.reset_process_cache()

    spawned: list = []
    res = _run_tick(kbd, kbc, spawned)

    assert res.skipped_keypool_capped == [task_id]
    assert spawned == [], "no worker may be spawned without a key"
    assert _status(kb, kbc, task_id) == "ready", "the card waits, it does not fail"


def test_dispatch_spawns_when_a_card_key_is_free(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    _pool(monkeypatch, tmp_path, card_keys=1)
    task_id = _ready_card(kb, kbc)

    spawned: list = []
    res = _run_tick(kbd, kbc, spawned)

    assert res.skipped_keypool_capped == []
    assert [s[0] for s in res.spawned] == [task_id]
    assert spawned == [task_id]


def test_dispatch_is_unchanged_without_a_pool(kanban_home):
    """No pool config: the gate must not exist as far as dispatch can tell."""
    kb, kbc, kbd = kanban_home
    task_id = _ready_card(kb, kbc)

    assert kbd._keypool_has_free_slot() is True
    spawned: list = []
    res = _run_tick(kbd, kbc, spawned)
    assert res.skipped_keypool_capped == []
    assert [s[0] for s in res.spawned] == [task_id]


def test_slot_probe_reserves_headroom_for_workers_spawned_this_tick(
    kanban_home, monkeypatch, tmp_path
):
    """Two keys: the second spawn of the tick must see the reduced headroom.

    The child has not reserved anything yet when the dispatch loop asks, so the
    in-tick tally is the only thing that stops two cards taking the same slot.
    """
    _pool(monkeypatch, tmp_path, card_keys=2)
    _, _, kbd = kanban_home

    assert kbd._keypool_has_free_slot() is True
    assert kbd._keypool_has_free_slot(reserve=1) is True
    assert kbd._keypool_has_free_slot(reserve=2) is False


# ── custom spawner ─────────────────────────────────────────────────────────


def _run_tick_with_spawn_fn(kbd, kbc, spawned: list, spawn_fn, **kwargs):
    with kbc.connect_closing() as conn:
        return kbd.dispatch_once(conn, spawn_fn=spawn_fn, **kwargs)


def test_custom_spawner_obeys_the_same_key_gate(
    kanban_home, monkeypatch, tmp_path, live_pid
):
    """An explicit ``spawn_fn`` must NOT bypass the key-pool gate.

    A custom spawner still starts a real worker that resolves its key through
    ``resolve_runtime_provider`` — the same chokepoint the gate protects — so
    spawning with a full pool produces exactly the worker the gate exists to
    keep off the board (KeyPoolError at startup, burned retry budget, card
    auto-blocked on a transient condition).
    """
    kb, kbc, kbd = kanban_home
    kp = _pool(monkeypatch, tmp_path, card_keys=1)
    task_id = _ready_card(kb, kbc)
    kp.reserve_for_card("t_occupant", 1, pid=live_pid())
    kp.reset_process_cache()

    spawned: list = []

    def spawn_fn(task, workspace, board=None):  # pragma: no cover - must not run
        spawned.append(task.id)
        return 4242

    res = _run_tick_with_spawn_fn(kbd, kbc, spawned, spawn_fn)

    assert res.skipped_keypool_capped == [task_id]
    assert spawned == [], "a custom spawner may not start a worker without a key"
    assert _status(kb, kbc, task_id) == "ready"


def test_custom_spawner_still_spawns_when_a_key_is_free(
    kanban_home, monkeypatch, tmp_path
):
    kb, kbc, kbd = kanban_home
    _pool(monkeypatch, tmp_path, card_keys=1)
    task_id = _ready_card(kb, kbc)

    spawned: list = []

    def spawn_fn(task, workspace, board=None):
        spawned.append(task.id)
        return 4242

    res = _run_tick_with_spawn_fn(kbd, kbc, spawned, spawn_fn)

    assert res.skipped_keypool_capped == []
    assert [s[0] for s in res.spawned] == [task_id]
    assert spawned == [task_id]


def test_keypool_exempt_is_the_named_opt_out(
    kanban_home, monkeypatch, tmp_path, live_pid
):
    """A spawner that truly keys its workers another way opts out by name."""
    kb, kbc, kbd = kanban_home
    kp = _pool(monkeypatch, tmp_path, card_keys=1)
    task_id = _ready_card(kb, kbc)
    kp.reserve_for_card("t_occupant", 1, pid=live_pid())
    kp.reset_process_cache()

    spawned: list = []

    def spawn_fn(task, workspace, board=None):
        spawned.append(task.id)
        return 4242

    res = _run_tick_with_spawn_fn(kbd, kbc, spawned, spawn_fn, keypool_exempt=True)

    assert res.skipped_keypool_capped == []
    assert spawned == [task_id], "the named opt-out must actually spawn"


def test_reserve_raises_the_slot_busy_subclass_when_every_key_is_taken(
    kanban_home, monkeypatch, tmp_path, live_pid
):
    """The exhaustion branch must raise ``KeyPoolSlotBusy``, not a bare ``KeyPoolError``.

    That subclass is the whole contract: ``gateway.session_kanban`` maps it to the transient
    exit (75) so the dispatcher requeues the card without charging ``consecutive_failures``
    (22.11.2026: a worker that never started still blocked the card). A plain
    ``KeyPoolError`` — a pool that is not in force, a key rejected mid-run — deliberately
    stays a crash, so the two must not collapse into one class.
    """
    kp = _pool(monkeypatch, tmp_path, card_keys=1)
    kp.reserve_for_card("t_occupant", 1, pid=live_pid())
    from agent.kanban_key_pool import KeyPoolSlotBusy

    with pytest.raises(KeyPoolSlotBusy) as exc:
        kp.reserve_for_card("t_contender", 2, pid=live_pid())
    assert "no free card key slot" in str(exc.value)


# ── startup exit contract ──────────────────────────────────────────────────


def _shell():
    from types import SimpleNamespace

    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    shell = CLIAgentSetupMixin.__new__(CLIAgentSetupMixin)
    shell.model = "fixture"
    shell.requested_provider = "letovo"
    shell._explicit_api_key = shell._explicit_base_url = None
    shell._fallback_model = []
    shell.tool_progress_mode = "off"
    shell.session_id = "fixture-session"
    shell.console = SimpleNamespace(print=lambda *a, **k: None)
    return shell


@pytest.mark.parametrize("worker", [True, False])
def test_keypool_exhaustion_exits_tempfail_for_a_worker_only(monkeypatch, worker):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")
    from agent.kanban_key_pool import KeyPoolError

    def resolve(**kwargs):
        raise KeyPoolError("no free card key")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)

    shell = _shell()
    assert shell._ensure_runtime_credentials() is False
    assert shell._credentials_rate_limited is worker
    code = cli._single_query_exit_code(
        None, credentials_rate_limited=shell._credentials_rate_limited,
        credentials_terminal=shell._credentials_terminal)
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

    assert code == (KANBAN_RATE_LIMIT_EXIT_CODE if worker else 1)


def test_a_plain_resolution_failure_keeps_exit_1_for_a_worker(monkeypatch):
    """No second contract: only KeyPoolError is transient."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")

    def resolve(**kwargs):
        raise RuntimeError("fixture transport")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)

    shell = _shell()
    assert shell._ensure_runtime_credentials() is False
    assert shell._credentials_rate_limited is False
    assert cli._single_query_exit_code(
        None, credentials_rate_limited=False, credentials_terminal=False) == 1
