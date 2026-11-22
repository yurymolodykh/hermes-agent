"""Probe Д-1: does ``kanban.failure_limit`` reach the TIMEOUT and CRASH sweeps?

Reproduces the reviewer's ``probe_fl_exact.py`` from t_72acd744 against a throwaway
board and an isolated ``HERMES_HOME`` whose ``config.yaml`` says ``failure_limit: 3``.
Three variants, exactly as the verdict lists them:

    A: config only                -> must report effective_limit=3, source=dispatcher
    B: per-task max_retries=3     -> must report effective_limit=3, source=task
    C: dispatch_once(failure_limit=3) -> must report effective_limit=3, source=dispatcher

Variant C is the one that failed acceptance: the value reached ``_dispatch_once_locked``
but not the timeout/crash sweeps, so the event still said 2.

Run:
    .venv/bin/python evidence-t63278be8/probe_fl_exact.py [--json]

The board, home and all files are created under a temp dir; nothing touches ``~/.hermes``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _bootstrap(configured_limit: int | None = 3) -> Path:
    """Point HERMES_HOME/HERMES_KANBAN_HOME at a throwaway root BEFORE importing kanban."""
    root = Path(tempfile.mkdtemp(prefix="probe-fl-"))
    (root / ".hermes").mkdir()
    if configured_limit is None:
        # No ``kanban.failure_limit`` at all: behaviour must stay DEFAULT_FAILURE_LIMIT.
        (root / ".hermes" / "config.yaml").write_text("kanban:\n  max_spawn: 4\n", encoding="utf-8")
    else:
        (root / ".hermes" / "config.yaml").write_text(
            f"kanban:\n  failure_limit: {configured_limit}\n", encoding="utf-8"
        )
    os.environ["HERMES_HOME"] = str(root / ".hermes")
    os.environ["HERMES_KANBAN_HOME"] = str(root / ".hermes")
    os.environ.pop("HERMES_KANBAN_DB", None)
    os.environ.pop("HERMES_KANBAN_BOARD", None)
    # A Kanban worker inherits a board write fence (HERMES_DELEGATED_CHILD_CONTEXT)
    # that makes every path under ~/.hermes read-only — including a temp board
    # created there. This probe owns a throwaway board, so the fence is dropped.
    os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    os.environ["HERMES_KANBAN_CRASH_GRACE_SECONDS"] = "0"
    # Tests/probes must never write to the real home.
    Path.home = lambda: root  # type: ignore[assignment]
    return root


def _load():
    from hermes_cli import kanban_auto_continue as kac
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    return kac, kb, kbc, kbd


def _stub_signals(kbd) -> None:
    """Nothing here may probe or signal a real process."""
    kbd._worker_alive = lambda pid, started_at=None: True
    kbd._poll_worker_exit = lambda pid, started_at=None: True
    kbd._kill_fn = lambda signal_fn: (lambda pid, sig: None)


def _backdate(conn, kb, tid: str) -> None:
    old = int(time.time()) - 30
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )


def _continued(payloads) -> list[dict]:
    return [e.payload or {} for e in payloads if e.kind == "auto_continued"]


def _timeout_path(conn, kb, kac, kbd, *, variant: str, per_task_retries=None,
                  dispatch_limit=None) -> dict:
    """Three backdated timeouts; the third must trip at 3, not at 2."""
    kac.resolve_settings = lambda: kac.AutoContinueSettings(enabled=True, max_splits=2)
    _stub_signals(kbd)
    tid = kb.create_task(conn, title=f"overrun {variant}", assignee="worker",
                         max_runtime_seconds=1)
    conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (f"/tmp/ws/{tid}", tid))
    if per_task_retries is not None:
        conn.execute("UPDATE tasks SET max_retries = ? WHERE id = ?", (per_task_retries, tid))
    conn.commit()

    for attempt in (1, 2, 3):
        if attempt > 1:
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            conn.commit()
        claimed = kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':')[0]}:w{attempt}")
        if claimed is None:
            break
        kbd._set_worker_pid(conn, tid, 700_000 + attempt, run_id=claimed.current_run_id)
        _backdate(conn, kb, tid)
        if dispatch_limit is None:
            # A: the config path — _run_reclaim_phase receives what the CLI resolved.
            from hermes_cli.kanban_ops import _kanban_failure_limit_setting, _resolve_failure_limit
            limit = _resolve_failure_limit(None, _kanban_failure_limit_setting())
            result = kb.DispatchResult()
            kbd._run_reclaim_phase(conn, result, stale_timeout_seconds=0,
                                   failure_limit=limit, reconcile_orphans=False, board=None)
        else:
            # C: an explicit dispatch_once(failure_limit=...) override.
            kbd.dispatch_once(conn, failure_limit=dispatch_limit,
                              spawn_fn=lambda *a, **k: None)
        events = _continued(kb.list_events(conn, tid))
        if events:
            return {"variant": variant, "path": "timeout",
                    "effective_limit": events[-1].get("effective_limit"),
                    "limit_source": events[-1].get("limit_source"),
                    "failures": events[-1].get("failures")}
    return {"variant": variant, "path": "timeout", "effective_limit": None,
            "limit_source": None, "failures": None,
            "note": "never tripped in 3 attempts"}


def _crash_path(conn, kb, kac, kbd, *, variant: str, dispatch_limit=None) -> dict:
    """Three dead workers; the third must trip at 3."""
    kac.resolve_settings = lambda: kac.AutoContinueSettings(enabled=True, max_splits=2)
    kbd._worker_alive = lambda pid, started_at=None: False
    kbd._poll_worker_exit = lambda pid, started_at=None: True
    tid = kb.create_task(conn, title=f"crash {variant}", assignee="worker")
    conn.execute("UPDATE tasks SET max_runtime_seconds = NULL WHERE id = ?", (tid,))
    conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (f"/tmp/ws/{tid}", tid))
    conn.commit()

    for attempt in (1, 2, 3):
        if attempt > 1:
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            conn.commit()
        if kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':')[0]}:c{attempt}") is None:
            break
        kbd._set_worker_pid(conn, tid, 800_000 + attempt)
        if dispatch_limit is None:
            from hermes_cli.kanban_ops import _kanban_failure_limit_setting, _resolve_failure_limit
            limit = _resolve_failure_limit(None, _kanban_failure_limit_setting())
            result = kb.DispatchResult()
            kbd._run_reclaim_phase(conn, result, stale_timeout_seconds=0,
                                   failure_limit=limit, reconcile_orphans=False, board=None)
        else:
            kbd.dispatch_once(conn, failure_limit=dispatch_limit,
                              spawn_fn=lambda *a, **k: None)
        events = _continued(kb.list_events(conn, tid))
        if events:
            return {"variant": variant, "path": "crash",
                    "effective_limit": events[-1].get("effective_limit"),
                    "limit_source": events[-1].get("limit_source"),
                    "failures": events[-1].get("failures")}
    return {"variant": variant, "path": "crash", "effective_limit": None,
            "limit_source": None, "failures": None, "note": "never tripped in 3 attempts"}


def _configured_limit() -> int | None:
    from hermes_cli.kanban_ops import _kanban_failure_limit_setting
    return _kanban_failure_limit_setting()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-config", action="store_true",
                        help="omit kanban.failure_limit entirely: expect DEFAULT_FAILURE_LIMIT")
    args = parser.parse_args()

    root = _bootstrap(None if args.no_config else 3)
    kac, kb, kbc, kbd = _load()
    expected = kbd.DEFAULT_FAILURE_LIMIT if args.no_config else 3
    kb.init_db()
    conn = kbc.connect()
    out: dict = {"temp_root": str(root), "expected_effective_limit": expected,
                 "config_failure_limit": _configured_limit(),
                 "default_failure_limit": kbd.DEFAULT_FAILURE_LIMIT, "results": []}
    try:
        out["results"].append(_timeout_path(conn, kb, kac, kbd, variant="A: config only"))
        out["results"].append(_timeout_path(conn, kb, kac, kbd,
                                            variant="B: per-task max_retries=3",
                                            per_task_retries=3))
        # With no config the per-task variant still pins 3, so it is skipped there.
        if not args.no_config:
            out["results"].append(_timeout_path(conn, kb, kac, kbd,
                                                variant="C: dispatch_once(failure_limit=3)",
                                                dispatch_limit=3))
        out["results"].append(_crash_path(conn, kb, kac, kbd, variant="A: config only"))
        if not args.no_config:
            out["results"].append(_crash_path(conn, kb, kac, kbd,
                                              variant="C: dispatch_once(failure_limit=3)",
                                              dispatch_limit=3))
    finally:
        conn.close()

    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    else:
        print(f"config kanban.failure_limit = {out['config_failure_limit']}")
        print(f"DEFAULT_FAILURE_LIMIT      = {out['default_failure_limit']}")
        print(f"expected effective_limit   = {expected}")
        for r in out["results"]:
            print(f"  {r['path']:8s} {r['variant']:40s} -> "
                  f"effective_limit={r['effective_limit']} source={r['limit_source']} "
                  f"failures={r['failures']}")
    if args.no_config:
        # No config must book the module default on the dispatcher paths.
        checked = [r for r in out["results"] if r["limit_source"] == "dispatcher"]
        ok = bool(checked) and all(r["effective_limit"] == expected for r in checked)
    else:
        ok = all(r["effective_limit"] == expected for r in out["results"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
