"""Guard: card service artifacts must never reach the index.

Kanban workers leave ``PROGRESS*.md``, ``durable/`` evidence trees, ``evidence-*/``
directories and ``REPORT-*.md`` behind in the working tree.  ``.gitignore`` and
``.git/info/exclude`` do not apply to paths that are already tracked, so a worker's
``git add -A`` (or ``git add -f``) puts them back into the index and the artifacts
land in the product repository.  This module is the pre-commit mechanism that stops
that: it inspects the staged paths, not the working tree, and refuses the commit.

Bypass is explicit and one-off: ``ALLOW_ARTIFACT_COMMIT=1``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path

BYPASS_ENV = "ALLOW_ARTIFACT_COMMIT"
ARTIFACT_HINT = (
    "Артефакт хранится в рабочем каталоге карточки или в "
    "~/.hermes/durable/kanban/card-artifacts/<task>/."
)

# Root-anchored directories that belong to the board, never to the product tree.
_ROOT_ARTIFACT_DIR_PREFIXES = ("durable", "evidence-")
_ROOT_ARTIFACT_FILE_PATTERNS = ("evidence-*", "REPORT-*.md")
# Journal-of-progress files are card artifacts wherever they appear.
_PROGRESS_PATTERNS = ("PROGRESS.md", "PROGRESS-*.md")


def _parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.replace("\\", "/").split("/") if part not in ("", "."))


def is_card_artifact(path: str) -> bool:
    """True when ``path`` (repo-relative, POSIX or Windows separators) is a card artifact."""
    parts = _parts(path)
    if not parts:
        return False
    name = parts[-1]
    if any(fnmatchcase(name, pattern) for pattern in _PROGRESS_PATTERNS):
        return True
    if len(parts) > 1 and parts[0].startswith(_ROOT_ARTIFACT_DIR_PREFIXES):
        return True
    if len(parts) == 1 and any(fnmatchcase(name, pattern) for pattern in _ROOT_ARTIFACT_FILE_PATTERNS):
        return True
    return False


def find_card_artifacts(paths: Iterable[str]) -> list[str]:
    """Return the artifact paths from ``paths``, de-duplicated, in input order."""
    seen: dict[str, None] = {}
    for path in paths:
        if is_card_artifact(path):
            seen.setdefault(path, None)
    return list(seen)


def bypass_requested(environ: dict[str, str] | None = None) -> bool:
    """True only for an explicit truthy ``ALLOW_ARTIFACT_COMMIT``."""
    value = (environ if environ is not None else os.environ).get(BYPASS_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def staged_paths(repo: Path | str = ".") -> list[str]:
    """Paths added/copied/modified/renamed in the index (deletions never count)."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    return [entry for entry in result.stdout.decode("utf-8", "surrogateescape").split("\0") if entry]


def format_refusal(paths: Iterable[str]) -> str:
    """Russian refusal text: the offending paths plus where the artifact belongs."""
    listed = list(paths)
    lines = [
        f"Отказ: служебные артефакты карточки не должны попадать в коммит (найдено {len(listed)}):",
        *(f"  - {path}" for path in listed),
        ARTIFACT_HINT,
        f"Осознанное исключение — только явным флагом: {BYPASS_ENV}=1 git commit ...",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Pre-commit entry point; explicit paths in ``argv`` replace the index read."""
    argv = sys.argv[1:] if argv is None else argv
    if bypass_requested():
        print(f"{BYPASS_ENV}=1 — проверка служебных артефактов пропущена.", file=sys.stderr)
        return 0
    try:
        paths = argv if argv else staged_paths()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"guard: не удалось прочитать индекс git: {exc}", file=sys.stderr)
        return 1
    offenders = find_card_artifacts(paths)
    if not offenders:
        return 0
    print(format_refusal(offenders), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
