"""Scratch reproduction of the keypool tick regression (not part of the suite)."""
from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile

import pytest

import cli  # noqa: F401  (collection-time import, as the real test does)

BASE_URL = "https://llm.example.test/openai"


@pytest.fixture()
def kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kp_dbg_")
    os.makedirs(os.path.join(test_home, "profiles", "alpha"), exist_ok=True)
    with open(os.path.join(test_home, "profiles", "alpha", "config.yaml"), "w") as fh:
        fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli.kanban_db") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    monkeypatch.setattr(kanban_db_dispatch, "_system_memory_sample", dict)
    monkeypatch.delenv("HERMES_KANBAN_KEYPOOL", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_KEYPOOL_STATE", raising=False)
    return kanban_db, kanban_db_connect, kanban_db_dispatch


def test_debug_tick(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    tmp = tempfile.mkdtemp()
    keys = [{"id": 1, "label": "background", "role": "background", "api_key": "KEY_BG"},
            {"id": 2, "label": "account-2", "role": "card", "api_key": "KEY_CARD_2"}]
    path = os.path.join(tmp, "kanban-keypool.json")
    with open(path, "w") as fh:
        json.dump({"version": 1, "enabled": True, "base_url": BASE_URL, "provider": "letovo",
                   "background_key_id": 1, "max_cards_per_key": 1, "keys": keys}, fh)
    monkeypatch.setenv("HERMES_KANBAN_KEYPOOL", path)
    monkeypatch.setenv("HERMES_KANBAN_KEYPOOL_STATE", os.path.join(tmp, "state.json"))
    from agent import kanban_key_pool as kp
    kp.reset_process_cache()

    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")

    spawned: list = []
    pids = itertools.count(4242)

    def _fake_default_spawn(*args, **kwargs):
        spawned.append(args[0].id if args else None)
        return next(pids)

    original = kbd._default_spawn
    kbd._default_spawn = _fake_default_spawn
    try:
        with kbc.connect_closing() as conn:
            res = kbd.dispatch_once(conn)
    finally:
        kbd._default_spawn = original

    print("\n=== RESULT ===")
    print("task", tid, "spawned_attr", res.spawned, "fake_spawned", spawned)
    for name in ("skipped_locked", "skipped_keypool_capped", "skipped_nonspawnable",
                 "skipped_unassigned", "respawn_guarded", "budget_hold", "memory_pressure",
                 "promoted", "reclaimed", "auto_split", "auto_split_failed"):
        print(" ", name, "=", getattr(res, name, "<missing>"))
    print("promote candidates:", [r for r in res.promoted] if res.promoted else [])
    with kbc.connect_closing() as conn:
        row = kb.get_task(conn, tid)
        print("status", row.status, "workspace", row.workspace_path)
    assert spawned == [tid], f"fake spawner not called: {spawned}"


def test_trace(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    with kbc.connect_closing() as conn:
        print("\nROWS", [dict(r) for r in kbd._lane_rows(conn, "ready")])
        print("profile_exists", kbd._profile_exists_fn())
        print("may_spawn", kbd._tick_spawn_budget(conn, kbd.DispatchResult(), max_spawn=None, max_in_progress=None, board=None))
        orig = kbd._dispatch_lane_task
        def traced(conn_, row, assignee, result, **kw):
            out = orig(conn_, row, assignee, result, **kw)
            print("LANE", row["id"], assignee, "->", out, "result:", result.__dict__)
            return out
        kbd._dispatch_lane_task = traced
        try:
            kbd.dispatch_once(conn)
        finally:
            kbd._dispatch_lane_task = orig


def test_trace_locked(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    import hermes_cli.kanban_db_dispatch_split as sp
    print("\nsplit module:", sp.__file__)
    print("same dispatch module:", sp.__dict__.get("_kb"), kbd)
    orig = kbd._dispatch_lane_task
    def traced(conn_, row, assignee, result, **kw):
        out = orig(conn_, row, assignee, result, **kw)
        print("LANE", row["id"], assignee, "->", out)
        return out
    kbd._dispatch_lane_task = traced
    try:
        with kbc.connect_closing() as conn:
            r = kbd._dispatch_once_locked(conn)
        print("locked result:", {k: v for k, v in r.__dict__.items() if v})
    finally:
        kbd._dispatch_lane_task = orig


def test_trace_claims(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    with kbc.connect_closing() as conn:
        row = kbd._lane_rows(conn, "ready")[0]
        res = kbd.DispatchResult()
        print("\nrefuse_anon:", kbd._refuse_anonymous_card(conn, row, res, dry_run=False), res.__dict__)
        print("is_anon:", kb.is_anonymous_placeholder_card(row["assignee"], row["created_by"]))
        print("live_board:", kb.path_is_live_board(kb._conn_db_path(conn)), kb._conn_db_path(conn))
        print("respawn_guard:", kbd.check_respawn_guard(conn, row["id"], lane="ready"))
        print("keypool_free:", kbd._keypool_has_free_slot(reserve=0))
        claimed = kb.claim_task(conn, row["id"], ttl_seconds=None)
        print("claimed:", claimed.id if claimed else None)
