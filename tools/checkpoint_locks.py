"""Index-lock hygiene for the checkpoint shadow store.

Why this module exists
----------------------
``CheckpointManager`` stages with ``GIT_INDEX_FILE=<store>/indexes/<hash>``.
Git guards that index with ``<hash>.lock``; a worker killed while holding it
(SIGKILL, gateway restart, a killed ``git add``) leaves a **zero-byte** lock
behind.  Every later checkpoint of that project then dies with

    fatal: Не удалось создать «…/indexes/<hash>.lock»: Файл существует

and ``git add -A`` never runs again: the checkpoint stream for the project is
silently dead until a human deletes the file.  Measured 22.11.2026: 458 such
failures across three profiles (05 — 240, 02 — 162, 03 — 56), one lock holding
for ~18 hours.

Two rules, both proven by evidence rather than by age alone
-----------------------------------------------------------
1. **Dead means proven dead.**  ``hermes_cli._early_recovery._held_open`` is the
   one authority on "may a running process still own this path": on Linux it
   reads every process's fds through ``/proc`` and returns the holder, ``False``
   (proof nothing holds it) or ``None`` (unknowable — hidepid, a foreign OS).
   Only ``False`` authorises removal, so a live ``git`` that is about to write
   the index keeps its lock.  Age is a second, independent gate: a lock younger
   than ``min_age_seconds`` is never touched, because a git may create the file
   a moment before it starts writing.
2. **Every decision is journalled.**  ``<checkpoint base>/lock-sweep.jsonl``
   records what was removed, what was kept and why, so a fleet cleanup can be
   attached to a card instead of being an unverifiable "I deleted some files".
3. **Every refusal is visible.**  :class:`LockHygiene` (below) is the manager's
   half of this module: it sweeps at startup, retries staging after releasing a
   *dead* lock, and records each failed stage so that it reaches the worker's
   card — 458 "File exists" refusals once sat in ``agent.log`` while three
   profiles' checkpoint streams were dead and nobody was told.

The module is import-light on purpose: ``_held_open`` is imported lazily, so a
checkpoint that finds no lock pays one ``scandir`` on the indexes directory.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover - annotation only, avoids a facade cycle
    from tools.checkpoint_manager import CheckpointManager

logger = logging.getLogger(__name__)

# A lock older than this with no live holder is debris, not a checkpoint in
# progress: a whole ``git add -A`` of a 50k-file tree finishes in seconds, while
# the git timeout itself is 10-60 s.
DEFAULT_MIN_AGE_SECONDS = 10 * 60

# Name of the journal written beside ``store/`` (never inside it: the store is a
# bare repo whose own ``git add``/``gc`` would otherwise see the file appear).
JOURNAL_NAME = "lock-sweep.jsonl"

_ENV_MIN_AGE = "HERMES_CHECKPOINT_LOCK_MIN_AGE_MINUTES"


def min_age_seconds() -> int:
    """Configured age gate: ``HERMES_CHECKPOINT_LOCK_MIN_AGE_MINUTES`` (default 10)."""
    try:
        raw = os.environ.get(_ENV_MIN_AGE, "").strip()
        if raw:
            return max(0, int(float(raw) * 60))
    except (TypeError, ValueError):
        pass
    return DEFAULT_MIN_AGE_SECONDS


@dataclass(frozen=True)
class LockDecision:
    """One lock's verdict, journalled verbatim."""

    path: str
    action: str          # "removed" | "kept"
    reason: str          # human-readable, locale-free
    age_seconds: float
    size_bytes: int
    holder: str = ""

    def as_dict(self) -> dict:
        out = {
            "ts": round(time.time(), 3),
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "age_seconds": round(self.age_seconds, 1),
            "size_bytes": self.size_bytes,
        }
        if self.holder:
            out["holder"] = self.holder
        return out


@dataclass
class SweepResult:
    scanned: int = 0
    removed: list[LockDecision] = field(default_factory=list)
    kept: list[LockDecision] = field(default_factory=list)
    journal_path: Optional[str] = None

    @property
    def removed_paths(self) -> list[str]:
        return [d.path for d in self.removed]

    def journal_records(self) -> list[dict]:
        return [*(d.as_dict() for d in self.removed), *(d.as_dict() for d in self.kept)]


def journal_path(base: Path) -> Path:
    return Path(base) / JOURNAL_NAME


def _append_journal(base: Path, records: Iterable[dict]) -> Optional[str]:
    """Append ``records`` as JSON lines; never raises (a journal must not block a checkpoint)."""
    records = list(records)
    if not records:
        return None
    path = journal_path(base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return str(path)
    except OSError as exc:  # pragma: no cover - disk full / read-only home
        logger.warning("Could not write checkpoint lock journal %s: %s", path, exc)
        return None


def index_lock_path(index_file: Path) -> Path:
    """The lock git takes for ``GIT_INDEX_FILE=<index_file>`` (``<index>.lock``)."""
    return Path(index_file).with_name(f"{Path(index_file).name}.lock")


def _holder(path: Path, root: Optional[Path] = None) -> tuple[bool, str]:
    """``(proven_dead, holder_label)`` for a lock path.

    ``proven_dead`` is True only when the platform's proof machinery *proves*
    nothing holds the file.  ``None`` (unknowable) and any holder both mean
    "leave it alone" — a stuck checkpoint is recoverable, a clobbered live
    index write is not.  ``root`` (the store) widens the probe from "someone has
    this fd open" to "some git is working in this store", which is what keeps a
    live ``git add`` that has not opened the lock yet.
    """
    try:
        from hermes_cli._early_recovery import _held_open
    except ImportError:  # pragma: no cover - a partial tree has no probe
        logger.debug("Holder probe unavailable", exc_info=True)
        return False, "holder probe unavailable"
    try:
        verdict = _held_open(path, root=root)
    except OSError:  # pragma: no cover - a probe must never break a checkpoint
        logger.debug("Holder probe failed for %s", path, exc_info=True)
        return False, "holder probe failed"
    if verdict is False:
        return True, ""
    if verdict is True or verdict is None:
        return False, "holder unknown"
    return False, str(verdict)


def sweep_index_locks(
    store: Path,
    *,
    min_age: Optional[int] = None,
    only: Optional[Iterable[Path]] = None,
    journal: bool = True,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> SweepResult:
    """Remove zero-byte ``indexes/*.lock`` files no live process can own.

    ``only`` restricts the sweep to named lock paths (the retry path uses it for
    the one lock that just blocked a checkpoint).  Everything is journalled
    beside the store so a fleet run leaves one auditable file per profile.
    """
    store = Path(store)
    indexes = store / "indexes"
    age_gate = min_age_seconds() if min_age is None else max(0, int(min_age))
    now = time.time() if now is None else now
    result = SweepResult()

    candidates: list[Path] = []
    if only is not None:
        candidates = [Path(p) for p in only]
    elif indexes.is_dir():
        try:
            candidates = sorted(p for p in indexes.iterdir() if p.suffix == ".lock")
        except OSError as exc:  # pragma: no cover - unreadable home
            logger.debug("Could not list %s: %s", indexes, exc)
            return result

    for lock in candidates:
        try:
            st = lock.stat()
        except OSError:
            continue  # gone: another sweep or its owner finished
        result.scanned += 1
        age = max(0.0, now - st.st_mtime)
        if st.st_size > 0:
            # A non-empty lock is a live git's index in flight, never debris.
            result.kept.append(LockDecision(str(lock), "kept", "not empty", age, st.st_size))
            continue
        if age < age_gate:
            result.kept.append(LockDecision(str(lock), "kept", "younger than age gate", age, st.st_size))
            continue
        dead, holder = _holder(lock, root=store)
        if not dead:
            result.kept.append(LockDecision(str(lock), "kept", f"owner possible: {holder}",
                                            age, st.st_size, holder))
            continue
        if dry_run:
            result.removed.append(LockDecision(str(lock), "would-remove", "zero bytes, no live holder", age, st.st_size))
            continue
        try:
            lock.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            result.kept.append(LockDecision(str(lock), "kept", f"unlink failed: {exc}", age, st.st_size))
            continue
        logger.warning(
            "Removed abandoned checkpoint index lock %s (%.0fs old, no live git holds it)", lock, age)
        result.removed.append(LockDecision(str(lock), "removed", "zero bytes, no live holder", age, st.st_size))

    if journal and result.scanned and not dry_run:
        result.journal_path = _append_journal(store.parent, result.journal_records())
    return result


def journal_decision(base: Path, decision: LockDecision) -> Optional[str]:
    """Append one decision (e.g. a retry-path release) to the store's journal."""
    return _append_journal(base, [decision.as_dict()])


def release_dead_lock(lock: Path, store: Optional[Path] = None, *, journal: bool = True) -> bool:
    """Drop ``lock`` if (and only if) nothing holds it; ignore the age gate.

    This is the focused repair for a lock that *just* refused a checkpoint: the
    caller already has proof (git reported "File exists" and this process is not
    holding it), so waiting for the age gate would leave the project dead for
    another ten minutes.
    """
    lock = Path(lock)
    if not lock.exists():
        return False
    dead, holder = _holder(lock, root=store or lock.parent.parent)
    if not dead:
        logger.debug("Kept index lock %s: %s", lock, holder)
        return False
    try:
        size = lock.stat().st_size
        age = max(0.0, time.time() - lock.stat().st_mtime)
    except OSError:
        size, age = 0, 0.0
    try:
        lock.unlink()
    except OSError:
        return False
    logger.warning("Released dead checkpoint index lock %s after a refused staging", lock)
    if journal and store is not None:
        journal_decision(Path(store).parent,
                         LockDecision(str(lock), "removed", "zero bytes, no live holder (retry path)",
                                      age, size))
    return True


def index_lock_contention(stderr: str) -> Optional[str]:
    """The lock path git named when its index lock already exists, else ``None``.

    Git's message is localised ("File exists" / «Файл существует»), so the
    recognition keys on the shape git always emits — ``<path>.lock`` — plus a
    refusal marker, never on one language's wording.
    """
    text = stderr or ""
    if not text:
        return None
    match = re.search(r"[^\s:«»'\"]*\.lock(?![\w.])", text)
    if match is None:
        return None
    lowered = text.lower()
    markers = ("file exists", "файл существует", "already exists", "уже существует",
               "unable to create", "не удалось создать", "cannot create")
    if not any(marker in lowered for marker in markers):
        return None
    return match.group(0)


def build_manager(
    enabled: bool = False,
    max_snapshots: int = 20,
    max_total_size_mb: int = 500,
    max_file_size_mb: int = 10,
    *, notice: Optional[Callable[[str], None]] = None,
) -> CheckpointManager:
    """Construct a manager and point its failure notices at a visible channel.

    Lives here rather than in ``agent/agent_init.py`` (which is at its file-size
    cap and only calls this) so the *why* stays next to the notice code it serves:
    before this hook the sole trace of a dead checkpoint stream was an agent.log
    line, and three profiles ran with checkpointing silently broken.
    """
    from tools.checkpoint_manager import CheckpointManager

    manager = CheckpointManager(
        enabled=enabled,
        max_snapshots=max_snapshots,
        max_total_size_mb=max_total_size_mb,
        max_file_size_mb=max_file_size_mb,
    )
    manager.notice_callback = notice
    return manager


class LockHygiene:
    """Visible-failure bookkeeping + index-lock repair, mixed into the manager."""

    # Declared for the type checker; ``CheckpointManager.__init__`` sets them.
    enabled: bool
    notice_callback: object
    _last_failure: Optional[dict]
    _notified_failures: set
    released_locks: list
    _store_swept: bool

    def _notify(self, message: str) -> None:
        """Send a checkpoint warning to the visible channel, once per distinct message."""
        key = message.strip()
        if not key or key in self._notified_failures:
            return
        self._notified_failures.add(key)
        logger.warning("%s", key)
        try:
            # Imported inside the guard: a partial/broken install can raise ImportError
            # here, and a notice must never break the turn it is reporting on.
            from tools.checkpoint_notice import post_worker_notice

            post_worker_notice(key)
        except Exception:  # a notice must never break the turn
            logger.debug("checkpoint card notice failed", exc_info=True)
        callback = self.notice_callback
        if callback is None:
            return
        try:
            callback(key)
        except Exception:  # a notice must never break the turn
            logger.debug("checkpoint notice callback failed", exc_info=True)

    def _sweep_index_locks_once(self) -> None:
        """Startup hygiene: drop abandoned zero-byte index locks of this store once per process.

        A worker killed mid-``git add`` leaves ``indexes/<hash>.lock`` and every later
        checkpoint of that project dies with "File exists" — silently, since the failure only
        reached ``agent.log``.  The sweep removes a lock only when it is older than the age
        gate AND the platform proves no live git holds it (``tools.checkpoint_locks``).
        """
        from tools.checkpoint_manager import _INDEXES_DIRNAME, _store_path
        from tools.checkpoint_locks import sweep_index_locks

        if not self.enabled or self._store_swept:
            return
        store = _store_path()
        if not (store / _INDEXES_DIRNAME).is_dir():
            return  # nothing to sweep yet: the latch stays open for the first real store
        self._store_swept = True
        try:
            result = sweep_index_locks(store)
        except OSError:  # an unreadable home must not fail the turn
            logger.debug("Checkpoint lock sweep failed (non-fatal)", exc_info=True)
            return
        if result.removed:
            self.released_locks.extend(result.removed_paths)
            self._notify(
                f"Checkpoints: removed {len(result.removed)} abandoned index lock(s) in "
                f"{(store.parent)} — checkpointing was blocked by a killed worker. "
                f"Journal: {result.journal_path}")

    def lock_report(self) -> dict:
        """What this manager's lock hygiene did: released paths + last failure."""
        return {
            "released": list(self.released_locks),
            "last_failure": dict(self._last_failure) if self._last_failure else None,
        }

    def _failure_message(self) -> str:
        """The human-facing line for the last unrepaired checkpoint failure."""
        fail = self._last_failure or {}
        return (
            "⚠️ Checkpoints are NOT being taken for "
            f"{fail.get('working_dir') or 'this workspace'}: {fail.get('stage')} failed — "
            f"{fail.get('error')}. Changes in this run are not rollback-able until it is fixed.")

    def _record_failure(self, working_dir: str, stage: str, error: str) -> None:
        import time

        self._last_failure = {
            "working_dir": working_dir,
            "stage": stage,
            "error": (error or "").strip()[:500],
            "ts": time.time(),
        }
        logger.warning("Checkpoint failed for %s at %s: %s", working_dir, stage, error)

    def _record_stage_failure(self, working_dir: str, err: str) -> None:
        """Record a failed ``git add`` — but never over a more specific record.

        ``_stage_with_lock_retry`` already recorded ``git add (index lock held)``, which
        names the actual cause; the outer branch would replace it with the vaguer stage.
        """
        if not self._last_failure:
            self._record_failure(working_dir, "git add", err or "git add failed")

    def _stage_with_lock_retry(
        self, store: Path, working_dir: str, index_file: Path, add_args: list[str],
    ) -> tuple[bool, str]:
        """``git add`` once, and a second time after releasing a *dead* index lock.

        Git refuses with "File exists" when ``indexes/<hash>.lock`` survives a killed
        worker; retrying is what turns that permanent per-project outage into a
        single skipped checkpoint.  A live holder is never released (the proof lives in
        ``tools.checkpoint_locks``), so a genuinely concurrent staging still fails and is
        now reported instead of only logged.
        """
        from tools.checkpoint_manager import _GIT_TIMEOUT, _run_git
        from tools.checkpoint_locks import index_lock_path, release_dead_lock

        ok, _, err = _run_git(
            add_args, store, working_dir,
            timeout=_GIT_TIMEOUT * 2, index_file=index_file,
        )
        if ok:
            return True, ""
        if index_lock_contention(err) is None:
            self._record_failure(working_dir, "git add", err or "git add failed")
            return False, err

        lock = index_lock_path(index_file)
        if not release_dead_lock(lock, store=store):
            self._record_failure(working_dir, "git add (index lock held)", err or "index lock exists")
            return False, err
        self.released_locks.append(str(lock))
        logger.warning("Retrying checkpoint staging of %s after releasing dead lock %s",
                       working_dir, lock)
        self._notify(
            f"Checkpoints: released an abandoned index lock that blocked staging of "
            f"{working_dir} ({lock.name}) — the checkpoint is being retried. Journal: "
            f"{Path(store).parent / JOURNAL_NAME}")
        ok, _, err = _run_git(
            add_args, store, working_dir,
            timeout=_GIT_TIMEOUT * 2, index_file=index_file,
        )
        if ok:
            return True, ""
        self._record_failure(working_dir, "git add", err or "git add failed")
        return False, err

    def _nothing_to_snapshot(
        self, store: Path, working_dir: str, index_file: Path, ref_commit: Optional[str],
    ) -> bool:
        """Whether this turn has nothing worth a snapshot — a routine skip, not a failure.

        Split out of ``_take`` (whose branch ladder was at its complexity cap) and kept
        here because it is the *other half* of the visible-failure rule: these two skips
        must stay at ``logger.debug`` and post no notice, while every real refusal does.
        """
        from tools.checkpoint_manager import _run_git

        if ref_commit:
            ok_diff, _, _ = _run_git(
                ["diff-index", "--cached", "--quiet", ref_commit],
                store, working_dir,
                allowed_returncodes={1},
                index_file=index_file,
            )
            if ok_diff:
                logger.debug("Checkpoint skipped: no changes in %s", working_dir)
                return True
            return False
        # No ref yet — skip only if the index is empty.
        ok_ls, ls_out, _ = _run_git(
            ["ls-files", "--cached"],
            store, working_dir,
            index_file=index_file,
        )
        if ok_ls and not ls_out.strip():
            logger.debug("Checkpoint skipped: empty tree in %s", working_dir)
            return True
        return False
