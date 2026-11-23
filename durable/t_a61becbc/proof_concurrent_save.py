"""Red-proof for tests/hermes_cli/test_fs_write_text_offload.py.

Runs the SAME burst as the regression test against the PRE-FIX staging name
(`.<name>.hermes-tmp-<pid>`, a constant for the process) to show the test would have failed on
the base revision — i.e. the defect was real and the fix is what makes the test green.

Usage: python3 .probe-tmp/proof_concurrent_save.py
"""
from __future__ import annotations

import os
import tempfile
import threading
import traceback
from pathlib import Path


def save_old(target: Path, text: str) -> dict:
    tmp = target.with_name(f".{target.name}.hermes-tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Could not write file: {exc}") from exc
    return {"ok": True, "path": str(target), "byteSize": len(text.encode("utf-8"))}


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="proof-save-"))
    target = workdir / "note.txt"
    target.write_text("original", encoding="utf-8")
    payloads = [f"payload-{i}-" + "x" * 20000 for i in range(8)]

    outcomes: list[object] = []
    barrier = threading.Barrier(len(payloads))

    def worker(text: str) -> None:
        barrier.wait()
        try:
            outcomes.append(save_old(target, text))
        except Exception as exc:  # noqa: BLE001 - the failure IS the observation
            outcomes.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    failures = [o for o in outcomes if isinstance(o, Exception)]
    ok = [o for o in outcomes if not isinstance(o, Exception)]
    body = target.read_text(encoding="utf-8") if target.exists() else "<MISSING>"
    leftovers = [p.name for p in workdir.iterdir() if "hermes-tmp" in p.name]

    print(f"ok={len(ok)} failed={len(failures)}")
    for exc in failures[:5]:
        print("  failure:", exc)
    print(f"target exists={target.exists()} size={0 if body == '<MISSING>' else len(body)}")
    print(f"content is one payload in full: {body in payloads}")
    print(f"leftover staging files: {leftovers}")
    # what the regression test asserts
    red = bool(failures) or (body not in payloads and body != "original")
    print("VERDICT:", "RED on the pre-fix code (defect reproduced)" if red else "GREEN (no defect)")
    return 0 if red else 1


if __name__ == "__main__":
    raise SystemExit(main())
