"""Adversarial check: can a confined command read ANOTHER subject's memory bucket?

The per-subject memory dir sits under HERMES_HOME, which the terminal child has no business
reading. This is checked by execution, because a policy that merely looks right is how the
earlier defect ("grant on a parent made confinement decorative") survived a green test run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("ACL_REPO", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(REPO))

FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail != "" else ""))
    if not ok:
        FAILED.append(name)


def main() -> int:
    from hermes_acl import native, policy

    root = Path(tempfile.mkdtemp(prefix="adv-boundary-"))
    home = root / "home"
    (home / "memories" / "u" / "tg_bob").mkdir(parents=True)
    secret = home / "memories" / "u" / "tg_bob" / "MEMORY.md"
    secret.write_text("B-SECRET: bob's private calibration constant\n", encoding="utf-8")
    (home / "config.yaml").write_text("acl:\n  enabled: true\n", encoding="utf-8")

    workspace = root / "ws"
    workspace.mkdir()
    (workspace / "allowed.txt").write_text("PUBLIC-DATA\n", encoding="utf-8")

    os.environ["HERMES_HOME"] = str(home)

    sp = policy.build_terminal_spec(workspace=str(workspace))
    spec_path = policy.write_spec(sp, str(root))
    shim = native.shim_argv(spec_path)

    def run(cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run([*shim, "/bin/bash", "-c", cmd], capture_output=True,
                              text=True, timeout=90, check=False)

    # 1. the workspace file is readable (grounding: the child works at all)
    r = run(f"cat {workspace}/allowed.txt")
    check("confined child reads its workspace file", "PUBLIC-DATA" in r.stdout,
          r.stdout.strip()[:60])

    # 2. a sibling subject's memory is NOT readable, and the content never appears in stdout
    r = run(f"cat {secret}")
    leaked = "B-SECRET" in r.stdout
    check("confined child CANNOT read another subject's memory", not leaked,
          f"stdout={r.stdout.strip()[:60]!r} rc={r.returncode}")
    check("the refusal is the kernel's, not a missing file", r.returncode != 0,
          f"rc={r.returncode}")

    # 3. the whole memories tree is unlistable
    r = run(f"ls {home}/memories/u/")
    check("confined child cannot list the subject buckets", "tg_bob" not in r.stdout,
          f"stdout={r.stdout.strip()[:80]!r}")

    # 4. a python one-liner (not just cat) cannot read it either — the boundary is the process
    r = run(f"{sys.executable} -c \"print(open('{secret}').read())\"")
    check("confined python cannot read it either", "B-SECRET" not in r.stdout,
          f"rc={r.returncode} err={(r.stderr or '')[-80:]!r}")

    # 5. exfiltration is cut: with the file unreadable, the network is also closed.
    #    Assert EACCES specifically — a generic non-zero exit could just be a missing binary.
    netcall = (f"{sys.executable} -c \"import socket,sys\n"
               f"try:\n socket.create_connection(('1.1.1.1',80),timeout=2)\n"
               f" print('CONNECTED')\n"
               f"except PermissionError as e:\n print('DENIED',e.errno)\n"
               f"except Exception as e:\n print('OTHER',type(e).__name__)\n\"")
    r = run(netcall)
    check("confined child has no network: denied with EACCES (13)",
          "DENIED 13" in r.stdout, f"stdout={r.stdout.strip()[:80]!r} rc={r.returncode}")

    # 6. the spec itself never contains '/'
    body = Path(spec_path).read_text(encoding="utf-8")
    root_grants = [ln for ln in body.splitlines()
                   if ln.strip().endswith(" /") or ln.strip().split()[-1:] == ["/"]]
    check("no grant names / as a root", not root_grants, root_grants[:3])

    print()
    (REPO / "probe" / "verify_boundary.result.json").write_text(
        json.dumps({"failed": FAILED}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT:", "ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
