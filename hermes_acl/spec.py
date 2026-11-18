"""The policy spec: ONE line-based format, read by both shim implementations.

A JSON spec forced the C shim to carry a JSON parser for data that is a flat list of
(path, perms) and (port, kinds) pairs. The format below is what the kernel call sequence
actually needs, so the C shim (the one on the hot path: a Python interpreter costs tens of
milliseconds before it can confine anything) and the Python fallback parse the very same bytes —
one artifact, two readers, no second source of truth to drift.

    # comment
    version 1
    fs  <perms> <path>        perms: r (read file+dir), w (write), x (execute); combine: rwx
    net deny                  handle the network rights, allow nothing (the default)
    net allow connect <port>  open one TCP/UDP destination port (0 = any port)
    net allow bind <port>
    net off                   do not handle network rights at all (explicit opt-out)
    net required              refuse to run if the kernel cannot enforce the network boundary

The writer refuses anything it cannot express, instead of emitting a spec the C shim would
misread: a path containing a newline or a port that is not an integer is a build-time error.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, NamedTuple

SPEC_VERSION = 1


class SpecError(ValueError):
    """A policy that cannot be expressed in the format — refuse, never guess."""


class FsRule(NamedTuple):
    path: str
    perms: str


class NetRule(NamedTuple):
    kind: str  # "connect" | "bind"
    port: int


class Spec(NamedTuple):
    fs: tuple[FsRule, ...]
    net_mode: str  # "deny" | "off"
    net_required: bool
    net: tuple[NetRule, ...]

    def render(self) -> str:
        lines = [f"version {SPEC_VERSION}"]
        for rule in self.fs:
            lines.append(f"fs {rule.perms} {rule.path}")
        if self.net_mode == "off":
            lines.append("net off")
        else:
            lines.append("net deny")
            for rule in self.net:
                lines.append(f"net allow {rule.kind} {rule.port}")
        if self.net_required:
            lines.append("net required")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SPEC_VERSION,
            "fs": [{"path": r.path, "perms": r.perms} for r in self.fs],
            "net": {"mode": self.net_mode, "required": self.net_required,
                    "allow": [{"kind": r.kind, "port": r.port} for r in self.net]},
        }


def _clean_perms(perms: Any) -> str:
    text = "".join(sorted(set(str(perms or "").strip().lower())))
    bad = set(text) - set("rwx")
    if bad:
        raise SpecError(f"unknown permission letters {''.join(sorted(bad))!r} in {perms!r}")
    return text


def _clean_path(path: Any) -> str:
    text = str(path or "")
    if not text:
        raise SpecError("empty path in a spec rule")
    if "\n" in text or "\r" in text:
        raise SpecError("a path may not contain a newline")
    if not os.path.isabs(text):
        raise SpecError(f"path {text!r} is not absolute")
    return text


def build(fs: Iterable[tuple[str, str]], *, net_mode: str = "deny", net_required: bool = False,
          net: Iterable[tuple[str, int]] = ()) -> Spec:
    """Validate and assemble a spec; raises :class:`SpecError` on anything inexpressible."""
    if net_mode not in ("deny", "off"):
        raise SpecError(f"net_mode must be 'deny' or 'off', not {net_mode!r}")
    fs_rules: list[FsRule] = []
    for path, perms in fs:
        clean = _clean_perms(perms)
        if not clean:
            raise SpecError(f"no permissions for {path!r} — the rule would grant nothing")
        fs_rules.append(FsRule(_clean_path(path), clean))
    net_rules: list[NetRule] = []
    for kind, port in net:
        if kind not in ("connect", "bind"):
            raise SpecError(f"net kind must be 'connect' or 'bind', not {kind!r}")
        try:
            value = int(port)
        except (TypeError, ValueError) as exc:
            raise SpecError(f"port {port!r} is not an integer") from exc
        if not 0 <= value <= 65535:
            raise SpecError(f"port {value} out of range")
        net_rules.append(NetRule(kind, value))
    return Spec(tuple(fs_rules), net_mode, bool(net_required), tuple(net_rules))


def parse(text: str) -> Spec:
    """Read the format back. Unknown directives are an error, not a silent skip."""
    fs: list[tuple[str, str]] = []
    net: list[tuple[str, int]] = []
    mode = "deny"
    required = False
    seen_version = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 3)
        head = parts[0]
        if head == "version":
            if parts[1:] != [str(SPEC_VERSION)]:
                raise SpecError(f"line {lineno}: unsupported spec version {parts[1:]}")
            seen_version = True
        elif head == "fs":
            # ``fs <perms> <path>`` with a PATH THAT MAY CONTAIN SPACES: split on the first two
            # runs of whitespace only, the rest is the path (the C reader does the same with
            # strchr). ``split(None, 2)`` on the line after the head would still eat the head, so
            # the split is done once here and the remainder taken whole.
            match = re.match(r"^fs\s+(\S+)\s+(.+)$", line)
            if not match:
                raise SpecError(f"line {lineno}: 'fs <perms> <path>' expected")
            fs.append((match.group(2), match.group(1)))
        elif head == "net":
            rest = parts[1:]
            if not rest:
                raise SpecError(f"line {lineno}: 'net <directive>' expected")
            if rest == ["off"]:
                mode = "off"
            elif rest == ["deny"]:
                mode = "deny"
            elif rest == ["required"]:
                required = True
            elif len(rest) >= 3 and rest[0] == "allow":
                net.append((rest[1], int(rest[2])))
            else:
                raise SpecError(f"line {lineno}: unknown net directive {' '.join(rest)!r}")
        else:
            raise SpecError(f"line {lineno}: unknown directive {head!r}")
    if not seen_version:
        raise SpecError("spec has no 'version' line")
    return build(fs, net_mode=mode, net_required=required, net=net)


def write(spec: Spec, path: str | Path) -> Path:
    """Write the spec atomically-enough for a child that reads it immediately after."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(spec.render(), encoding="utf-8")
    return target
