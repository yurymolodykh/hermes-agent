"""Preserve a kanban worker's work before its iteration budget runs out.

An iteration budget is per-turn and per-profile (``agent.max_turns`` of the
*assignee*), and the hard stop is a ``break`` out of the tool loop: nothing about
it commits, so a worker that produced 1459 verified lines and then hit
``300/300`` left them as uncommitted working-tree state and the card looked like
a plain failure (``t_f3d28e37``, step 6 of the multi-profile deck).

This module is the last-resort safety net for that stop, not a work tracker:

* ``checkpoint_wip`` commits what is in the card's working tree (or, without a
  git repository, archives it beside the board) so the iterations already spent
  are never lost;
* ``describe_stop`` phrases the stop so the board says *stopped by budget, work
  in commit X, still open Y* instead of a bare failure.

Both are best-effort: a checkpoint must never raise into the turn loop, and a
worker that has nothing to save must not get an empty commit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Working-tree entries that are never a deliverable: dependency trees, caches and
# VCS/agent bookkeeping. A checkpoint that archived them would be unusable and,
# for a checkout with a venv, gigabytes wide.
_EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".hermes", ".worktrees", "site-packages",
})
_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})

# Ceiling for the archive fallback. A card whose tree exceeds this is not
# checkpointed silently: the caller is told, so the loss is visible.
_ARCHIVE_MAX_BYTES = 200 * 1024 * 1024

# Subject prefix the resume path and the board both recognise.
WIP_SUBJECT_PREFIX = "WIP бюджет:"


@dataclass
class WipCheckpoint:
    """Outcome of one checkpoint attempt. ``ok`` means work is durable somewhere."""

    mode: str = "none"            # "git" | "archive" | "none"
    commit: Optional[str] = None  # short sha of the WIP commit
    branch: Optional[str] = None
    archive: Optional[str] = None
    files: list[str] = field(default_factory=list)
    detail: Optional[str] = None  # why there is nothing to show, when mode == "none"

    @property
    def ok(self) -> bool:
        return self.mode != "none"

    @property
    def locator(self) -> str:
        if self.mode == "git" and self.commit:
            return f"коммит {self.commit}" + (f" (ветка {self.branch})" if self.branch else "")
        if self.mode == "archive" and self.archive:
            return f"архив {self.archive}"
        return "нет сохранения"


@dataclass
class WipPreservation:
    """Outcome of a mid-run save: a git ref, a board-side archive, or nothing.

    Separate from :class:`WipCheckpoint` because the reminder threshold does not
    stop the run: it parks the tree without a commit, while the checkpoint is the
    durable record a budget stop reports on.
    """

    mode: str = "none"            # "git" | "archive" | "none"
    locator: Optional[str] = None  # ``refs/hermes-wip/<task>`` or the tarball path
    detail: Optional[str] = None  # why nothing was saved, when mode == "none"


def _git(cwd: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """``git -C cwd args`` that never raises: a broken repo is a skipped checkpoint."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess([str(cwd), *args], 1, "", str(exc))


def _is_excluded(rel: str) -> bool:
    parts = Path(rel).parts
    if any(p in _EXCLUDED_DIRS for p in parts):
        return True
    return Path(rel).suffix.lower() in _EXCLUDED_SUFFIXES


def _walk_paths(root: Path, limit: int = 5000) -> list[str]:
    """Files in *root* (relative posix paths) for a workspace with no repository."""
    out: list[str] = []
    try:
        for entry in root.rglob("*"):
            if len(out) >= limit:
                break
            if not entry.is_file() or entry.is_symlink():
                continue
            rel = entry.relative_to(root).as_posix()
            if _is_excluded(rel):
                continue
            out.append(rel)
    except OSError:
        return out
    return out


def _changed_paths(root: Path) -> list[str]:
    """Repository-relative paths git reports as changed, minus dependency trees.

    ``.gitignore`` already hides a venv in a normal checkout; the explicit filter
    covers the cases it does not (an ignored-less tree, a nested dependency dir).
    A workspace with no repository at all has no "changed" set — every file it
    holds is the deliverable, so the walk stands in for ``git status``.
    """
    if _repo_root(root) is None:
        return _walk_paths(root)
    proc = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    # -z: entries are NUL-separated; a rename carries the old path in its own field.
    for entry in proc.stdout.split("\0"):
        if len(entry) < 4:
            continue
        rel = entry[3:]
        if rel.startswith('"') or not rel.strip():
            continue
        if _is_excluded(rel):
            continue
        paths.append(rel)
    return paths


def _repo_root(start: Path) -> Optional[Path]:
    proc = _git(start, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def _current_branch(root: Path) -> Optional[str]:
    proc = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    name = proc.stdout.strip()
    # A detached HEAD (a probe checkout) commits onto nothing a successor can
    # resume from, so it is reported but not presented as a branch.
    return name if proc.returncode == 0 and name and name != "HEAD" else None


def _commit_wip(root: Path, files: Iterable[str], subject: str) -> Optional[str]:
    """Stage and commit exactly *files*; None when the commit did not land."""
    paths = list(files)
    if not paths:
        return None
    added = _git(root, "add", "--", *paths)
    if added.returncode != 0:
        return None
    # A pre-commit hook is the repository's policy, not this checkpoint's: a WIP
    # save that a formatter/linter rejects would lose the very work it protects.
    commit = _git(root, "commit", "--no-verify", "-m", subject, "--", *paths)
    if commit.returncode != 0:
        return None
    sha = _git(root, "rev-parse", "--short", "HEAD")
    return sha.stdout.strip() if sha.returncode == 0 and sha.stdout.strip() else None


def _tree_fingerprint(root: Path, files: Iterable[str]) -> str:
    """Cheap identity of a workspace: relative paths plus each file's size and mtime.

    A full content hash would read every byte of a card tree on each reminder; the
    reminder only has to answer "did anything change since the last copy?", and an
    edit that keeps both size and mtime is not a case archiving must catch.
    """
    digest = hashlib.sha256()
    for rel in sorted(files):
        try:
            st = (root / rel).stat()
        except OSError:
            continue
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8", "replace"))
    return digest.hexdigest()


_FINGERPRINT_FILE = ".wip-fingerprint"


def _existing_archive(out_dir: Path, fingerprint: str) -> Optional[str]:
    """The archive already holding this exact tree state, when one exists.

    Newest by mtime, not by name: two saves inside one second get a ``-N`` suffix
    that sorts *before* the bare stamp, so a name sort would return the older copy.
    """
    try:
        if (out_dir / _FINGERPRINT_FILE).read_text(encoding="utf-8").strip() != fingerprint:
            return None
        previous = sorted(out_dir.glob("wip-*.tar.gz"), key=lambda p: p.stat().st_mtime_ns)
    except OSError:
        return None
    return str(previous[-1]) if previous else None


def _archive_tree(root: Path, files: Iterable[str], task_id: str, stamp: str) -> Optional[str]:
    """Fallback for a workspace with no git repository: a bounded tarball of its attachments.

    Repeating the call on an unchanged tree returns the archive already on disk
    instead of writing a second one: the reminder threshold can fire more than once
    per card, and a pile of identical tarballs is not more safety.
    """
    paths = list(files)
    if not paths:
        return None
    # ``task_attachments_dir`` is the board's own convention (and honours the board's
    # attachments-root override), so the archive lands where a human looks for it.
    from hermes_cli.kanban_db import task_attachments_dir
    out_dir = task_attachments_dir(task_id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    fingerprint = _tree_fingerprint(root, paths)
    unchanged = _existing_archive(out_dir, fingerprint)
    if unchanged:
        return unchanged
    target = out_dir / f"wip-{stamp}.tar.gz"
    # Two saves inside one second must not overwrite each other: a changed tree
    # in the same tick still deserves its own file.
    serial = 1
    while target.exists():
        serial += 1
        target = out_dir / f"wip-{stamp}-{serial}.tar.gz"
    total = 0
    try:
        with tarfile.open(target, "w:gz") as tar:
            for rel in paths:
                src = root / rel
                if not src.is_file():
                    continue
                total += src.stat().st_size
                if total > _ARCHIVE_MAX_BYTES:
                    break
                tar.add(src, arcname=rel, recursive=False)
    except (OSError, tarfile.TarError):
        target.unlink(missing_ok=True)
        return None
    try:
        (out_dir / _FINGERPRINT_FILE).write_text(fingerprint + "\n", encoding="utf-8")
    except OSError:
        pass
    return str(target)


def card_workspace() -> Optional[Path]:
    """The dispatcher-pinned working directory, when this process really is a card worker.

    Gated on the FULL dispatcher envelope (``agent.kanban_stop.is_dispatcher_worker_env``),
    not on ``HERMES_KANBAN_WORKSPACE`` alone: a long-lived interactive process inherits
    that variable from an old worker context, and a checkpoint must never commit into a
    repository this process does not own.
    """
    raw = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not raw:
        return None
    from agent.kanban_stop import is_dispatcher_worker_env

    # Declining to save is the safe answer: an unreadable env must never be read as
    # "this process owns the card".
    try:
        if not is_dispatcher_worker_env():
            return None
    except (ImportError, OSError, ValueError):  # health: allow BLE001 -- env probe may fail on any import/env state; refusing to checkpoint is the safe answer
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def _git_env(cwd: Path, *args: str, env_extra: Optional[dict] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """Like :func:`_git` but with extra environment (used for a throwaway index)."""
    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False, stdin=subprocess.DEVNULL, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess([str(cwd), *args], 1, "", str(exc))


def preserve_wip_ref(*, task_id: Optional[str] = None, workspace: Optional[Path] = None) -> Optional[str]:
    """Park the working tree under ``refs/hermes-wip/<task>``; no branch commit.

    The mid-flight safety net: at ~85% of the budget the worker may still finish,
    so the run must not litter the branch with a commit it later has to explain.
    A commit object reachable through a ref keeps the exact tree bytes (a
    successor can ``git diff`` / ``git checkout`` against it) while leaving branch
    history, the real index and the working tree exactly as they were.

    The tree is built in a scratch index (``GIT_INDEX_FILE``) over a copy of the
    real one, so untracked files — the normal state of work in progress — are
    included without ever staging them in the card's own index.
    """
    root = workspace or card_workspace()
    if root is None:
        return None
    repo = _repo_root(root)
    if repo is None:
        return None
    files = _changed_paths(root)
    if not files:
        return None

    real_index = _git(repo, "rev-parse", "--git-path", "index")
    index_path = real_index.stdout.strip() if real_index.returncode == 0 else "index"
    if not os.path.isabs(index_path):
        index_path = str(repo / index_path)

    scratch = Path(tempfile.mkdtemp(prefix="hermes-wip-"))
    tmp_index = scratch / "index"
    try:
        if os.path.exists(index_path):
            shutil.copy2(index_path, tmp_index)
        env = {"GIT_INDEX_FILE": str(tmp_index)}
        if _git_env(repo, "add", "-A", "--", *files, env_extra=env).returncode != 0:
            return None
        tree = _git_env(repo, "write-tree", env_extra=env)
        tree_sha = tree.stdout.strip() if tree.returncode == 0 else ""
        if not tree_sha:
            return None
        head = _git(repo, "rev-parse", "HEAD")
        head_sha = head.stdout.strip() if head.returncode == 0 else None
        parents = ["-p", head_sha] if head_sha else []
        tid = (task_id or (os.environ.get("HERMES_KANBAN_TASK") or "task")).strip()
        commit = _git_env(
            repo, "commit-tree", tree_sha, *parents, "-m",
            f"{WIP_SUBJECT_PREFIX} промежуточный снимок — {tid}",
            env_extra={**env, "GIT_AUTHOR_NAME": "hermes",
                       "GIT_AUTHOR_EMAIL": "hermes@localhost",
                       "GIT_COMMITTER_NAME": "hermes",
                       "GIT_COMMITTER_EMAIL": "hermes@localhost"},
        )
        commit_sha = commit.stdout.strip() if commit.returncode == 0 else ""
        if not commit_sha:
            return None
        ref = f"refs/hermes-wip/{tid}"
        if _git(repo, "update-ref", ref, commit_sha).returncode != 0:
            return None
        return ref
    except OSError:
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def preserve_wip_tree(
    *, task_id: Optional[str] = None, workspace: Optional[Path] = None
) -> WipPreservation:
    """Save the working tree mid-run, without committing and without stopping.

    The reminder threshold exists so the worker still has budget left; a
    ``workspace_kind=scratch`` card (no repository at all) has nothing to park,
    so it gets the same board-side tarball the hard stop would have written.
    Returns where the copy landed, for the reminder text.
    """
    root = workspace or card_workspace()
    if root is None:
        return WipPreservation(detail="нет каталога карточки")
    if _repo_root(root) is not None:
        ref = preserve_wip_ref(task_id=task_id, workspace=root)
        if ref:
            return WipPreservation(mode="git", locator=ref)
        return WipPreservation(detail="ref не создан (индекс занят или git не ответил)")
    try:
        files = _changed_paths(root)
    except (OSError, subprocess.SubprocessError) as exc:  # never break the turn loop
        return WipPreservation(detail=f"не удалось прочитать дерево: {exc}")
    if not files:
        return WipPreservation(detail="нечего сохранять")

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    tid = (task_id or (os.environ.get("HERMES_KANBAN_TASK") or "task")).strip()
    try:
        archive = _archive_tree(root, files, tid, stamp)
    except (OSError, tarfile.TarError) as exc:
        return WipPreservation(detail=f"архив не создан: {exc}")
    if archive:
        return WipPreservation(mode="archive", locator=archive)
    return WipPreservation(detail="архив не создан")


def checkpoint_wip(
    *,
    reason: str,
    used: int,
    max_total: int,
    workspace: Optional[Path] = None,
    task_id: Optional[str] = None,
) -> WipCheckpoint:
    """Make the card's current work durable; never raises.

    *reason* is a short marker for the commit subject (``"85%"`` / ``"предел"``).
    Idempotent in effect: with nothing changed there is no commit, so a card that
    fits its budget leaves no WIP commits behind.
    """
    root = workspace or card_workspace()
    if root is None:
        return WipCheckpoint(detail="нет каталога карточки (HERMES_KANBAN_WORKSPACE не задан)")
    try:
        files = _changed_paths(root)
    except (OSError, subprocess.SubprocessError) as exc:  # never break the turn loop
        return WipCheckpoint(detail=f"не удалось прочитать состояние дерева: {exc}")
    if not files:
        return WipCheckpoint(detail="незакоммиченных изменений нет")

    subject = f"{WIP_SUBJECT_PREFIX} {used}/{max_total} — {reason}"
    repo = _repo_root(root)
    detail: Optional[str] = None
    if repo is not None:
        try:
            sha = _commit_wip(repo, files, subject)
        except (OSError, subprocess.SubprocessError) as exc:
            sha = None
            detail = f"коммит не удался: {exc}"
        else:
            detail = None if sha else "git не создал коммит (конфликт или блокировка индекса)"
        if sha:
            return WipCheckpoint(mode="git", commit=sha, branch=_current_branch(repo),
                                 files=files)
    else:
        detail = "нет git-репозитория в каталоге карточки"

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    tid = (task_id or (os.environ.get("HERMES_KANBAN_TASK") or "task")).strip()
    try:
        archive = _archive_tree(root, files, tid, stamp)
    except (OSError, tarfile.TarError) as exc:
        return WipCheckpoint(detail=f"{detail}; архив не создан: {exc}")
    if archive:
        return WipCheckpoint(mode="archive", archive=archive, files=files, detail=detail)
    return WipCheckpoint(detail=detail or "нечего сохранять", files=files)


def _open_items(workspace: Optional[Path]) -> list[str]:
    """Unchecked checklist items from the card's own ``PROGRESS.md``, if it keeps one."""
    if workspace is None:
        return []
    progress = workspace / "PROGRESS.md"
    try:
        text = progress.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # Only explicitly unchecked boxes: a plain bullet is a note, not an item.
        if stripped.startswith(("- [ ]", "* [ ]")):
            items.append(stripped[5:].strip())
    return items[-5:]


def describe_stop(
    checkpoint: Optional[WipCheckpoint],
    *,
    used: int,
    max_total: int,
    workspace: Optional[Path] = None,
) -> str:
    """One line for the board: what stopped, where the work is, what is still open.

    The bare "task could not complete within the allowed iterations" reads as a
    give-up even when the tree holds verified work; this names the commit so a
    successor resumes from it instead of redoing the card.
    """
    head = f"остановлено по бюджету {used}/{max_total}"
    parts = [head]
    if checkpoint is not None and checkpoint.ok:
        parts.append(f"работа в {checkpoint.locator}")
        if checkpoint.files:
            shown = ", ".join(checkpoint.files[:6])
            more = f" и ещё {len(checkpoint.files) - 6}" if len(checkpoint.files) > 6 else ""
            parts.append(f"сохранено файлов: {len(checkpoint.files)} ({shown}{more})")
    elif checkpoint is not None:
        parts.append(f"сохранения нет: {checkpoint.detail or 'причина не установлена'}")
    else:
        parts.append("сохранения нет: состояние дерева не читалось")
    open_items = _open_items(workspace or card_workspace())
    if open_items:
        parts.append("осталось: " + "; ".join(open_items))
    else:
        parts.append("осталось: критерии приёмки карточки не закрыты (см. PROGRESS.md/доску)")
    return " · ".join(parts)


__all__ = [
    "WIP_SUBJECT_PREFIX",
    "WipCheckpoint",
    "WipPreservation",
    "card_workspace",
    "checkpoint_wip",
    "describe_stop",
    "preserve_wip_ref",
    "preserve_wip_tree",
]
