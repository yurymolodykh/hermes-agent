"""Red proof for tests/hermes_cli/test_status_offload_live.py.

Temporarily puts back the pre-fix body (counters inside the TTL snapshot), runs the test, and
restores the fixed file. The test must FAIL there — otherwise it is not pinning anything.

Usage: python3 .probe-tmp/proof_status_cache.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/hermes/.hermes/hermes-agent/.worktrees/t_6c16a6cb")
TARGET = ROOT / "hermes_cli/web_routers/status.py"
BACKUP = ROOT / ".probe-tmp/status-fixed.py"

FIXED = '''    if signed_out:
        full = {key_: value for key_, value in full.items() if key_ in _WEBAPP_SIGNED_OUT_STATUS_KEYS}
    # The offload counters are LIVE by definition — they describe the pool that is serving right
    # now. Adding them to the cached snapshot would report the state at the last TTL refresh, so a
    # burst shorter than the TTL would read ``queue_depth: 0`` while requests were queued. Merged
    # after the cache instead; nothing else in the payload depends on them.
    return {**full, "handler_offload": _handler_offload_view()}'''

BROKEN = '''    if signed_out:
        return {key_: value for key_, value in full.items() if key_ in _WEBAPP_SIGNED_OUT_STATUS_KEYS}
    return full'''


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    assert FIXED in src, "fixed block not found — did the file change?"
    shutil.copy2(TARGET, BACKUP)
    try:
        TARGET.write_text(src.replace(FIXED, BROKEN), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/hermes_cli/test_status_offload_live.py", "-q"],
            cwd=ROOT, capture_output=True, text=True,
            env={**__import__("os").environ, "HERMES_HOME": "/tmp/t6c-proof"})
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
        print("\n".join(tail))
        print("VERDICT:", "RED as expected (test pins the live-counter merge)"
              if proc.returncode != 0 else "GREEN — the test does NOT pin the defect")
        return 0 if proc.returncode != 0 else 1
    finally:
        shutil.copy2(BACKUP, TARGET)
        print("restored", TARGET)


if __name__ == "__main__":
    raise SystemExit(main())
