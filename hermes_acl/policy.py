"""Build the shim policy for a confined child, and wrap an argv with the shim.

Two things decide the policy:

* ``acl.enabled`` — the one switch of the access model. ``false`` (default) means NO shim:
  ``wrap_argv`` returns the argv untouched, so a single-user install and every CLI/desktop
  surface keep byte-identical behaviour and zero overhead.
* The roots the child legitimately needs — a NAMED LIST of system directories, never ``/``.

That last point is the whole boundary and it is easy to get wrong: Landlock is an allow-list,
so granting ``("/", "rx")`` grants every file on the machine and the confinement becomes a
decoration that still shows "confined" in the report. Measured before the fix: with ``/``
allowed, a confined ``cat`` read ``<HERMES_HOME>/secret/medical.txt`` — exactly the leak the
per-subject memory work exists to stop. The list below is what a shell and an interpreter
actually need, and nothing else is readable.

Network default is DENY. An operator opens destinations by PORT under ``acl.shim.net_allow``;
there is no per-host rule to express (the kernel has none), which the plan names as a residual
gap instead of implying the boundary is finer than it is.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_acl import spec as spec_mod

logger = logging.getLogger(__name__)

#: System directories a shell/interpreter genuinely needs to READ+EXECUTE. Named individually:
#: ``/`` is deliberately absent (see the module docstring), and so is every home directory.
_SYSTEM_READ = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32",
    "/etc", "/opt", "/snap", "/nix",
)

#: Pseudo-filesystems that must be readable or the shell itself fails: ``/dev/null`` for a
#: redirect, ``/proc`` for a pager and for ``ps``. Writable: ``/dev`` and the temp dirs.
_SYSTEM_READ_ANY = ("/proc",)
_SYSTEM_RW = ("/dev", "/tmp", "/var/tmp", "/run/user")

#: Roots that must never be granted ``rw`` wholesale, even when a workspace is nested in them.
_NEVER_RW = frozenset({"/", "/dev", "/proc", "/sys", "/boot", "/etc", "/usr", "/bin", "/sbin",
                       "/lib", "/lib64", "/var", "/root", "/home"})


def acl_enabled() -> bool:
    """``acl.enabled`` from config, default false; a broken config never enables the boundary."""
    try:
        from hermes_acl_scope import acl_enabled as _enabled
        return bool(_enabled())
    except Exception:
        logger.debug("acl policy: config unavailable", exc_info=True)
        return False


def net_allow() -> list[tuple[str, int]]:
    """``acl.shim.net_allow`` as ``[(kind, port)]``; malformed entries are dropped with a log."""
    try:
        from hermes_cli.config import cfg_get, load_config
        rows = cfg_get(load_config(), "acl", "shim", "net_allow", default=None)
    except Exception:
        return []
    out: list[tuple[str, int]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            logger.warning("acl shim: net_allow entry %r is not an object; ignored", row)
            continue
        kind = str(row.get("kind", "connect")).strip().lower()
        try:
            port = int(row.get("port", -1))
        except (TypeError, ValueError):
            logger.warning("acl shim: net_allow port %r is not an integer; ignored", row.get("port"))
            continue
        if kind not in ("connect", "bind") or not 0 <= port <= 65535:
            logger.warning("acl shim: net_allow entry %r is out of range; ignored", row)
            continue
        out.append((kind, port))
    return out


def _code_roots() -> list[str]:
    """The interpreter prefix and the repo it runs — the child cannot import without them."""
    roots: list[str] = []
    for candidate in (sys.prefix, sys.base_prefix, getattr(sys, "exec_prefix", ""),
                      str(Path(__file__).resolve().parent.parent)):
        if not candidate:
            continue
        try:
            real = os.path.realpath(str(candidate))
        except OSError:
            continue
        # Guard against a home-directory prefix: granting ``/home/hermes`` would hand the child
        # every other profile's files, which is the leak this policy exists to prevent.
        if real.startswith("/") and real not in _NEVER_RW and real not in roots:
            roots.append(real)
    return roots


def _inside(path: str, parent: str) -> bool:
    """Whether *path* is *parent* or lives under it (both already real-pathed)."""
    path = path.rstrip("/") or "/"
    parent = parent.rstrip("/") or "/"
    return path == parent or path.startswith(parent + "/")


def _writable_roots(workspace: Optional[str], extra: Iterable[str] = ()) -> list[str]:
    """The ``rw`` roots for one child: its workspace, its temp dir, and any explicit extra.

    The shared temp dir is granted **unless the memory/state home lives inside it**. Landlock
    grants recursively, so ``rw`` on a temp root hands the child every file underneath — and a
    deployment that puts ``HERMES_HOME`` under ``TMPDIR`` would then expose ``memories/u/<other>``
    through the terminal, defeating the per-subject split at its strongest point. When that
    overlap exists the temp grant is dropped and the child keeps only its workspace: a command
    that needs scratch space outside it fails loudly instead of silently reading a neighbour.
    """
    roots: list[str] = []
    candidates = [workspace, *extra, tempfile.gettempdir()]
    try:
        from hermes_constants import get_hermes_home
        home = os.path.realpath(str(get_hermes_home()))
    except Exception:
        home = ""
    workspace_real = os.path.realpath(str(workspace)) if workspace else ""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            real = os.path.realpath(str(candidate))
        except OSError:
            continue
        if not real.startswith("/") or real in _NEVER_RW:
            continue
        if home and real != workspace_real and _inside(home, real):
            # This root CONTAINS the memory home, so granting it recursively would expose
            # ``memories/u/<other>`` through the terminal. The workspace is the one exception:
            # confining a command to the directory it was told to work in is the point, and an
            # operator who put a workspace inside HERMES_HOME made that choice about their files.
            logger.warning(
                "acl shim: HERMES_HOME %s is inside the writable root %s; that root is NOT "
                "granted recursively (it would expose every profile's files). Commands that "
                "need scratch space should use the workspace.", home, real)
            continue
        if real not in roots:
            roots.append(real)
    return roots


def _iter_ancestors(path: str) -> Iterable[str]:
    """``/a/b/c`` -> ``/a/b/c``, ``/a/b``, ``/a`` — the directories whose traversal is needed."""
    current = path.rstrip("/")
    while current and current != "/":
        yield current
        current = os.path.dirname(current)


def build_terminal_spec(*, workspace: Optional[str] = None, extra_rw: Iterable[str] = (),
                        allow_net: Optional[Iterable[tuple[str, int]]] = None,
                        net_required: bool = False) -> spec_mod.Spec:
    """The policy for a terminal/background/`rg` child.

    ``net_required=False`` by default: refusing to run every command on a kernel without the
    network rights would take the terminal away entirely. The shim then reports ``net_enabled:
    false`` in its report and the enclosure is filesystem-only — a state the operator can see,
    not a state the system pretends about.
    """
    fs: list[tuple[str, str]] = []
    for path in _SYSTEM_READ:
        if os.path.exists(path):
            fs.append((path, "rx"))
    for path in _SYSTEM_READ_ANY:
        if os.path.exists(path):
            fs.append((path, "r"))
    for path in _SYSTEM_RW:
        if os.path.isdir(path):
            fs.append((path, "rw"))
    # The interpreter and the repo: read+execute only. A ``rw`` here would let a command
    # rewrite the very code that builds the boundary.
    for path in _code_roots():
        fs.append((path, "rx"))

    rw_roots = _writable_roots(workspace, extra_rw)
    for path in rw_roots:
        # ``rw`` on the workspace implies directory TRAVERSAL of its parents, which
        # ``path_beneath`` does NOT grant: without it a child cannot stat its way to the
        # workspace and every command fails with ENOENT on its own cwd. The ancestors get
        # ``x`` ONLY — traversal, not listing and not reading. Granting ``r`` here would hand
        # the child every file in every parent directory, which is how a "confined" command
        # still read a neighbouring profile's memory (measured before the fix).
        for ancestor in _iter_ancestors(os.path.dirname(path) or "/"):
            if ancestor not in ("/", *[p for p, _ in fs]):
                fs.append((ancestor, "x"))
        fs.append((path, "rw"))

    net = list(allow_net) if allow_net is not None else net_allow()
    # De-duplicate while keeping the FIRST permission seen for a path: ``rw`` from the workspace
    # must not be downgraded by a later ``rx`` for the same directory.
    seen: dict[str, str] = {}
    for path, perms in fs:
        if path not in seen or "w" in perms and "w" not in seen[path]:
            seen[path] = perms
    return spec_mod.build([(p, perm) for p, perm in seen.items()],
                          net_mode="deny", net_required=net_required, net=net)


def write_spec(spec: spec_mod.Spec, directory: Optional[str] = None) -> str:
    """Write the spec to a per-call file; the shim reads it before execve."""
    if directory:
        target = Path(directory) / "acl-spec.txt"
    else:
        fd, name = tempfile.mkstemp(prefix="hermes-acl-spec-", suffix=".txt")
        os.close(fd)
        target = Path(name)
    spec_mod.write(spec, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return str(target)


def wrap_argv(argv: list[str], *, workspace: Optional[str] = None,
              extra_rw: Iterable[str] = (), spec_dir: Optional[str] = None) -> list[str]:
    """Prepend the shim to *argv*, or return it untouched when ``acl.enabled=false``.

    Called from the argv builders of the terminal paths. Returning the SAME list object when the
    boundary is off is deliberate: callers compare argv for equality (tests, systemd wrappers),
    and "no change" has to be literally no change.
    """
    if not acl_enabled():
        return argv
    try:
        from hermes_acl import native
        spec = build_terminal_spec(workspace=workspace, extra_rw=extra_rw)
        spec_path = write_spec(spec, spec_dir)
        return [*native.shim_argv(spec_path), *argv]
    except Exception:
        # Fail CLOSED only where the operator asked for the boundary; a policy that cannot be
        # built must not silently run the command unconfined.
        logger.exception("acl shim: could not build the policy; refusing to run unconfined")
        raise


def describe() -> dict[str, Any]:
    """Report facts: switch state, policy shape, shim implementation, kernel capability."""
    from hermes_acl import landlock, native
    return {
        "enabled": acl_enabled(),
        "net_allow": net_allow(),
        "impl": native.shim_impl() if acl_enabled() else None,
        "kernel": landlock.summary(),
    }
