"""Landlock capability probe — answers, on THIS kernel, what the policy may rely on.

Nothing here guesses. Every answer is a syscall return code:

* ``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)`` -> ABI (or ``-1/ENOSYS``
  when Landlock is not built in, ``-1/EOPNOTSUPP`` when it is present but disabled).
* ``landlock_create_ruleset(attr{handled_access_net}, ...)`` -> whether the NET rights are
  really handled. This is the probe the plan demands for the network boundary: a kernel can
  report ABI >= 4 and still refuse the net mask if the LSM is compiled without it.

The constants are spelled out rather than imported from libc headers so the module works on a
system without kernel headers, and a version skew cannot silently shift a bit.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from typing import Any, Optional

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446

LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2

#: Filesystem rights, by ABI they first appeared in.
FS_RIGHTS: dict[str, tuple[int, int]] = {
    "execute": (1 << 0, 1),
    "write_file": (1 << 1, 1),
    "read_file": (1 << 2, 1),
    "read_dir": (1 << 3, 1),
    "remove_file": (1 << 4, 1),
    "remove_dir": (1 << 5, 1),
    "make_reg": (1 << 6, 1),
    "make_dir": (1 << 7, 1),
    "make_sym": (1 << 8, 1),
    "make_char": (1 << 9, 1),
    "make_block": (1 << 11, 1),
    "make_sock": (1 << 12, 1),
    "refer": (1 << 13, 2),
    "truncate": (1 << 14, 3),
    "ioctl_dev": (1 << 15, 5),
}

#: Network rights, by ABI they first appeared in.
NET_RIGHTS: dict[str, tuple[int, int]] = {
    "bind_tcp": (1 << 0, 4),
    "connect_tcp": (1 << 1, 4),
    "bind_udp": (1 << 2, 7),
    "connect_send_udp": (1 << 3, 7),
}

PR_SET_NO_NEW_PRIVS = 38


class _PathBeneath(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _NetPort(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def _libc() -> Optional[ctypes.CDLL]:
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:  # Windows / musl with no discoverable libc
        return None


def abi_version() -> int:
    """The Landlock ABI the running kernel offers; ``0`` when Landlock is unusable."""
    libc = _libc()
    if libc is None or not hasattr(libc, "syscall"):
        return 0
    ctypes.set_errno(0)
    try:
        abi = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION)
    except Exception:
        return 0
    return int(abi) if abi > 0 else 0


def net_supported() -> dict[str, Any]:
    """Probe the NET rights by asking the kernel for a ruleset that handles them.

    Returns ``{"abi": int, "supported": bool, "rights": [...], "errno": int}``. The syscall must
    succeed AND the returned descriptor must be a real fd: a kernel that ignores
    ``handled_access_net`` (silently treating the struct as short) would answer ``EINVAL``, not
    success, which is exactly the distinction that has to be tested rather than assumed.

    The probe covers TCP **and** UDP: a policy that handles only ``connect_tcp`` leaves
    ``sendto`` to any host as a one-packet exfiltration channel (measured: a confined child sent
    a datagram to a listening socket while its TCP connect was refused).
    """
    abi = abi_version()
    mask = sum(bit for _n, (bit, since) in NET_RIGHTS.items() if since <= abi)
    out: dict[str, Any] = {"abi": abi, "supported": False, "rights": [], "errno": 0}
    if abi < 4:
        out["errno"] = 95 if abi else 38  # EOPNOTSUPP / ENOSYS, for a human reading the report
        out["reason"] = f"ABI {abi} < 4: no network rights in this kernel"
        return out
    libc = _libc()
    if libc is None or not hasattr(libc, "syscall"):
        out["reason"] = "no libc syscall entry"
        return out
    # struct landlock_ruleset_attr { __u64 handled_access_fs; __u64 handled_access_net; ... }
    attr = struct.pack("=Q", 0) + struct.pack("=Q", mask)
    buf = ctypes.create_string_buffer(attr, len(attr))
    ctypes.set_errno(0)
    fd = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, buf, len(attr), 0)
    err = ctypes.get_errno()
    if fd >= 0:
        os.close(fd)
        out.update(supported=True,
                   rights=[n for n, (_b, since) in NET_RIGHTS.items() if since <= abi],
                   reason="handled_access_net accepted")
    else:
        out["errno"] = err
        out["reason"] = f"landlock_create_ruleset(handled_access_net) failed errno={err}"
    return out


def summary() -> dict[str, Any]:
    """Everything a report or a log line needs, in one call."""
    net = net_supported()
    return {
        "abi": net["abi"],
        "fs": sorted(name for name, (_bit, since) in FS_RIGHTS.items() if since <= net["abi"]),
        "net": {k: net[k] for k in ("supported", "rights", "errno", "reason")},
    }
