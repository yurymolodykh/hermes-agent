"""Checked history rewrites shared by snapshot limits and store maintenance."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import time
from typing import Callable


class PruneError(RuntimeError):
    """Pruning stopped without permission to discard another snapshot."""


GC_PENDING_NAME = ".gc-pending"

# How long a snapshot waits for the per-profile store lock before giving up. A checkpoint is the
# rollback insurance of a whole run, so it waits: measured on a live store, one snapshot holds the
# lock 0.6-0.75 s, and two workers of one profile racing lost HALF of their snapshots when the
# acquisition refused instantly (24 refusals / 16 attempts, board evidence 22-24.11.2026: 108
# "store is busy" card notices). Five seconds covers a queue of snapshots with room to spare; a
# refusal after that is a real outage (maintenance's ``git gc`` on a 150-180 MB store) and is
# reported with the holder's pid instead of only the store path.
STORE_LOCK_TIMEOUT_SECONDS = 5.0


def store_lock_path(base: Path) -> Path:
    base = base.resolve()
    return base.with_name(f".{base.name}.lock")


def lock_holder_hint(lock: Path) -> str:
    """Who to blame when ``lock`` could not be taken: the live holder, or that none is visible.

    ``hermes_cli._early_recovery._held_open`` is the one authority on "does a running process
    still own this path" (on Linux it reads every process's fds through /proc, on macOS/BSD it
    asks ``lsof``); a third probe must not grow here. Every answer is honest: a visible holder is
    named with its pid and command line, an unknown one is reported as unknown, and neither is
    invented. The label is folded to one line and clipped — a worker's argv can be a whole script,
    and the failure line ends up in a card comment.
    """
    try:
        from hermes_cli._early_recovery import _held_open

        verdict = _held_open(Path(lock))
    except (ImportError, OSError) as exc:
        # A diagnostic must never replace the failure it explains; log it so a broken probe is
        # visible rather than silently reported as "unknown".
        logging.getLogger(__name__).debug("checkpoint lock holder probe failed: %s", exc, exc_info=True)
        return "holder unknown (probe unavailable)"
    if verdict is True or verdict is None:
        return "holder unknown (the platform could not inspect open files)"
    if verdict is False:
        return "no live process holds a descriptor on the lock file"
    label = " ".join(str(verdict).split())
    return f"held by {label[:120]}{'…' if len(label) > 120 else ''}"


@contextmanager
def store_lock(base: Path, *, timeout: float | None = STORE_LOCK_TIMEOUT_SECONDS):
    """Serialize whole operations, including GC and clear, across processes.

    Waits up to ``timeout`` seconds for the lock (``None`` waits forever, ``0`` tries once) —
    contending snapshots queue instead of being dropped, which is what the silent "store is
    busy" refusal cost every run that lost one. ``pm.filesystem.lock_path`` does the waiting and
    also re-checks that the held descriptor is still the file at the path, so a lock file removed
    while held cannot hand two processes the same lock.
    """
    from pm.filesystem import lock_path

    base.parent.mkdir(parents=True, exist_ok=True)
    # Outside base so clear_all and legacy migration cannot replace its inode.
    lock = store_lock_path(base)
    started = time.monotonic()
    fd = lock_path(lock, timeout=timeout, create_dir=True)
    if fd is None:
        waited = time.monotonic() - started
        raise PruneError(
            f"checkpoint store is busy: {base} — {lock_holder_hint(lock)}; "
            f"gave up after {waited:.1f}s of waiting ({lock})")
    try:
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Pruner:
    run_git: Callable
    store: Path
    cwd: str
    timeout: int
    dir_size: Callable[[Path], int]
    refs_prefix: str

    def _git(self, args: list[str], **kwargs) -> str:
        ok, output, error = self.run_git(args, self.store, self.cwd, **kwargs)
        if not ok:
            raise PruneError(f"checkpoint {args[0]} failed: {error}")
        return output

    def reclaim(self) -> None:
        """Full ``git gc`` — tens of seconds on a GB store, so only the periodic prune calls it.
        A ref rewrite that ran without it leaves a marker so the next prune reclaims."""
        self._git(["reflog", "expire", "--expire=now", "--all"])
        self._git(["gc", "--prune=now", "--quiet"], timeout=self.timeout * 3)
        # Bare stores must remain usable when gc packs all refs.
        for name in ("refs/heads", "branches"):
            (self.store / name).mkdir(parents=True, exist_ok=True)
        try:
            (self.store / GC_PENDING_NAME).unlink()
        except OSError:
            pass

    def gc_pending(self) -> bool:
        return (self.store / GC_PENDING_NAME).exists()

    def _mark_gc_pending(self) -> None:
        try:
            (self.store / GC_PENDING_NAME).touch()
        except OSError:
            pass

    def _commits(self, ref: str) -> list[str]:
        commits = self._git(["rev-list", "--reverse", ref]).splitlines()
        if not commits:
            raise PruneError(f"checkpoint ref has no readable history: {ref}")
        return commits

    def _rewrite(self, ref: str, original: list[str], keep: list[str], *, gc: bool) -> None:
        parent = None
        for sha in keep:
            metadata = self._git(["log", "-1", "--format=%T%x00%aI%x00%cI%x00%B", sha])  # whole body: keeps the take-time line
            fields = metadata.split("\x00", 3)
            if len(fields) != 4:
                raise PruneError(f"invalid checkpoint metadata: {sha}")
            tree, author_date, committer_date, message = fields
            args = ["commit-tree", tree, "--no-gpg-sign", "-m", message]
            if parent is not None:
                args += ["-p", parent]
            parent = self._git(args, extra_env={"GIT_AUTHOR_DATE": author_date, "GIT_COMMITTER_DATE": committer_date})
            if not parent:
                raise PruneError("commit-tree returned no checkpoint identity")
        if parent is None:
            raise PruneError("cannot remove a project's last snapshot")
        # Never overwrite a snapshot published since we read this history.
        self._git(["update-ref", ref, parent, original[-1]])
        if gc:
            self.reclaim()
        else:
            self._mark_gc_pending()

    def trim(self, ref: str, keep_count: int) -> None:
        """Snapshot-count budget on the checkpoint-take path: rewrites the ref, defers the gc."""
        commits = self._commits(ref)
        keep_count = max(1, keep_count)
        if len(commits) > keep_count:
            self._rewrite(ref, commits, commits[-keep_count:], gc=False)

    def drop_one_round(self, cap_bytes: int) -> bool:
        """Over the cap on the take path: drop the oldest snapshot of every project once and
        defer the gc. One round per checkpoint converges over turns; the full loop measured
        against an unchanging pack once flattened every project to a single snapshot."""
        if cap_bytes <= 0 or self.dir_size(self.store) <= cap_bytes:
            return False
        changed = False
        for ref in self._git(["for-each-ref", "--format=%(refname)", self.refs_prefix]).splitlines():
            commits = self._commits(ref)
            if len(commits) > 1:
                self._rewrite(ref, commits, commits[1:], gc=False)
                changed = True
        return changed

    def enforce_size(self, cap_bytes: int) -> bool:
        """Periodic prune: gc per round (the measurement only moves after a repack) until the
        store fits. Returns whether it did; a failed Git step raises immediately."""
        if cap_bytes <= 0 or self.dir_size(self.store) <= cap_bytes:
            return True
        self.reclaim()
        if self.dir_size(self.store) <= cap_bytes:
            return True
        refs = self._git(["for-each-ref", "--format=%(refname)", self.refs_prefix]).splitlines()
        # Keep the existing round-robin policy, remeasure after EACH rewrite.
        # Twenty rounds bound maintenance when many large histories remain.
        for _ in range(20):
            changed = False
            for ref in refs:
                commits = self._commits(ref)
                if len(commits) <= 1:
                    continue
                self._rewrite(ref, commits, commits[1:], gc=True)
                changed = True
                if self.dir_size(self.store) <= cap_bytes:
                    return True
            if not changed:
                break
        return False
