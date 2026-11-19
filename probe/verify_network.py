"""Criterion 3, the exact form the task asks for: `socket.create_connection` from a confined process.

Three states, each measured, none inferred:
  1. unrestricted child        -> the connection must SUCCEED (otherwise "denied" proves nothing);
  2. confined child, no allow  -> the connection must be DENIED with EACCES;
  3. confined child, allowed   -> the connection must SUCCEED (otherwise the boundary is a wall).

A local listener serves as the "allowed destination", so the run needs no network egress:
the denial is a kernel decision, not an unreachable host.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(os.environ.get("ACL_REPO", Path(__file__).resolve().parents[1])).resolve()
ESC = '/'  # placeholder to keep the string literal honest in reports

CLIENT = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    s = socket.create_connection((host, port), timeout=3)
    s.close()
    print("CONNECTED")
except PermissionError as e:
    print(f"DENIED(errno={e.errno})")
except ConnectionRefusedError:
    print("REFUSED")
except Exception as e:
    print(f"{type(e).__name__}:{e}")
"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def listener(port: int) -> socket.socket:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    return srv


def main() -> int:
    sys.path.insert(0, str(REPO))
    from hermes_acl import landlock

    kernel = landlock.net_supported()
    print("kernel probe:", json.dumps(kernel, ensure_ascii=False))
    if not kernel.get("supported"):
        print("\nRESULT: network restriction NOT SUPPORTED on this kernel — residual gap")
        return 2

    ok_port, deny_port = free_port(), free_port()
    srv_ok, srv_deny = listener(ok_port), listener(deny_port)
    for s in (srv_ok, srv_deny):
        threading.Thread(target=lambda s=s: s.settimeout(20) and None, daemon=True).start()
    # accept in the background so a successful connect is not left hanging
    stop = threading.Event()

    def accepter(srv: socket.socket) -> None:
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except (TimeoutError, OSError):
                continue

    for srv in (srv_ok, srv_deny):
        threading.Thread(target=accepter, args=(srv,), daemon=True).start()

    script = REPO / "probe" / "_client_create_connection.py"
    script.write_text(CLIENT, encoding="utf-8")

    def run(argv: list[str]) -> str:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        return (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else \
               f"NOOUTPUT rc={r.returncode} {(r.stderr or '')[-200:]}"

    results: dict[str, str] = {}

    # 1. baseline: no shim at all
    results["baseline_allowed"] = run([sys.executable, str(script), "127.0.0.1", str(ok_port)])
    results["baseline_denied"] = run([sys.executable, str(script), "127.0.0.1", str(deny_port)])

    # 2/3. through the project's own policy + shim
    from hermes_acl import policy
    from hermes_acl import spec as spec_mod

    def confined(allow: list[tuple[str, int]]) -> str:  # [(kind, port)] — no host: see D-5
        sp = policy.build_terminal_spec(workspace=str(REPO), allow_net=allow)
        path = policy.write_spec(sp, str(REPO / "probe" / "specs"))
        argv = [*__import__("hermes_acl.native", fromlist=["x"]).shim_argv(path),
                sys.executable, str(script), "127.0.0.1", str(ok_port)]
        return run(argv)

    results["confined_default"] = confined([])
    results["confined_allowed"] = confined([("connect", ok_port)])
    # the ALLOWED port rule must not open a DIFFERENT port
    sp = policy.build_terminal_spec(workspace=str(REPO), allow_net=[("connect", ok_port)])
    path = policy.write_spec(sp, str(REPO / "probe" / "specs"))
    argv = [*__import__("hermes_acl.native", fromlist=["x"]).shim_argv(path),
            sys.executable, str(script), "127.0.0.1", str(deny_port)]
    results["confined_other_port"] = run(argv)

    stop.set()
    for s in (srv_ok, srv_deny):
        s.close()

    for k, v in results.items():
        print(f"{k:24s} = {v}")

    checks = {
        "baseline actually connects (the test can fail)":
            results["baseline_allowed"] == "CONNECTED",
        "baseline connects to the second port too":
            results["baseline_denied"] == "CONNECTED",
        "confined default: DENIED with EACCES":
            results["confined_default"].startswith("DENIED(errno=13)"),
        "confined with an allow rule: CONNECTED":
            results["confined_allowed"] == "CONNECTED",
        "allow rule is port-scoped (other port still DENIED)":
            results["confined_other_port"].startswith("DENIED(errno=13)"),
    }
    print()
    failed = []
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed.append(name)

    out = REPO / "probe" / "verify_network.result.json"
    out.write_text(json.dumps({"kernel": kernel, "results": results, "checks": checks,
                               "failed": failed}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("\nRESULT:", "ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
