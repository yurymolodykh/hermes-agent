"""Probe 5: the ceiling BELOW the HTTP layer, and what several processes do to
one state.db (the exact question variants B and C disagree on).

Three independent measurements on a temp HERMES_HOME:

1. READ scaling in threads: list_sessions_rich / messages / search at 1..16
   concurrent callers. This is the store+CPU ceiling that no amount of HTTP
   parallelisation can exceed.
2. WRITE scaling in threads through the real mutation path
   (hermes_state_runtime.mutate_runtime_session with a receipt), 1..8 callers,
   counting successes, revisions and busy/OperationalError retries.
3. SHARED-FILE multi-process writes: N separate OS processes each open the same
   state.db and commit, to measure what variant B (N uvicorn workers) would hit:
   OperationalError 'database is locked', WAL growth, checkpoint starvation.

Writes JSON to $OUT.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

REPO = "/home/hermes/.hermes/hermes-agent/.worktrees/t_eca8cb15"
TMP = Path(tempfile.mkdtemp(prefix="t-eca8-store-"))
HOME = TMP / "home"
HOME.mkdir(parents=True, exist_ok=True)
(HOME / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\n"
    "database:\n  cache_size: -8000\n  synchronous: FULL\n"
    "sessions:\n  auto_archive: false\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
sys.path.insert(0, REPO)

DB = HOME / "state.db"


def run_threads(fn, n: int) -> tuple[list[float], list[str]]:
    barrier = threading.Barrier(n)
    lat = [0.0] * n
    errs: list[str] = []

    def wrap(i):
        barrier.wait()
        t0 = time.perf_counter()
        try:
            fn(i)
        except Exception:
            errs.append(traceback.format_exc(limit=2))
        lat[i] = (time.perf_counter() - t0) * 1000

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return lat, errs


def summarise(lat: list[float]) -> dict:
    s = sorted(lat)
    return {"p50_ms": round(s[len(s) // 2], 1), "max_ms": round(s[-1], 1),
            "sum_ms": round(sum(lat), 1)}


def main() -> int:
    from hermes_state import SessionDB

    db = SessionDB(db_path=DB)
    for i in range(200):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        for j in range(8):
            db.append_message(f"s{i}", role="user" if j % 2 == 0 else "assistant",
                              content=f"message body {i}-{j} with some words to index")
    db.close()

    res: dict = {"home": str(HOME)}

    # ---- 1. read scaling, one SessionDB per caller (the honest per-request shape)
    def reads(name: str, op) -> None:
        rows = []
        for n in (1, 2, 4, 8, 16):
            def job(i, op=op):
                d = SessionDB(db_path=DB)
                try:
                    op(d, i)
                finally:
                    d.close()
            lat, errs = run_threads(job, n)
            rows.append({"n": n, **summarise(lat), "errors": len(errs),
                         "error_sample": errs[0].splitlines()[-1] if errs else None})
        res[name] = rows

    reads("read_sessions", lambda d, i: d.list_sessions_rich(limit=20, offset=0))
    reads("read_messages", lambda d, i: d.get_messages(f"s{i % 200}", limit=500))
    reads("read_search", lambda d, i: d.search_messages("message", limit=20)
          if hasattr(d, "search_messages") else None)

    # ---- 2. write scaling in one process
    from hermes_state_runtime import begin_runtime_epoch, mutate_runtime_session

    def write_once(idx: int, counter: dict, lock: threading.Lock, errors: list) -> None:
        with lock:
            counter["n"] += 1
            k = counter["n"]
        d = SessionDB(db_path=DB)
        try:
            with d._read_ctx() as conn:
                row = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()
            epoch = row[0] if row else begin_runtime_epoch(d, instance_id="probe")
            sid = f"s{k % 200}"
            rev = (d.get_session(sid) or {}).get("runtime_revision") or 0
            mutate_runtime_session(d, epoch=epoch, principal_id="probe",
                                   expected_generation=None, session_id=sid,
                                   request_id=f"w-{k}-{time.time_ns()}", expected_revision=rev,
                                   operation="sidebar", payload={"title": f"t-{k}"})
        except Exception:
            errors.append(traceback.format_exc(limit=3))
            raise
        finally:
            d.close()

    counter = {"n": 0}
    lock = threading.Lock()
    write_rows = []
    for n in (1, 2, 4, 8):
        errors: list = []

        def job(i, errors=errors):
            write_once(i, counter, lock, errors)
        lat, errs = run_threads(job, n)
        write_rows.append({"n": n, **summarise(lat), "errors": len(errs),
                           "error_sample": errs[0].splitlines()[-1] if errs else None})
    res["write_threads"] = write_rows

    # ---- 3. shared-file writes across processes (what variant B would live with)
    script = TMP / "wproc.py"
    script.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {REPO!r})\n"
        f"os.environ['HERMES_HOME'] = {str(HOME)!r}\n"
        "os.environ['HERMES_TEST_ISOLATION']='1'\n"
        "os.environ['HERMES_STATE_DB_GUARD_BYPASS']='1'\n"
        "import json\n"
        "from hermes_state import SessionDB\n"
        "from hermes_state_runtime import begin_runtime_epoch, mutate_runtime_session\n"
        f"db=SessionDB(db_path={str(DB)!r})\n"
        "ok=busy=other=0\n"
        "with db._read_ctx() as c:\n"
        "    r=c.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()\n"
        "epoch=r[0] if r else begin_runtime_epoch(db, instance_id='probe')\n"
        "t0=time.time()\n"
        "N=int(sys.argv[1])\n"
        "for k in range(N):\n"
        "    sid=f's{k%200}'\n"
        "    rev=(db.get_session(sid) or {}).get('runtime_revision') or 0\n"
        "    try:\n"
        "        mutate_runtime_session(db, epoch=epoch, principal_id='p', expected_generation=None,\n"
        "            session_id=sid, request_id=f'p{sys.argv[2]}-{k}', expected_revision=rev,\n"
        "            operation='sidebar', payload={'title': f'x{sys.argv[2]}-{k}'})\n"
        "        ok+=1\n"
        "    except Exception as e:\n"
        "        if 'locked' in str(e).lower() or 'busy' in str(e).lower(): busy+=1\n"
        "        else: other+=1; print('ERR', type(e).__name__, str(e)[:160])\n"
        "db.close()\n"
        "print(json.dumps({'ok':ok,'busy':busy,'other':other,'secs':round(time.time()-t0,2)}))\n",
        encoding="utf-8")

    import subprocess
    proc_rows = []
    for procs, per in ((1, 40), (2, 40), (4, 40), (8, 40)):
        env = dict(os.environ)
        start = time.time()
        ps = [subprocess.Popen([sys.executable, str(script), str(per), str(p)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
              for p in range(procs)]
        outs = []
        for p in ps:
            out, err = p.communicate(timeout=300)
            outs.append((out.strip().splitlines()[-1:] or [""])[0] or err.strip()[-300:])
        parsed = []
        for line in outs:
            try:
                parsed.append(json.loads(line))
            except Exception:
                parsed.append({"raw": line[:300]})
        wal = DB.with_suffix(".db-wal")
        proc_rows.append({"procs": procs, "per_proc": per,
                          "wall_s": round(time.time() - start, 2),
                          "ok": sum(p.get("ok", 0) for p in parsed),
                          "busy": sum(p.get("busy", 0) for p in parsed),
                          "other": sum(p.get("other", 0) for p in parsed),
                          "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                          "detail": parsed})
    res["write_processes"] = proc_rows
    res["db_bytes"] = DB.stat().st_size if DB.exists() else 0

    out = os.environ.get("OUT", str(TMP / "result.json"))
    Path(out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
