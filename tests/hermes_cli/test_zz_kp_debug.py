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


def test_trace_claim_none(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    real_claim = kb.claim_task
    def traced_claim(conn_, task_id, **kw):
        out = real_claim(conn_, task_id, **kw)
        print("\nCLAIM_CALL", task_id, kw, "->", getattr(out, "id", None))
        if out is None:
            row = conn_.execute("select id,status,claim_lock,claim_expires,worker_pid from tasks where id=?", (task_id,)).fetchone()
            print("  row now:", dict(row))
        return out
    kb.claim_task = traced_claim
    kbd.claim_task = traced_claim if hasattr(kbd, "claim_task") else None
    monkeypatch.setattr(kb, "claim_task", traced_claim)
    try:
        with kbc.connect_closing() as conn:
            r = kbd._dispatch_once_locked(conn)
        print("result:", {k: v for k, v in r.__dict__.items() if v})
    finally:
        kb.claim_task = real_claim


def test_trace_workspace(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    real = kbd._kbw.resolve_workspace
    def traced(task, **kw):
        try:
            out = real(task, **kw)
            print("\nWS OK", out)
            return out
        except Exception as exc:
            print("\nWS RAISED", type(exc).__name__, exc)
            raise
    monkeypatch.setattr(kbd._kbw, "resolve_workspace", traced)
    env = kbd._kbenv.ensure_tree_install_state
    def traced_env(p):
        out = env(p)
        print("ENV", p, "->", out)
        return out
    monkeypatch.setattr(kbd._kbenv, "ensure_tree_install_state", traced_env)
    with kbc.connect_closing() as conn:
        r = kbd._dispatch_once_locked(conn)
    print("result:", {k: v for k, v in r.__dict__.items() if v})


def test_trace_spawn(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    real = kbd._call_spawn_fn
    def traced(fn, task, ws, board):
        print("\nSPAWN_FN", fn)
        try:
            out = real(fn, task, ws, board)
            print("SPAWN ->", out)
            return out
        except Exception as exc:
            print("SPAWN RAISED", type(exc).__name__, exc)
            raise
    monkeypatch.setattr(kbd, "_call_spawn_fn", traced)
    with kbc.connect_closing() as conn:
        r = kbd._dispatch_once_locked(conn)
    print("result:", {k: v for k, v in r.__dict__.items() if v})


def test_trace_failure(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    real = kbd._record_task_failure
    def traced(conn_, task_id, reason, **kw):
        print("\nFAILURE_RECORDED", task_id, repr(reason)[:200], kw.get("outcome"))
        return real(conn_, task_id, reason, **kw)
    monkeypatch.setattr(kbd, "_record_task_failure", traced)
    orig_spawn = kbd._default_spawn
    calls = []
    def fake(*a, **k):
        calls.append(a[0].id if a else None)
        print("FAKE_SPAWN_CALLED", calls)
        return 4242
    monkeypatch.setattr(kbd, "_default_spawn", fake)
    with kbc.connect_closing() as conn:
        r = kbd.dispatch_once(conn)
    print("spawned", r.spawned, "calls", calls, "res", {k: v for k, v in r.__dict__.items() if v})


def test_trace_exc(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    real_spawn = kbd._call_spawn_fn
    def boom(*a, **k):
        print("\nCALL_SPAWN_FN_REACHED")
        raise RuntimeError("simulated")
    monkeypatch.setattr(kbd, "_call_spawn_fn", boom)
    real_rec = kbd._record_task_failure
    def rec(conn_, task_id, reason, **kw):
        print("RECORD", repr(reason)[:120], kw.get("outcome"))
        return real_rec(conn_, task_id, reason, **kw)
    monkeypatch.setattr(kbd, "_record_task_failure", rec)
    with kbc.connect_closing() as conn:
        r = kbd._dispatch_once_locked(conn)
    print("after:", {k: v for k, v in r.__dict__.items() if v})
    print("CALL_SPAWN_FN_REACHED?", "yes" )


def test_trace_split(kanban_home, monkeypatch, tmp_path):
    kb, kbc, kbd = kanban_home
    import hermes_cli.kanban_db_dispatch_split as sp
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="probe", assignee="alpha")
    real = sp._kac.run_split_phase
    def traced(conn_):
        out = real(conn_)
        print("\nSPLIT ->", out)
        rows = [dict(r) for r in conn_.execute("select id,status,claim_lock,assignee from tasks")]
        print("ROWS AFTER SPLIT", rows)
        return out
    monkeypatch.setattr(sp._kac, "run_split_phase", traced)
    with kbc.connect_closing() as conn:
        r = kbd.dispatch_once(conn)
    print("result", {k: v for k, v in r.__dict__.items() if v})


def test_trace_real(kanban_home, monkeypatch, tmp_path):
    import tests.hermes_cli.test_kanban_keypool_integration as T
    kb, kbc, kbd = kanban_home
    T._pool(monkeypatch, tmp_path, card_keys=1)
    task_id = T._ready_card(kb, kbc)
    spawned: list = []
    orig = kbd._dispatch_lane_task
    def traced(conn_, row, assignee, result, **kw):
        out = orig(conn_, row, assignee, result, **kw)
        print("\nLANE", row["id"], "->", out, "res:", {k: v for k, v in result.__dict__.items() if v})
        return out
    monkeypatch.setattr(kbd, "_dispatch_lane_task", traced)
    res = T._run_tick(kbd, kbc, spawned)
    print("FINAL", res.spawned, spawned, {k: v for k, v in res.__dict__.items() if v})


def test_trace_real2(kanban_home, monkeypatch, tmp_path):
    import tests.hermes_cli.test_kanban_keypool_integration as T
    kb, kbc, kbd = kanban_home
    T._pool(monkeypatch, tmp_path, card_keys=1)
    task_id = T._ready_card(kb, kbc)
    print("\ncreated", task_id, "db", kb.kanban_db_path(board=None))
    with kbc.connect_closing() as conn:
        print("rows in conn:", [dict(r) for r in conn.execute("select id,status,assignee,claim_lock from tasks")])
        print("lane:", [dict(r) for r in kbd._lane_rows(conn, "ready")])
    import hermes_cli.kanban_db_dispatch_split as sp
    real_locked = kbd._dispatch_once_locked
    def traced(conn_, **kw):
        print("LOCKED sees:", [dict(r) for r in conn_.execute("select id,status,assignee from tasks")])
        return real_locked(conn_, **kw)
    monkeypatch.setattr(sp, "_dispatch_once_locked", traced, raising=False)
    import hermes_cli.kanban_db_dispatch as d
    monkeypatch.setitem(sys.modules["hermes_cli.kanban_db_dispatch"].__dict__, "_dispatch_once_locked", traced)
    print("tick:", kbd.dispatch_once(kbc.connect()))


def test_trace_real3(kanban_home, monkeypatch, tmp_path):
    import tests.hermes_cli.test_kanban_keypool_integration as T
    kb, kbc, kbd = kanban_home
    T._pool(monkeypatch, tmp_path, card_keys=1)
    task_id = T._ready_card(kb, kbc)
    real_rows = kbd._lane_rows
    def rows(conn_, status):
        out = real_rows(conn_, status)
        print("\nLANE_ROWS", status, [r["id"] for r in out])
        return out
    monkeypatch.setattr(kbd, "_lane_rows", rows)
    real_budget = kbd._tick_spawn_budget
    def budget(*a, **k):
        out = real_budget(*a, **k)
        print("BUDGET", out)
        return out
    monkeypatch.setattr(kbd, "_tick_spawn_budget", budget)
    with kbc.connect_closing() as conn:
        r = kbd._dispatch_once_locked(conn)
    print("RES", {k: v for k, v in r.__dict__.items() if v})


def test_trace_inner(kanban_home, monkeypatch, tmp_path):
    import tests.hermes_cli.test_kanban_keypool_integration as T
    kb, kbc, kbd = kanban_home
    T._pool(monkeypatch, tmp_path, card_keys=1)
    task_id = T._ready_card(kb, kbc)
    pe = kbd._profile_exists_fn()
    print("\nalpha exists?", pe("alpha"))
    print("respawn guard:", kbd.check_respawn_guard(kbc.connect(), task_id, lane="ready"))
    print("keypool free:", kbd._keypool_has_free_slot(reserve=0))
    real_guard = kbd.check_respawn_guard
    def guard(conn_, tid, **kw):
        out = real_guard(conn_, tid, **kw)
        print("GUARD", tid, kw, "->", out)
        return out
    monkeypatch.setattr(kbd, "check_respawn_guard", guard)
    real_kp = kbd._keypool_has_free_slot
    def kpf(reserve=0):
        out = real_kp(reserve)
        print("KEYPOOL_SLOT", reserve, "->", out)
        return out
    monkeypatch.setattr(kbd, "_keypool_has_free_slot", kpf)
    real_anon = kbd._refuse_anonymous_card
    def anon(conn_, row, result, *, dry_run):
        out = real_anon(conn_, row, result, dry_run=dry_run)
        print("ANON ->", out)
        return out
    monkeypatch.setattr(kbd, "_refuse_anonymous_card", anon)
    with kbc.connect_closing() as conn:
        r = kbd._dispatch_once_locked(conn)
    print("RES", {k: v for k, v in r.__dict__.items() if v})


def test_trace_seq(kanban_home, monkeypatch, tmp_path):
    import tests.hermes_cli.test_kanban_keypool_integration as T
    kb, kbc, kbd = kanban_home
    T._pool(monkeypatch, tmp_path, card_keys=1)
    task_id = T._ready_card(kb, kbc)
    real_claim = kb.claim_task
    def claim(conn_, tid, **kw):
        out = real_claim(conn_, tid, **kw)
        print("\nSTEP claim", tid, "->", getattr(out, "id", None))
        return out
    monkeypatch.setattr(kbd, "claim_task", claim, raising=False)
    monkeypatch.setitem(kb.__dict__, "claim_task", claim)
    real_fn = kbd._call_spawn_fn
    def csf(*a, **k):
        print("STEP call_spawn_fn", a[0])
        try:
            out = real_fn(*a, **k)
        except Exception as exc:
            print("STEP spawn raised:", type(exc).__name__, str(exc)[:120])
            raise
        print("STEP spawn ->", out)
        return out
    monkeypatch.setattr(kbd, "_call_spawn_fn", csf)
    with kbc.connect_closing() as conn:
        r = kbd._dispatch_once_locked(conn)
    print("RES", {k: v for k, v in r.__dict__.items() if v})
