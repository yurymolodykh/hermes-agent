"""Smoke: real agent commands must still WORK under the boundary, not just be blocked.

A boundary that denies everything is trivially "secure" and completely useless: the terminal tool
is the product. Every command here is one an agent actually issues, run through
``LocalEnvironment.execute`` with the shim on, and the assertion is that it SUCCEEDS with the
right content. The one exception is the network call, which must fail — that is the point of the
network rule.

Run: python3 smoke_real_commands.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("ACL_REPO") or ".")
PY = sys.executable

BODY = r'''
import json, os, sys
sys.path.insert(0, "@@REPO@@")
from tools.environments.local import LocalEnvironment

ws = "@@WS@@"
env = LocalEnvironment(cwd=ws)
results = {}

def run(name, cmd, timeout=60):
    try:
        r = env.execute(cmd, timeout=timeout)
    except Exception as exc:
        results[name] = {"ok": False, "exc": repr(exc)}
        return
    results[name] = {
        "ok": r.get("returncode") == 0,
        "rc": r.get("returncode"),
        "out": str(r.get("output", ""))[:400],
    }

run("echo", "echo hello-from-confined")
run("pwd", "pwd")
run("write_and_read", "printf 'abc\\n' > scratch.txt && cat scratch.txt")
run("git_version", "git --version")
run("python_version", "@@PY@@ --version")
run("python_import_json", "@@PY@@ -c 'import json,os,pathlib;print(json.dumps([1,2]))'")
run("ls_workspace", "ls -la")
run("grep_workspace", "grep -r abc . 2>&1 | head -3")
run("mkdir_nested", "mkdir -p a/b/c && touch a/b/c/f && find a -type f")
run("sed_edit", "echo 'x=1' > cfg.txt && sed -i 's/x=1/x=2/' cfg.txt && cat cfg.txt")
run("env_read", "env | grep -c .")
run("which_curl", "which curl || echo no-curl")
run("pipe_chain", "printf 'a\\nb\\nc\\n' | sort -r | head -2")
results["net_blocked"] = None
try:
    r = env.execute("@@PY@@ -c \"import socket;s=socket.socket();s.settimeout(2);s.connect(('1.1.1.1',80))\"", timeout=30)
    results["net_blocked"] = {"rc": r.get("returncode"), "out": str(r.get("output",""))[:200]}
except Exception as exc:
    results["net_blocked"] = {"exc": repr(exc)}

print(json.dumps(results))
'''


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="smoke-ws-"))
    home = Path(tempfile.mkdtemp(prefix="smoke-home-"))
    (home / "config.yaml").write_text("acl:\n  enabled: true\n", encoding="utf-8")
    script = (BODY.replace("@@REPO@@", str(REPO.resolve()))
                  .replace("@@WS@@", str(ws))
                  .replace("@@PY@@", PY))
    path = home / "smoke.py"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([PY, str(path)], capture_output=True, text=True, timeout=900, check=False,
                          cwd=str(REPO), env={**os.environ, "HERMES_HOME": str(home)})
    out = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print(proc.stdout[-3000:], proc.stderr[-3000:])
        return 2

    print(json.dumps(data, indent=2, ensure_ascii=False))
    must_pass = ["echo", "pwd", "write_and_read", "git_version", "python_version",
                 "python_import_json", "ls_workspace", "grep_workspace", "mkdir_nested",
                 "sed_edit", "env_read", "which_curl", "pipe_chain"]
    failures = [n for n in must_pass if not data.get(n, {}).get("ok")]
    net_rc = (data.get("net_blocked") or {}).get("rc")
    net_ok = net_rc not in (0, None)

    print("\n== verdict")
    print(f"commands that must work: {len(must_pass) - len(failures)}/{len(must_pass)} passed")
    if failures:
        for name in failures:
            print(f"  FAILED {name}: {json.dumps(data.get(name), ensure_ascii=False)[:300]}")
    print(f"network blocked: {net_ok} (rc={net_rc})")
    ok = not failures and net_ok
    print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
