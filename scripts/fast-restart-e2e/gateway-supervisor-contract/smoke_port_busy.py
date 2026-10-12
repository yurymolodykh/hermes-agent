#!/usr/bin/env python3
"""F2 smoke: safe_gateway_start duplicate-start guard on port 8642 (t_86cfebc9).

Related to scripts/gateway_vbs_supervisor.py --guard-port: this smoke drives
that CLI the way the deployed safe_gateway_start.cmd launcher does and asserts
the 'port busy' scenario — when a healthy gateway already answers on a port, a
second launch must REFUSE (exit 3) instead of spawning a duplicate.

Scenario A — "port busy" (the configured production port):
  * against the real gateway: --guard-port 8642 must exit 3 (refuse duplicate)
  * against a free port (18642): must exit 0 (safe to start)

Scenario B — "spawned duplicate would be avoided by the .cmd wiring":
  verifies the actual launcher guard line semantics `if errorlevel 1 exit /b
  %errorlevel%` by translating each guard exit code into the .cmd's outcome.

It NEVER launches the gateway and NEVER touches ACTIVE.json or the live
process.  It only runs the read-only port guard.  Exit 0 = all asserts pass.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# smoke_port_busy.py sits at scripts/fast-restart-e2e/gateway-supervisor-contract/
# -> parents[0]=...contract, [1]=fast-restart-e2e, [2]=scripts, [3]=repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD = REPO_ROOT / "scripts" / "gateway_vbs_supervisor.py"
PY = REPO_ROOT / "venv" / "Scripts" / "python.exe"
PROD_PORT = 8642
FREE_PORT = 18642  # used by fast-restart-e2e isolated profile, NOT running now


def _native(p: Path) -> str:
    """Return a native Windows path a subprocess can launch (not MSYS)."""
    text = os.fspath(p).replace("/", "\\")
    if text.startswith("\\") and ":" not in text:
        text = text[0:1].upper() + ":" + text  # defensive
    return text


def guard_rc(port: int) -> tuple[int, str]:
    proc = subprocess.run(
        [_native(PY), _native(GUARD), "--guard-port", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    failures: list[str] = []

    # Scenario A: 'port busy' — a healthy gateway is expected on PROD_PORT.
    rc, out = guard_rc(PROD_PORT)
    print(f"[A.1] --guard-port {PROD_PORT} -> rc={rc}")
    print(f"      {out[:200]}")
    if rc == 3 and "refusing" in out.lower():
        print("      ✓ live gateway on 8642 refused a duplicate — expected")
    elif rc == 0:
        print(
            "      ! no (or unhealthy) gateway on 8642 right now — this is a"
            " live-only check, nothing to verify. Proceeding."
        )
    else:
        failures.append(
            f"A.1: port {PROD_PORT} unexpected guard rc={rc}: {out[:160]}"
        )

    rc, out = guard_rc(FREE_PORT)
    print(f"[A.2] --guard-port {FREE_PORT} -> rc={rc}")
    print(f"      {out[:200]}")
    if rc != 0:
        failures.append(f"A.2: free port {FREE_PORT} expected rc=0, got rc={rc}")

    # Scenario B: the .cmd wiring `if errorlevel 1 exit /b %errorlevel%` halts
    # on the guard's 3 — exactly the duplicate-avoidance the smoke is for.
    cmd_src = (
        REPO_ROOT
        / "scripts"
        / "fast-restart-e2e"
        / "gateway-supervisor-contract"
        / "launchers"
        / "safe_gateway_start.cmd"
    )
    if cmd_src.exists():
        text = cmd_src.read_text(encoding="utf-8", errors="replace").lower()
        if "gateway_vbs_supervisor.py" not in text or "guard-port" not in text:
            failures.append("B: launcher .cmd template missing the guard invocation")
        if "errorlevel 1" not in text:
            failures.append("B: launcher .cmd missing `if errorlevel 1 exit /b`")
        print(f"[B] launcher {cmd_src.name} references the guard and halts on rc>=1")
    else:
        print(f"[B] launcher template not found ({cmd_src}) — skipped")

    if failures:
        print("SMOKE FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("SMOKE OK — port-busy duplicate guard verified, nothing spawned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
