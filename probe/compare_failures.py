"""Classify tests/tools failures: pre-existing vs introduced by the change.

Compares the FAILED/ERROR node ids of the branch run against the base-commit run
(taken in a separate `git worktree` at 0afa73de93). Anything that fails on BOTH is
pre-existing; anything that fails only on the branch is a regression and must be
explained before this task can be called done.

Usage: python3 probe/compare_failures.py <branch.txt> <base.txt>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PAT = re.compile(r"^(FAILED|ERROR)\s+(\S+)")


def failures(path: Path) -> dict[str, str]:
    """node id -> first status letter (F/E), plus the summary lines."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = PAT.match(line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def module_of(node: str) -> str:
    return node.split("::", 1)[0]


def main() -> int:
    branch, base = failures(Path(sys.argv[1])), failures(Path(sys.argv[2]))
    both = sorted(set(branch) & set(base))
    only_branch = sorted(set(branch) - set(base))
    only_base = sorted(set(base) - set(branch))

    print(f"branch failures: {len(branch)}")
    print(f"base   failures: {len(base)}")
    print(f"failing in BOTH (pre-existing): {len(both)}")
    print(f"failing ONLY on branch (REGRESSION): {len(only_branch)}")
    print(f"failing ONLY on base (flaky/fixed):  {len(only_base)}")

    if only_branch:
        print("\n== REGRESSIONS — must be explained ==")
        by_mod: dict[str, int] = {}
        for n in only_branch:
            by_mod[module_of(n)] = by_mod.get(module_of(n), 0) + 1
        for mod, count in sorted(by_mod.items(), key=lambda kv: -kv[1]):
            print(f"  {count:3d}  {mod}")
        print()
        for n in only_branch[:40]:
            print("   ", n)

    if only_base:
        print("\n== only on base (not a regression) ==")
        for n in only_base[:20]:
            print("   ", n)

    print("\nVERDICT:", "NO REGRESSIONS" if not only_branch
          else f"{len(only_branch)} REGRESSIONS to explain")
    return 0 if not only_branch else 1


if __name__ == "__main__":
    raise SystemExit(main())
