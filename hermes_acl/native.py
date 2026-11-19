"""Locate (or build once) the native shim; fall back to the Python one, and say so.

The native binary is the one on the hot path: a Python interpreter costs ~60 ms before it can
confine anything, measured on this host, which is more than most terminal calls take. The build
happens once into ``<HERMES_HOME>/cache/acl/`` keyed by a hash of the source, so a source change
rebuilds and a second process reuses it.

If no compiler is available the Python fallback is used and the caller is told (a log line, not
a silent 60 ms regression). ``ACL_SHIM_IMPL=python|native`` pins the choice for tests.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NATIVE_SOURCE = Path(__file__).resolve().parent / "native" / "shim.c"
_BINARY_NAME = "hermes-acl-shim"
_build_lock = threading.Lock()
_cached: Optional[str] = None
_warned = False


def _cache_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "cache" / "acl"


def _source_hash() -> str:
    try:
        return hashlib.sha256(NATIVE_SOURCE.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def build_native(*, force: bool = False) -> Optional[str]:
    """Compile the shim into the cache dir; ``None`` when no working compiler exists."""
    if not sys.platform.startswith("linux"):
        return None
    if force:
        _write_report_line("build forced")
    cc = shutil.which(os.getenv("CC") or "cc") or shutil.which("gcc")
    if not cc:
        return None
    out = _cache_dir() / f"{_BINARY_NAME}-{_source_hash()}"
    if out.exists() and os.access(out, os.X_OK):
        return str(out)
    _cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([cc, "-O2", "-o", str(out), str(NATIVE_SOURCE)],
                              capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("acl shim: compiler failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning("acl shim: native build failed, using the Python fallback: %s",
                       (proc.stderr or "").strip()[:400])
        return None
    return str(out)


def _write_report_line(text: str) -> None:
    logger.debug("acl shim: %s", text)


def shim_impl() -> str:
    """``"native"`` when the C shim is usable, else ``"python"`` (honest, not optimistic)."""
    global _cached, _warned
    forced = (os.getenv("ACL_SHIM_IMPL") or "").strip().lower()
    if forced == "python":
        return "python"
    if _cached is None:
        with _build_lock:
            if _cached is None:
                _cached = build_native() or ""
    if _cached:
        return "native"
    if forced == "native":
        raise RuntimeError("ACL_SHIM_IMPL=native but no usable compiler for the C shim")
    if not _warned:
        _warned = True
        logger.warning(
            "acl shim: no C compiler — the Python shim costs ~60 ms per confined command. "
            "Install a compiler (cc/gcc) or set ACL_SHIM_IMPL=python to silence this.")
    return "python"


def shim_argv(spec_path: str | Path) -> list[str]:
    """argv prefix that runs *spec_path*'s policy and then the command (append ``-- cmd``)."""
    if shim_impl() == "native":
        return [str(build_native() or ""), "--spec", str(spec_path), "--"]
    repo_root = str(Path(__file__).resolve().parent.parent)
    env_python = sys.executable
    if repo_root not in (os.getenv("PYTHONPATH") or ""):
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [repo_root, os.getenv("PYTHONPATH")]))
    return [env_python, "-m", "hermes_acl.shim", "--spec", str(spec_path), "--"]


def describe() -> dict[str, object]:
    """Facts for a report: which implementation, which binary, whether net is enforceable."""
    from hermes_acl import landlock
    return {
        "impl": shim_impl(),
        "binary": _cached or None,
        "source": str(NATIVE_SOURCE),
        "compile_runnable": bool(shutil.which(os.getenv("CC") or "cc") or shutil.which("gcc")),
        "landlock": landlock.summary(),
    }
