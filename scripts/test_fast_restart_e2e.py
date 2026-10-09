#!/usr/bin/env python3
r"""scripts/test_fast_restart_e2e.py — end-to-end smoke test for the gateway
fast-restart path (A1), cron resume tokens (A2), and generation-scoped kanban
reclaim (A2), fully isolated from the production gateway.

What it proves (acceptance):
  1. A minimal isolated test profile boots a REAL gateway with ONLY the
     api_server platform on port 18642, under a temporary HERMES_HOME and a
     temporary kanban home (production port 8642 untouched, no telegram/yadisk).
  2. A cron LLM-agent job (no_agent=false, resume_mode=resume) driven by a
     local MOCK LLM (the "long-running script stub" that writes checkpoints to
     a file) starts and stays mid-flight.
  3. An in-band fast restart (".gateway-fast-restart-request.json" marker,
     consumed by the gateway's fast-restart watcher) restarts the gateway; the
     new generation is healthy on 18642 within <= 30 s (downtime_s).
  4. The interrupted cron run is RESUMED (same session continued), reaches a
     terminal (completed) status, and the checkpoint file is preserved with
     both pre-restart and post-restart entries.
  5. "FAST_RESTART" marker appears in gateway.log.
  6. A test kanban card stamped with the OLD generation (dead worker) is
     reclaimed to 'ready' within <= 60 s.

The harness is the gateway supervisor: it launches generation 1, and when
generation 1 exits promptly after the in-band fast-restart marker (exit 0 on
`gateway run`; EX_TEMPFAIL 75 where a service manager handles relaunch) it
relaunches generation 2 and measures the healthcheck gap.

The mock LLM also encodes the resume proof: every completion request is
recorded (phase, history length, and whether it carries the cron scheduler's
re-arm continuation instruction — “Продолжай выполнение cron-задачи …
Сессия <id> …”, which the scheduler prepends ONLY when resuming an
interrupted run from its stored session). Seeing that marker in a post-restart
request is direct proof the SAME session was continued, not cold-restarted.

Usage:
  python scripts/test_fast_restart_e2e.py [--repo PATH] [--python PATH]
      [--report PATH] [--port 18642] [--mock-port 18643] [--keep] [-v]

Exit code 0 => all asserts passed and a JSON report written.
Exit code 1 => one or more asserts failed (the report is still written).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Production gateway listener port — MUST never be bound by this harness.
PROD_GATEWAY_PORT = 8642
FAST_RESTART_REQUEST_FILENAME = ".gateway-fast-restart-request.json"
GATEWAY_GENERATION_MARKER = "kanban/.gateway_generation"
JOBS_FILENAME = "cron/jobs.json"
EX_TEMPFAIL = 75  # exit code the gateway uses to ask its supervisor to restart
JOB_ID = "e2e-fast-restart-0001"
RECLAIM_TASK_ID = "e2e-reclaim-0001"
PROFILE_NAME = "test-fast-restart"

# Keys whose presence in the gateway env could reach production messaging or
# storage. The harness strips them, so "no real Telegram messages are sent".
_ENV_BLOCKLIST_PREFIXES = (
    "TELEGRAM_",
    "YADISK_",
    "HERMES_OPENWEBUI_",
    "HERMES_KANBAN_",
    "HERMES_SESSION_",
    "HERMES_PROFILE",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_DEFAULT_MODEL",
)
_ENV_BLOCKLIST_EXACT = {
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
}


class TerminalStyle:
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RESET = "\033[0m"


def _log(msg: str) -> None:
    print(f"{TerminalStyle.CYAN}[e2e]{TerminalStyle.RESET} {msg}", flush=True)


def _ok(msg: str) -> None:
    print(f"{TerminalStyle.GREEN}  ok    {msg}{TerminalStyle.RESET}", flush=True)


def _warn(msg: str) -> None:
    print(f"{TerminalStyle.YELLOW}  warn  {msg}{TerminalStyle.RESET}", flush=True)


def _fail(msg: str) -> None:
    print(f"{TerminalStyle.RED}  FAIL  {msg}{TerminalStyle.RESET}", flush=True)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _flatten_content(msg: dict) -> str:
    """Flatten an OpenAI-style message content (str or list of parts) to text."""
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for it in c:
            if isinstance(it, dict):
                out.append(str(it.get("text") or it.get("content") or ""))
            else:
                out.append(str(it))
        return "".join(out)
    return str(c or "")


def _free_port(preferred: int) -> int:
    """Return ``preferred`` if free, else the first free port >= preferred."""
    for port in range(preferred, preferred + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port found starting at {preferred}")


def _is_port_bound(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


# ---------------------------------------------------------------------------
# Mock LLM — the "long-running script stub" that writes checkpoints to a file.
# It holds completion responses until released, which keeps the cron agent run
# mid-flight while the harness triggers the fast restart.
# ---------------------------------------------------------------------------
class MockLLM:
    def __init__(self, port: int, checkpoint_path: Path, hold_timeout: float = 240.0):
        self.port = port
        self.checkpoint_path = Path(checkpoint_path)
        self.hold_timeout = hold_timeout
        self.released_event = threading.Event()
        self._lock = threading.Lock()
        self._count = 0
        self._requests: list[dict] = []
        self._phase_fn = lambda: "post"
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set_phase_fn(self, fn) -> None:
        self._phase_fn = fn

    def release(self) -> None:
        self.released_event.set()

    def _append_checkpoint(self, rec: dict) -> None:
        with self._lock:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            with self.checkpoint_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _handle_chat(self, body: dict) -> dict:
        messages = body.get("messages") or []
        hist_len = len(messages)
        joined = "\n".join(_flatten_content(m) for m in messages)
        # The cron scheduler's re-arm path (cron/scheduler.py run_one_job)
        # PREPENDS an explicit continuation instruction to the prompt of an
        # interrupted run and reuses the SAME stored session id. Seeing that
        # marker in a post-restart request is direct proof of resume (A2);
        # a rerun-from-scratch would carry neither.
        resume_marker = "Продолжай выполнение cron-задачи" in joined
        _m = re.search(r"Сессия (\S+)", joined)
        resume_session_seen = _m.group(1) if _m else None
        with self._lock:
            self._count += 1
            n = self._count
        tail = _flatten_content(messages[-1])[-80:] if messages else ""
        rec = {
            "ts": _utc_now_iso(),
            "t": round(time.monotonic(), 3),
            "n": n,
            "phase": self._phase_fn(),
            "hist_len": hist_len,
            "resume_marker": bool(resume_marker),
            "resume_session_seen": resume_session_seen,
            "model": body.get("model", ""),
            "prompt_tail": tail,
        }
        with self._lock:
            self._requests.append(rec)
        self._append_checkpoint(rec)

        # Hold the response open until released (or until the hold timeout).
        if not self.released_event.wait(self.hold_timeout):
            # Released nothing by deadline; still answer so the run can finish.
            pass
        return {
            "id": f"chatcmpl-e2e-{n}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "mock-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Задача выполнена. Итог: e2e fast-restart resume ok. "
                            "Финальный ответ."
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "total_tokens": 20,
            },
        }

    def _make_handler(self):
        owner = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _read_body(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw.decode("utf-8", "replace")) or {}
                except Exception:
                    return {}

            def _json(self, code: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # The client (a gateway generation) died mid-request — e.g.
                    # the held pre-restart request unblocked after gen1 already
                    # exited. Expected in this test; not a harness error.
                    pass

            def do_POST(self):
                body = self._read_body()
                if self.path == "/v1/chat/completions":
                    self._json(200, owner._handle_chat(body))
                elif self.path == "/__control/release":
                    owner.release()
                    self._json(200, {"released": True})
                else:
                    self._json(404, {"error": "not found"})

            def do_GET(self):
                if self.path == "/__control/state":
                    with owner._lock:
                        self._json(200, {
                            "count": owner._count,
                            "released": owner.released_event.is_set(),
                            "requests": list(owner._requests),
                        })
                elif self.path == "/__control/checkpoints":
                    if owner.checkpoint_path.exists():
                        text = owner.checkpoint_path.read_text(encoding="utf-8")
                        self._json(200, {"checkpoints": text})
                    else:
                        self._json(200, {"checkpoints": ""})
                else:
                    self._json(404, {"error": "not found"})

        return _H

    def start(self) -> None:
        server_cls = type(
            "_DaemonHTTPServer",
            (ThreadingHTTPServer,),
            {"daemon_threads": True},  # don't let stuck handler threads keep us alive
        )
        self._server = server_cls(("127.0.0.1", self.port), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def requests(self) -> list[dict]:
        with self._lock:
            return list(self._requests)


# ---------------------------------------------------------------------------
# Isolated test profile builder
# ---------------------------------------------------------------------------
def build_test_profile(root: Path, api_port: int, mock_port: int) -> Path:
    """Create the minimal isolated profile (api_server only) under ``root``.

    Layout: ``root/profiles/test-fast-restart`` is HERMES_HOME; ``root`` is the
    kanban home (HERMES_KANBAN_HOME). Returns the profile dir path.
    """
    profile_dir = root / "profiles" / PROFILE_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    (root / "kanban").mkdir(parents=True, exist_ok=True)

    api_key = secrets.token_urlsafe(32)
    mock_key = secrets.token_urlsafe(16)

    config = {
        "telegram_delivery_policy": "off",
        "model": {
            "default": "mock-model",
            "provider": "mock",
            "context_length": 131072,
        },
        "providers": {
            "mock": {
                "base_url": f"http://127.0.0.1:{mock_port}/v1",
                "model": "mock-model",
                "key_env": "MOCK_API_KEY",
                "request_timeout_seconds": 300,
                "stale_timeout_seconds": 400,
                "discover_models": False,
                "models": {"mock-model": {}},
            }
        },
        "custom_providers": [
            {
                "name": "mock",
                "base_url": f"http://127.0.0.1:{mock_port}/v1",
                "key_env": "MOCK_API_KEY",
                "models": {"mock-model": {"context_length": 131072}},
            }
        ],
        # Small dispatch cadence so the reclaim assert (<= 60 s) is met fast.
        "kanban": {"dispatch_interval_seconds": 5},
    }
    (profile_dir / "config.yaml").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    env_lines = [
        f"API_SERVER_KEY={api_key}",
        f"API_SERVER_PORT={api_port}",
        "API_SERVER_HOST=127.0.0.1",
        f"MOCK_API_KEY={mock_key}",
        "HERMES_CRON_TIMEOUT=600",
    ]
    (profile_dir / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return profile_dir


def write_test_job(profile_dir: Path, mock_port: int) -> None:
    """Write a single LLM-agent cron job (no_agent=false, resume_mode=resume)
    pinned to the mock LLM, due immediately (fires on the next cron tick)."""
    jobs_path = profile_dir / JOBS_FILENAME
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    job = {
        "id": JOB_ID,
        "name": "e2e-fast-restart-resume-probe",
        "prompt": (
            "Выполни задачу и верни короткий финальный ответ одним предложением. "
            "Задача: подтвердить, что cron-запуск резюмируется после быстрого "
            "рестарта gateway."
        ),
        "skills": [],
        "skill": None,
        "model": "mock-model",
        "provider": "mock",
        "provider_snapshot": "mock",
        "model_snapshot": "mock-model",
        "base_url": f"http://127.0.0.1:{mock_port}/v1",
        "no_agent": False,
        "resume_mode": "resume",
        "schedule": {"kind": "cron", "expr": "0 0 1 1 1", "display": "0 0 1 1 1"},
        "schedule_display": "0 0 1 1 1",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now.isoformat(),
        # Due on the first tick after boot.
        "next_run_at": now.isoformat(),
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "failure_streak": 0,
        "deliver": "local",
        "origin": None,
        "enabled_toolsets": [],
        "workdir": None,
        "fire_claim": None,
        "resume_token": None,
    }
    with jobs_path.open("w", encoding="utf-8") as f:
        json.dump([job], f, ensure_ascii=False, indent=2)


def write_reclaim_card(root: Path, generation: str, worker_pid: int, assignee: str) -> None:
    """Insert a fake 'running' card stamped with the OLD gateway generation and
    a worker pid that will be dead once the old gateway exits."""
    db_path = root / "kanban.db"
    now = int(time.time())
    conn = None
    import sqlite3
    for _ in range(150):
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                break
            except sqlite3.Error:
                time.sleep(0.25)
        time.sleep(0.25)
    if conn is None:
        raise RuntimeError(f"kanban.db not ready at {db_path}")
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, title, body, assignee, status, priority, created_at, "
                "started_at, workspace_kind, workspace_path, worker_pid, "
                "gateway_generation, claim_lock, claim_expires, "
                "last_heartbeat_at, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RECLAIM_TASK_ID,
                    "e2e reclaim probe (dead generation)",
                    "Created by the fast-restart e2e harness. "
                    "Expects reclaim to 'ready' by the next gateway generation.",
                    assignee,
                    "running",
                    10,
                    now,
                    now,
                    "scratch",
                    None,
                    worker_pid,
                    generation,
                    "e2e-claim",
                    now + 3600,
                    now,
                    "e2e-harness",
                ),
            )
    finally:
        conn.close()


def read_reclaim_status(root: Path) -> str | None:
    import sqlite3
    db_path = root / "kanban.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (RECLAIM_TASK_ID,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def read_gateway_generation(root: Path) -> str | None:
    p = root / GATEWAY_GENERATION_MARKER
    try:
        val = p.read_text(encoding="utf-8").strip()
        return val or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _gateway_env(repo: Path, profile_dir: Path, root: Path, base_env: dict) -> dict:
    env = dict(base_env)
    for k in list(env.keys()):
        if k in _ENV_BLOCKLIST_EXACT or k.startswith(_ENV_BLOCKLIST_PREFIXES):
            env.pop(k, None)
    env["HERMES_HOME"] = str(profile_dir)
    env["HERMES_KANBAN_HOME"] = str(root)
    parts = [str(repo)]
    # _resolve_python() runs the venv's REAL base interpreter, which does not
    # auto-activate the venv; inject its site-packages so the gateway's deps
    # and the editable hermes-agent install resolve. Repo itself comes first.
    sp = repo / "venv" / ("Lib" if os.name == "nt" else "lib") / "site-packages"
    if sp.is_dir():
        parts.append(str(sp))
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Never let a hosting worker session leak into the gateway.
    for k in _ENV_BLOCKLIST_EXACT:
        env.pop(k, None)
    return env


def _spawn_gateway(python: Path, repo: Path, profile_dir: Path, root: Path,
                   env: dict, out_path: Path) -> subprocess.Popen:
    cmd = [str(python), "-m", "hermes_cli.main", "gateway", "run", "--replace"]
    out = out_path.open("ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(profile_dir),
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    return proc


def _resolve_python(repo: Path, explicit: str | None) -> Path:
    """Pick the python that runs the gateway under test.

    On Windows a ``<venv>/Scripts/python.exe`` is a launcher STUB: it spawns
    the venv's real base interpreter as a child process and stays alive as a
    parent. That two-process topology would make ``Popen.pid`` / ``wait()`` /
    the restart-marker ``target_pid`` all observe the stub instead of the
    gateway, so prefer the REAL base interpreter resolved from the venv's
    ``pyvenv.cfg`` (its site-packages are injected via ``_gateway_env``).
    """
    if explicit:
        return Path(explicit).resolve()
    try:
        cfg = repo / "venv" / "pyvenv.cfg"
        if cfg.exists():
            for line in cfg.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("home ="):
                    base = Path(stripped.split("=", 1)[1].strip())
                    exe = base / ("python.exe" if os.name == "nt" else "python3")
                    if exe.exists():
                        return exe.resolve()
    except OSError:
        pass
    if sys.executable and Path(sys.executable).exists():
        return Path(sys.executable).resolve()
    return (repo / "venv" / "Scripts" / "python.exe").resolve()


def _read_gateway_pidfile(profile_dir: Path) -> int | None:
    """Read the REAL gateway pid from the profile's gateway.pid JSON record,
    defense-in-depth against any remaining launcher-stub pid mismatch."""
    try:
        data = json.loads((profile_dir / "gateway.pid").read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
        return pid or None
    except Exception:
        return None


def _pid_dead(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # Windows 8+
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid)
            )
            if not handle:
                return True  # cannot open -> gone (or gone-permission; treat dead)
            try:
                code = wintypes.DWORD()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value != 259  # 259 == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(pid, 0)  # posix liveness probe
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except Exception:
        return False


def _wait_gateway_exit(proc: subprocess.Popen, real_pid: int | None,
                       timeout: float) -> int | None:
    """Wait for the gateway to exit; return its exit code (None if the real
    gateway was a non-child of ``proc`` and vanished, e.g. a launcher stub
    parent lingering after the child gateway exited)."""
    deadline = time.monotonic() + timeout
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        if real_pid and real_pid != proc.pid and _pid_dead(real_pid):
            return None  # real gateway gone; stub parent may linger
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        time.sleep(0.25)


def _http_json(url: str, method: str = "GET", body: dict | None = None,
               timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _health_ok(port: int) -> bool:
    try:
        payload = _http_json(f"http://127.0.0.1:{port}/health", timeout=2.5)
        return payload.get("status") == "ok"
    except Exception:
        return False


def _wait_for(pred, timeout: float, interval: float = 0.5, label: str = "") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# Diagnostics for jobs.json reads (the cron store owns that file; a transient
# Windows lock / atomic-replace can make a read fail and look like "no job").
_JOB_READ_STATS = {"errors": 0, "last_error": None}


def _read_jobs(profile_dir: Path) -> list[dict]:
    p = profile_dir / JOBS_FILENAME
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # The cron store persists a {"jobs": [...], "updated_at": ...}
            # wrapper after its first load ("bare list wrapped as dict").
            jobs = data.get("jobs") or data.get("entries") or []
            return jobs if isinstance(jobs, list) else []
        return []
    except Exception as exc:
        _JOB_READ_STATS["errors"] += 1
        _JOB_READ_STATS["last_error"] = f"{type(exc).__name__}: {exc}"
        return []


def _job_terminal(jobs: list[dict]) -> tuple[bool, dict | None]:
    for j in jobs:
        if j.get("id") != JOB_ID:
            continue
        status = j.get("last_status")
        has_token = bool(j.get("resume_token"))
        terminal = bool(status) and not has_token
        return terminal, j
    return False, None


def _checkpoint_lines(checkpoint: Path) -> list[dict]:
    if not checkpoint.exists():
        return []
    out = []
    for line in checkpoint.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Fast-restart e2e smoke test.")
    ap.add_argument("--repo", default=None, help="hermes-agent repo root (auto if omitted)")
    ap.add_argument("--python", default=None, help="python interpreter (venv python if omitted)")
    ap.add_argument("--report", default=None, help="JSON report output path")
    ap.add_argument("--port", type=int, default=18642, help="api_server test port (default 18642)")
    ap.add_argument("--mock-port", type=int, default=18643, help="mock LLM port (default 18643)")
    ap.add_argument("--hold-timeout", type=float, default=240.0, help="mock hold cap seconds")
    ap.add_argument("--keep", action="store_true", help="keep the temp profile after the run")
    ap.add_argument("--assignee", default="e2e-nosuch-profile-0001",
                    help="assignee stamp for the pretend reclaim card (default is a "
                         "nonexistent profile so the dispatcher never spawns it)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    repo = Path(a.repo).resolve() if a.repo else Path(__file__).resolve().parent.parent
    python = _resolve_python(repo, a.python)
    api_port = a.port
    if api_port == PROD_GATEWAY_PORT:
        print(f"FATAL: refusing to run the e2e on production port {PROD_GATEWAY_PORT}",
              file=sys.stderr)
        return 2

    if _is_port_bound(api_port):
        print(f"FATAL: test port {api_port} is already in use — aborting (nothing touched).",
              file=sys.stderr)
        return 2

    # Snapshot the production port's state BEFORE the test starts. The prod
    # gateway legitimately owns 8642 on this host; isolation means OUR run
    # never binds it and leaves its state untouched — not that it is free.
    prod_bound_before = _is_port_bound(PROD_GATEWAY_PORT)

    base_env = os.environ.copy()
    # Defense in depth: prove the harness itself carries no telegram/yadisk secrets.
    leaked = [k for k in base_env if k.startswith(("TELEGRAM_", "YADISK_"))]
    if leaked:
        _warn(f"harness env carries messaging secrets ({', '.join(sorted(leaked))}) — "
              "the gateway env strips them anyway")

    # ---- scratch root -------------------------------------------------------
    run_stamp = time.strftime("%Y%m%d_%H%M%S")
    base_tmp = Path(tempfile.gettempdir()) / "hermes-e2e"
    base_tmp.mkdir(parents=True, exist_ok=True)
    root = base_tmp / run_stamp
    root.mkdir(parents=True, exist_ok=True)
    profile_dir = build_test_profile(root, api_port, a.mock_port)
    write_test_job(profile_dir, a.mock_port)
    _log(f"test root:      {root}")
    _log(f"test HERMES_HOME: {profile_dir}")
    _log(f"test kanban home: {root}")

    checkpoint = root / "checkpoints.jsonl"
    mock = MockLLM(a.mock_port, checkpoint, hold_timeout=a.hold_timeout)
    phase = {"t0": None}
    mock.set_phase_fn(lambda: ("pre" if phase["t0"] is None else "post"))
    mock.start()
    _ok(f"mock LLM listening on 127.0.0.1:{a.mock_port}")

    asserts: list[dict] = []
    report = {
        "generated_at": _utc_now_iso(),
        "repo": str(repo),
        "python": str(python),
        "api_port": api_port,
        "mock_port": a.mock_port,
        "prod_port_untouched": PROD_GATEWAY_PORT,
        "hermes_home": str(profile_dir),
        "kanban_home": str(root),
        "profile": PROFILE_NAME,
        "downtime_s": None,
        "resumed": None,
        "reclaimed": None,
        "reclaimed_s": None,
        "resume_evidence": {},
        "isolation": {
            "platforms": ["api_server"],
            "telegram_tokens_in_env": False,
            "yadisk_tokens_in_env": False,
        },
        "asserts": asserts,
    }

    def add_assert(name: str, passed: bool, detail: str = "") -> None:
        asserts.append({"name": name, "pass": bool(passed), "detail": detail})
        if passed:
            _ok(f"{name} {('— ' + detail) if detail else ''}")
        else:
            _fail(f"{name} — {detail}")

    try:
        # ---- generation 1 --------------------------------------------------
        _log("booting gateway generation 1 ...")
        gw1_env = _gateway_env(repo, profile_dir, root, base_env)
        report["isolation"]["telegram_tokens_in_env"] = any(
            k.startswith("TELEGRAM_") for k in gw1_env
        )
        report["isolation"]["yadisk_tokens_in_env"] = any(
            k.startswith("YADISK_") for k in gw1_env
        )
        gw1 = _spawn_gateway(python, repo, profile_dir, root, gw1_env, root / "gen1.out")
        report["gen1_pid"] = gw1.pid
        _log(f"gen1 pid={gw1.pid}")
        t_boot = time.monotonic()
        booted = _wait_for(lambda: _health_ok(api_port), timeout=60.0, label="gen1 health")
        gen1_boot_s = time.monotonic() - t_boot
        _ok(f"gen1 healthy after {gen1_boot_s:.1f}s")

        # Resolve the REAL gateway pid (defense-in-depth against any launcher
        # stub mismatch) and use it for the restart marker, the reclaim card
        # worker pid, and the exit wait.
        gw_pid = gw1.pid
        real_pid = _read_gateway_pidfile(profile_dir)
        if real_pid and real_pid != gw1.pid:
            _warn(f"gateway.pid reports {real_pid} != spawn pid {gw1.pid}; "
                  "using the gateway.pid value for marker/exit tracking")
            gw_pid = real_pid

        add_assert("gen1-healthy", booted, f"/health on {api_port} within {gen1_boot_s:.1f}s")
        if not booted:
            raise RuntimeError(
                f"gen1 did not become healthy on {api_port} within 60s — see {root / 'gen1.out'}"
            )

        gen1_generation = read_gateway_generation(root)
        if gen1_generation is None:
            seen_marker = _wait_for(
                lambda: read_gateway_generation(root) is not None,
                timeout=15.0, interval=0.5, label="generation marker",
            )
            gen1_generation = read_gateway_generation(root)
            if not seen_marker:
                _warn("gateway generation marker not seen within 15s; "
                      "reclaim stamp falls back to pid-only")
        _log(f"gen1 gateway generation: {gen1_generation}")

        # ---- mid-flight -----------------------------------------------------
        # Wait for the cron job to fire and the mock to record the first LLM
        # call (checkpoint line >= 1) — the run is long-running mid-flight
        # because the mock holds its response open.
        fired = _wait_for(
            lambda: len(_checkpoint_lines(checkpoint)) >= 1 or _job_terminal(_read_jobs(profile_dir))[0],
            timeout=90.0, interval=1.0, label="job fire",
        )
        cps = _checkpoint_lines(checkpoint)
        _ok(f"job fired; checkpoint lines so far: {len(cps)} -> {[c.get('hist_len') for c in cps]}")
        add_assert("cron-job-mid-flight", fired,
                   f"mock recorded {len(cps)} LLM call(s); run is held mid-flight")

        # ---- pretend old-generation kanban card ------------------------------
        write_reclaim_card(root, gen1_generation or "pid:0:start:0:boot:0", gw_pid, a.assignee)
        _ok(f"pretend running card {RECLAIM_TASK_ID} stamped with gen1 ({gw_pid})")

        # ---- trigger fast restart -------------------------------------------
        marker = {
            "fast": True,
            "reason": "e2e fast-restart smoke (isolated test profile)",
            "requester_pid": os.getpid(),
            "written_at": _utc_now_iso(),
            "target_pid": gw_pid,
        }
        marker_path = profile_dir / FAST_RESTART_REQUEST_FILENAME
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        t0 = time.monotonic()
        phase["t0"] = t0
        _log(f"fast-restart marker written at t0 (pid {gw_pid})")

        # -- supervise exit + relaunch -----------------------------------------
        try:
            rc = _wait_gateway_exit(gw1, gw_pid, 30)
            _log(f"gen1 exited rc={rc}")
            # In-band fast restart under `gateway run` exits CLEANLY with 0
            # (gateway/run.py finishes with runner.exit_code None → return
            # True → exit 0).  EX_TEMPFAIL (75) is the documented code where a
            # service manager performs the relaunch (gateway/restart.py
            # GATEWAY_SERVICE_RESTART_EXIT_CODE).  Accept either; report
            # which one we observed.
            assert_used_fast_exit = rc in (0, EX_TEMPFAIL)
            if not assert_used_fast_exit:
                _warn(f"gen1 exit code {rc} not in (0, {EX_TEMPFAIL}); "
                      "still proceeding — restart observable is gen2 coming up")
        except subprocess.TimeoutExpired:
            _fail("gen1 did not exit within 30s of the fast-restart marker")
            add_assert("gen1-restarted", False, "gen1 stayed alive after fast-restart marker")
            raise
        add_assert("gen1-restarted", True, f"gen1 exited rc={rc} within 30s of the marker")
        add_assert("gen1-fast-exit", assert_used_fast_exit,
                   f"exit code {rc} in (0, {EX_TEMPFAIL})")

        gw2 = _spawn_gateway(python, repo, profile_dir, root, gw1_env, root / "gen2.out")
        report["gen2_pid"] = gw2.pid
        _log(f"gen2 pid={gw2.pid} (supervisor relaunch)")
        healthy2 = _wait_for(lambda: _health_ok(api_port), timeout=35.0, label="gen2 health")
        t1 = time.monotonic()
        downtime_s = round(t1 - t0, 3)
        report["downtime_s"] = downtime_s
        add_assert("gateway-downtime<=30s", healthy2 and downtime_s <= 30.0,
                   f"downtime_s={downtime_s}s (marker -> gen2 /health), healthy2={healthy2}")

        # ---- release the mock so the resumed run can complete -----------------
        try:
            _http_json(f"http://127.0.0.1:{a.mock_port}/__control/release", method="POST",
                       body={"released": True})
            _ok("mock LLM released")
        except Exception as exc:
            _warn(f"mock release call failed: {exc}")

        # ---- resumed + terminal ---------------------------------------------
        terminal_ok = _wait_for(
            lambda: _job_terminal(_read_jobs(profile_dir))[0],
            timeout=150.0, interval=2.0, label="job terminal",
        )
        if not terminal_ok:
            # Grace retries: jobs.json is owned by the cron store and can be
            # momentarily unreadable (Windows write handle / atomic replace)
            # right around the final state write.
            for _ in range(5):
                if _job_terminal(_read_jobs(profile_dir))[0]:
                    terminal_ok = True
                    break
                time.sleep(2)
        time.sleep(3)  # let final checkpoint writes flush
        cps2 = _checkpoint_lines(checkpoint)
        pre = [c for c in cps2 if c.get("phase") == "pre"]
        post = [c for c in cps2 if c.get("phase") == "post"]
        pre_hist = [c["hist_len"] for c in pre]
        post_hist = [c["hist_len"] for c in post]
        _, job_final = _job_terminal(_read_jobs(profile_dir))
        # A RESUMED run is one where a post-restart completion request carries
        # the scheduler's re-arm continuation instruction and the SAME stored
        # session id (cron/scheduler.py run_one_job prepends "Продолжай
        # выполнение cron-задачи ... Сессия <id> ..."). Direct proof the
        # interrupted session was continued, not cold-restarted.
        resumed_req = next((c for c in post if c.get("resume_marker")), None)
        connection_continued = resumed_req is not None
        resume_sessions = sorted(
            {c.get("resume_session_seen") for c in post if c.get("resume_session_seen")}
        )
        report["resume_evidence"] = {
            "pre_checkpoints": len(pre),
            "post_checkpoints": len(post),
            "pre_hist_len": pre_hist,
            "post_hist_len": post_hist,
            "connection_continued": connection_continued,
            "resume_marker_seen": bool(connection_continued),
            "resume_sessions": resume_sessions,
            "job_last_status": (job_final or {}).get("last_status"),
            "job_resume_token_present": bool((job_final or {}).get("resume_token")),
            "job_read_errors": _JOB_READ_STATS["errors"],
            "job_read_last_error": _JOB_READ_STATS["last_error"],
        }
        preserved = checkpoint.exists() and len(pre) >= 1 and len(post) >= 1
        resumed_ok = terminal_ok and preserved and connection_continued
        report["resumed"] = bool(resumed_ok)
        add_assert("cron-run-resumed-completed",
                   resumed_ok,
                   f"terminal={terminal_ok}, preserved={preserved}, "
                   f"continued={connection_continued} (pre={len(pre)}, post={len(post)})")
        add_assert("resume-continued-session", connection_continued,
                   f"post-restart request carries the scheduler continuation marker; "
                   f"sessions {resume_sessions} => interrupted session continued")

        # ---- FAST_RESTART marker in gateway.log --------------------------------
        glog = profile_dir / "logs" / "gateway.log"
        had_marker = False
        marker_lines = []
        if glog.exists():
            txt = glog.read_text(encoding="utf-8", errors="replace")
            marker_lines = [ln for ln in txt.splitlines() if "FAST_RESTART" in ln]
            had_marker = bool(marker_lines)
        report["fast_restart_marker_lines"] = marker_lines[:5]
        add_assert("FAST_RESTART-marker-in-log", had_marker,
                   f"gateway.log has {len(marker_lines)} FAST_RESTART line(s)")

        # ---- kanban reclaim -----------------------------------------------------
        t_reclaim_start = time.monotonic()
        reclaimed = _wait_for(
            lambda: read_reclaim_status(root) != "running",
            timeout=60.0, interval=1.0, label="reclaim",
        )
        reclaimed_s = round(time.monotonic() - t_reclaim_start, 3)
        status_after = read_reclaim_status(root)
        report["reclaimed"] = reclaimed and status_after == "ready"
        report["reclaimed_s"] = reclaimed_s
        report["reclaim_status_after"] = status_after
        add_assert("kanban-card-reclaimed<=60s",
                   reclaimed and status_after == "ready",
                   f"card {RECLAIM_TASK_ID}: running -> {status_after} in {reclaimed_s}s")

        # ---- isolation completeness ---------------------------------------------
        prod_bound_after = _is_port_bound(PROD_GATEWAY_PORT)
        prod_state_unchanged = prod_bound_after == prod_bound_before
        only_18642 = _is_port_bound(api_port) and prod_state_unchanged
        report["isolation"]["prod_port_8642_state_before"] = prod_bound_before
        report["isolation"]["prod_port_8642_state_after"] = prod_bound_after
        add_assert("isolation-prod-8642-untouched", only_18642,
                   f"api_server on {api_port}; production {PROD_GATEWAY_PORT} state "
                   f"unchanged by test (bound_before={prod_bound_before}, "
                   f"bound_after={prod_bound_after})")

        # ---- cleanup --------------------------------------------------------------
        _log("cleaning up gateway generation 2 ...")
        try:
            gw2.terminate()
            gw2.wait(timeout=15)
        except Exception:
            try:
                gw2.kill()
            except Exception:
                pass
        mock.stop()

    except Exception as exc:  # noqa: BLE001
        import traceback
        # Tear down any gateway generations that are still alive.
        for _name in ("gw1", "gw2"):
            _proc = locals().get(_name)
            if _proc is not None:
                try:
                    _proc.terminate()
                except Exception:
                    try:
                        _proc.kill()
                    except Exception:
                        pass
        _fail(f"harness exception: {exc}")
        if a.verbose:
            traceback.print_exc()
        add_assert("harness-no-exception", False, str(exc))
        report["exception"] = str(exc)

    all_passed = bool(asserts) and all(x["pass"] for x in asserts)
    report["all_asserts_passed"] = all_passed
    # Default report path lives OUTSIDE the temp root (cwd) so it survives the
    # default post-run cleanup of the scratch HERMES_HOME/kanban home, and a
    # fresh worker that runs the script can always find it.
    report_path = Path(a.report).resolve() if a.report else (
        Path.cwd() / "fast_restart_e2e_report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Keep a copy of the report + evidence inside the temp root too (useful when
    # --keep retains it for inspection).
    (root / "fast_restart_e2e_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Default: clean up the scratch profile / kanban home (task contract). Only
    # --keep retains it. The durable report is already written outside root.
    _kept_root = bool(a.keep)
    if not a.keep:
        try:
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            _kept_root = True  # failed to remove — leave the root in place

    print("\n" + "=" * 78)
    print(f"ALL ASSERTS PASSED: {all_passed}  (downtime_s={report.get('downtime_s')}, "
          f"resumed={report.get('resumed')}, reclaimed={report.get('reclaimed')})")
    print(f"report: {report_path}")
    print(f"test root {'(kept for inspection)' if _kept_root else '(cleaned up)'}: {root}")
    print("=" * 78)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
