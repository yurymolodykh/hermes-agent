"""The exec shim: apply a Landlock policy, then become the target command.

Invoked as a child, never imported into the gateway:
``python -m hermes_acl.shim --spec <spec.txt> -- <argv...>``

Order matters and is the whole guarantee:

1. load the spec (a JSON object produced by the caller; invalid spec -> refuse);
2. ``prctl(PR_SET_NO_NEW_PRIVS)`` — a setuid binary in the child cannot escape;
3. ``landlock_create_ruleset`` with the filesystem rights AND (when the kernel supports them)
   the network rights;
4. one ``landlock_add_rule`` per allowed path, plus one per allowed TCP port;
5. ``landlock_restrict_self``;
6. ``execvpe``.

Any failure exits ``126`` BEFORE ``execve``: a shim that cannot confine does not run the command
"without a boundary" — it refuses. That is the difference between a boundary and a decoration.

Network default is DENY: the ruleset handles ``connect_tcp``/``bind_tcp`` and, with no
``net.allow`` entries, every TCP connect and bind in the child is refused with ``EACCES``. A
destination is opened by PORT only — Landlock has no per-host rule, so an allowed port is open to
every host that port reaches (named in the plan's residual-gap section).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

from hermes_acl import landlock as ll

EXIT_REFUSED = 126
EXIT_USAGE = 127

#: Rights granted to an entry whose spec says ``"rw"`` / ``"r"`` / explicit list.
_PERM_BITS = {
    "execute": ll.FS_RIGHTS["execute"][0],
    "read": ll.FS_RIGHTS["read_file"][0] | ll.FS_RIGHTS["read_dir"][0],
    "read_file": ll.FS_RIGHTS["read_file"][0],
    "read_dir": ll.FS_RIGHTS["read_dir"][0],
    "write": (ll.FS_RIGHTS["write_file"][0] | ll.FS_RIGHTS["truncate"][0]
              | ll.FS_RIGHTS["make_reg"][0] | ll.FS_RIGHTS["make_dir"][0]
              | ll.FS_RIGHTS["make_sym"][0] | ll.FS_RIGHTS["remove_file"][0]
              | ll.FS_RIGHTS["remove_dir"][0]),
}


class ShimError(RuntimeError):
    """A refusal with an operator-readable reason; the caller exits 126."""


def _handled_fs_mask(abi: int) -> int:
    """Every filesystem right this ABI understands, so nothing is left unhandled by omission."""
    mask = 0
    for _name, (bit, since) in ll.FS_RIGHTS.items():
        if since <= abi:
            mask |= bit
    return mask


def _access_bits(perm: Any, abi: int) -> int:
    """Spec permission (``"r"``/``"rw"`` or a list of names) -> Landlock access bits."""
    if isinstance(perm, str):
        text = perm.strip().lower()
        bits = 0
        if "r" in text:
            bits |= _PERM_BITS["read"]
        if "w" in text:
            bits |= _PERM_BITS["write"]
        if "x" in text:
            bits |= _PERM_BITS["execute"]
        names = []
    elif isinstance(perm, (list, tuple)):
        names = [str(n).strip().lower() for n in perm]
        bits = 0
    else:
        raise ShimError(f"unsupported permission {perm!r}")
    for name in names:
        if name not in _PERM_BITS:
            raise ShimError(f"unknown permission name {name!r}")
        bits |= _PERM_BITS[name]
    # Drop rights this ABI cannot express instead of failing the whole ruleset.
    allowed = 0
    for _n, (bit, since) in ll.FS_RIGHTS.items():
        if since <= abi and bits & bit:
            allowed |= bit
    return allowed


def load_spec(path: str) -> dict[str, Any]:
    """Read the shared line-based spec (``hermes_acl/spec.py``) — the SAME bytes the C shim reads."""
    from hermes_acl.spec import SpecError, parse

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ShimError(f"spec {path}: {exc}") from exc
    try:
        spec = parse(text)
    except SpecError as exc:
        raise ShimError(f"spec {path}: {exc}") from exc
    return {
        "allow": [{"path": r.path, "perms": r.perms} for r in spec.fs],
        "net": {
            "enable": spec.net_mode != "off",
            "deny_all": not spec.net,
            "required": spec.net_required,
            "allow": [{"kind": r.kind, "port": r.port} for r in spec.net],
        },
    }


def _create_ruleset(libc: ctypes.CDLL, fs_mask: int, net_mask: int) -> int:
    """Create the ruleset; with a net mask it must be ACCEPTED, not silently ignored."""
    if net_mask:
        attr = _packed(fs_mask, net_mask)
    else:
        attr = _packed(fs_mask)
    buf = ctypes.create_string_buffer(attr, len(attr))
    ctypes.set_errno(0)
    fd = libc.syscall(ll.SYS_LANDLOCK_CREATE_RULESET, buf, len(attr), 0)
    err = ctypes.get_errno()
    if fd < 0:
        what = "handled_access_net" if net_mask else "handled_access_fs"
        raise ShimError(f"landlock_create_ruleset({what}) failed errno={err}")
    return int(fd)


def _packed(*values: int) -> bytes:
    return b"".join(int(v).to_bytes(8, "little", signed=False) for v in values)


def _add_path(libc: ctypes.CDLL, ruleset_fd: int, path: str, access: int) -> None:
    if not os.path.exists(path):
        # A path that is not there grants nothing; refusing the whole child would make every
        # policy brittle (HOME subdirs come and go). The boundary is unaffected: absent = denied.
        return
    try:
        pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise ShimError(f"open {path}: {exc}") from exc
    try:
        rule = ll._PathBeneath(access, pfd)
        ctypes.set_errno(0)
        rc = libc.syscall(ll.SYS_LANDLOCK_ADD_RULE, ruleset_fd, ll.LANDLOCK_RULE_PATH_BENEATH,
                          ctypes.byref(rule), 0)
        if rc != 0:
            raise ShimError(f"landlock_add_rule({path}) failed errno={ctypes.get_errno()}")
    finally:
        os.close(pfd)


def _add_net_port(libc: ctypes.CDLL, ruleset_fd: int, port: int, access: int) -> None:
    rule = ll._NetPort(access, port)
    ctypes.set_errno(0)
    rc = libc.syscall(ll.SYS_LANDLOCK_ADD_RULE, ruleset_fd, ll.LANDLOCK_RULE_NET_PORT,
                      ctypes.byref(rule), 0)
    if rc != 0:
        raise ShimError(f"landlock_add_rule(net port {port}) failed errno={ctypes.get_errno()}")


def _net_mask(abi: int) -> int:
    """Every network right this ABI understands: TCP **and** UDP.

    Handling only the TCP rights leaves UDP as a live exfiltration path — measured on this
    kernel: a confined child's TCP connect was refused (EACCES) while ``sendto`` delivered a
    datagram to a listening socket. UDP rights exist from ABI 7; below that there is nothing to
    handle and the residual gap is the one the plan already names.
    """
    mask = 0
    for _name, (bit, since) in ll.NET_RIGHTS.items():
        if since <= abi:
            mask |= bit
    return mask


def _net_port_bits(kind: str, abi: int) -> int:
    """Access bits for one allowed destination kind, matched to what this ABI can express."""
    bit, since = ll.NET_RIGHTS[kind]
    return bit if since <= abi else 0


def _net_ports(spec: dict[str, Any]) -> list[tuple[int, int]]:
    """``[(port, access_bits)]`` for ``spec.net.allow``; ``port 0`` means "any port"."""
    net = spec.get("net") or {}
    abi = ll.abi_version()
    if net.get("deny_all", True):
        pass  # the mask is handled with an empty allow list -> nothing leaves the process
    out: list[tuple[int, int]] = []
    for entry in net.get("allow") or []:
        if not isinstance(entry, dict):
            raise ShimError("net.allow entries must be objects with a 'port'")
        try:
            port = int(entry.get("port", -1))
        except (TypeError, ValueError) as exc:
            raise ShimError(f"net.allow port {entry.get('port')!r} is not an integer") from exc
        if not 0 <= port <= 65535:
            raise ShimError(f"net.allow port {port} out of range")
        kinds = entry.get("kinds") or ["connect"]
        bits = 0
        for kind in kinds:
            names = {
                "connect": ("connect_tcp", "connect_send_udp"),
                "bind": ("bind_tcp", "bind_udp"),
            }.get(kind)
            if names is None:
                raise ShimError(f"unknown net kind {kind!r}")
            for name in names:
                bits |= _net_port_bits(name, abi)
        out.append((port, bits))
    return out


def apply(spec: dict[str, Any]) -> dict[str, Any]:
    """Confine THIS process. Returns a report; raises :class:`ShimError` on any refusal."""
    if not sys.platform.startswith("linux"):
        raise ShimError("Landlock is Linux-only; refusing to run without a boundary")
    libc = ll._libc()
    if libc is None or not hasattr(libc, "syscall"):
        raise ShimError("no libc syscall entry: cannot confine")
    abi = ll.abi_version()
    if abi < 1:
        raise ShimError("this kernel has no Landlock: refusing to run without a boundary")

    fs_mask = _handled_fs_mask(abi)
    wanted_net = bool((spec.get("net") or {}).get("enable", True))
    net_mask = 0
    if wanted_net:
        cap = ll.net_supported()
        if cap["supported"]:
            net_mask = _net_mask(abi)
        elif (spec.get("net") or {}).get("required"):
            raise ShimError(f"network boundary required but unsupported: {cap['reason']}")

    ruleset_fd = _create_ruleset(libc, fs_mask, net_mask)
    try:
        for entry in spec.get("allow") or []:
            if not isinstance(entry, dict) or not entry.get("path"):
                raise ShimError("allow entries must be objects with a 'path'")
            access = _access_bits(entry.get("perms", "r"), abi) & fs_mask
            if not access:
                continue
            _add_path(libc, ruleset_fd, str(entry["path"]), access)
        for port, bits in _net_ports(spec):
            if net_mask:
                _add_net_port(libc, ruleset_fd, port, bits)

        ctypes.set_errno(0)
        if libc.prctl(ll.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise ShimError(f"prctl(PR_SET_NO_NEW_PRIVS) failed errno={ctypes.get_errno()}")
        ctypes.set_errno(0)
        if libc.syscall(ll.SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise ShimError(f"landlock_restrict_self failed errno={ctypes.get_errno()}")
    finally:
        os.close(ruleset_fd)
    return {
        "abi": abi,
        "fs_handled": fs_mask,
        "net_enabled": bool(net_mask),
        "net_ports": [p for p, _b in _net_ports(spec)] if net_mask else [],
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("--spec",) or "--" not in args:
        sys.stderr.write("usage: python -m hermes_acl.shim --spec <spec.txt> -- <argv...>\n")
        return EXIT_USAGE
    spec_path = args[1]
    sep = args.index("--")
    command = args[sep + 1:]
    if not command:
        sys.stderr.write("hermes_acl.shim: no command after --\n")
        return EXIT_USAGE
    try:
        spec = load_spec(spec_path)
        report = apply(spec)
    except ShimError as exc:
        sys.stderr.write(f"hermes_acl.shim: refused: {exc}\n")
        return EXIT_REFUSED
    digest = os.environ.pop("HERMES_ACL_SHIM_REPORT", "")
    if digest:
        try:
            with open(digest, "w", encoding="utf-8") as fh:
                json.dump(report, fh)
        except OSError:
            pass
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        sys.stderr.write(f"hermes_acl.shim: exec {command[0]}: {exc}\n")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
