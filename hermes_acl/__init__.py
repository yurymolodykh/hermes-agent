"""Kernel-enforced boundary for spawned children: Landlock (filesystem + network).

One rule, one place: a child is confined by the kernel before ``execve``, so ``bash -c``,
interpreters, ``find -exec`` and everything they spawn inherit the boundary and no command
string has to be parsed (plan §5.4).

The network half exists because a filesystem boundary alone is not a boundary for personal
data: whatever a child can read, it can also POST somewhere. ``LANDLOCK_ACCESS_NET_CONNECT_TCP``
(ABI 4+) closes that, but only per PORT — the kernel has no per-destination rule, so an allowed
port is open to every host (stated as a residual gap in the plan, not papered over).

Import-light on purpose: this package is imported from hot paths (argv construction) and from a
child that is about to be confined.
"""

from __future__ import annotations

__all__ = ["netpolicy", "shim"]

#: Bump when the policy/report shape changes and a consumer must re-read it.
SHIM_REPORT_VERSION = 1
