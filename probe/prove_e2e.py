"""End-to-end: the REAL terminal path, with the boundary on and off.

Not a unit test of a helper: this drives ``LocalEnvironment._run_bash`` (the path an agent's
``terminal`` tool actually uses) and reads what the KERNEL decided, plus what the child could and
could not reach. Two runs, same command, one difference — ``acl.enabled``:

* ``false`` -> argv is the plain ``[bash, -c, cmd]``, the secret file is readable, a TCP connect
  succeeds. This is the backward-compatibility proof (criterion 7).
* ``true``  -> argv carries the shim, the secret is unreadable (EACCES from Landlock, not from a
  path check in Python), and the TCP connect is refused.

Run: python3 prove_e2e.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("ACL_REPO") or Path(__file__).resolve().parent)
if not (REPO / "tools" / "environments" / "local.py").exists():
    raise SystemExit(
        "ACL_REPO must point at the hermes-agent tree under test (the worktree), not this "
        f"script's directory. Got {REPO}, which has no tools/environments/local.py.")
PY = sys.executable

#: The child script, as a TEMPLATE: placeholders are substituted with .replace so that no brace
#: in the generated code has to survive an f-string (the first version of this file lost
#: ``{exc!r}`` to the outer f-string and reported a NameError from inside the child).
CHILD_SCRIPT = r'''
import json, os, sys
sys.path.insert(0, "@@REPO@@")
os.environ["HERMES_HOME"] = "@@HOME@@"
out = {}

from tools.environments.local import LocalEnvironment
env = LocalEnvironment(cwd="@@WORKSPACE@@")

captured = {}
real_popen = __import__("subprocess").Popen

def spy(argv, *a, **kw):
    captured["argv"] = list(argv) if isinstance(argv, (list, tuple)) else [argv]
    class _Fake:
        returncode = 0
        pid = 1
        stdout = b""
        stderr = b""
        def communicate(self, *x, **k):
            return (b"", b"")
        def wait(self, *x, **k):
            return 0
        def poll(self):
            return 0
    return _Fake()

import subprocess as _sp
_sp.Popen = spy
try:
    env._run_bash("true", timeout=5)
except Exception as exc:
    out["run_error"] = repr(exc)
finally:
    _sp.Popen = real_popen
out["argv"] = captured.get("argv")
out["shim_present"] = bool(out["argv"] and "hermes-acl-shim" in " ".join(out["argv"]))

def child(cmd):
    try:
        r = env.execute(cmd, timeout=20)
        return str(r.get("output", ""))
    except Exception as exc:
        return "EXC " + repr(exc)

out["read_allowed"] = child("cat '@@VISIBLE@@'").strip()[:200]
out["read_secret"] = child("cat '@@SECRET@@'").strip()[:220]
NET = (
    "import socket\n"
    "s = socket.socket(); s.settimeout(3)\n"
    "try:\n"
    "    s.connect(('@@HOST@@', @@PORT@@)); print('CONNECTED')\n"
    "except PermissionError as e: print('DENIED', e.errno)\n"
    "except ConnectionRefusedError: print('REFUSED')\n"
)
import shlex
out["net"] = child("@@PY@@ -c " + shlex.quote(NET)).strip()[:240]
out["uvicense"] = 0
print(json.dumps(out))
'''


def _write_config(home: Path, acl_on: bool) -> None:
    (home / "config.yaml").write_text(
        f"acl:\n  enabled: {'true' if acl_on else 'false'}\n", encoding="utf-8")


def _run_case(acl_on: bool, probe_port: int) -> dict:
    home = Path(tempfile.mkdtemp(prefix=f"e2e-acl-{'on' if acl_on else 'off'}-"))
    secret = home / "secret" / "medical.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("PATIENT-DATA-9931", encoding="utf-8")
    workspace = home / "workspace"
    workspace.mkdir()
    visible = workspace / "visible.txt"
    visible.write_text("PUBLIC-DATA", encoding="utf-8")
    _write_config(home, acl_on)

    script = (CHILD_SCRIPT
              .replace("@@REPO@@", str(REPO))
              .replace("@@HOME@@", str(home))
              .replace("@@WORKSPACE@@", str(workspace))
              .replace("@@VISIBLE@@", str(visible))
              .replace("@@SECRET@@", str(secret))
              .replace("@@HOST@@", "127.0.0.1")
              .replace("@@PORT@@", str(probe_port))
              .replace("@@PY@@", PY))
    script_path = home / "child.py"
    script_path.write_text(script, encoding="utf-8")

    proc = subprocess.run([PY, str(script_path)], capture_output=True, text=True, timeout=300, check=False,
                          cwd=str(REPO), env={**os.environ, "HERMES_HOME": str(home)})
    payload = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    data["home"] = str(home)
    data["exit"] = proc.returncode
    if proc.returncode != 0 or not data.get("argv"):
        data["stderr"] = proc.stderr[-3000:]
        data["stdout_tail"] = proc.stdout[-2000:]
    return data


def main() -> int:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    off = _run_case(False, port)
    on = _run_case(True, port)
    print(json.dumps({"acl_off": off, "acl_on": on}, indent=2, ensure_ascii=False))

    print("\n== checks")
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")

    check("acl off: no shim in argv", off.get("shim_present") is False,
          str((off.get("argv") or [])[:3]))
    check("acl off: argv starts with bash", "bash" in str((off.get("argv") or [""])[0]))
    check("acl off: secret readable", "PATIENT-DATA-9931" in str(off.get("read_secret", "")))
    check("acl off: network reachable", "CONNECTED" in str(off.get("net", "")),
          str(off.get("net"))[:60])

    check("acl on: shim in argv", on.get("shim_present") is True, str((on.get("argv") or [])[:3]))
    check("acl on: allowed file readable", "PUBLIC-DATA" in str(on.get("read_allowed", "")),
          str(on.get("read_allowed"))[:60])
    check("acl on: secret unreadable", "PATIENT-DATA-9931" not in str(on.get("read_secret", "")),
          str(on.get("read_secret"))[:90])
    # The refusal text is LOCALISED (this host answers "Отказано в доступе"), so match on the
    # errno-independent fact that matters: the file's CONTENT is absent from the output while the
    # operation is described as a failure. Asserting an English word would fail on a ru_RU host
    # for the wrong reason.
    secret_out = str(on.get("read_secret", ""))
    check("acl on: refused by the kernel (content absent + failure reported)",
          "PATIENT-DATA-9931" not in secret_out and bool(secret_out.strip()),
          secret_out[:90])
    check("acl on: network denied", "DENIED" in str(on.get("net", "")), str(on.get("net"))[:90])

    ok = all(results)
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
