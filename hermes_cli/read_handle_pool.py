"""Bounded pool of reusable READ-ONLY SessionDB attaches for per-request dashboard reads.

Every dashboard read (``/api/status``'s active-session count, session lists, analytics, cron,
chat workspaces) calls ``_open_session_db_for_profile(profile, read_only=True)``, which built a
fresh ``SessionDB`` per request: connect, ``apply_database_pragmas`` (an 8 MB ``cache_size``
allocation per connection), the declared-schema probe, then close. Measured on a real
``web_server`` app with 120 sessions: 13.75 ms per active-session count, of which 8.27 ms (60%)
was that per-request open/close churn; the same work on a REUSED read-only handle costs 5.49 ms.

What is reused is the whole handle, deliberately, because the expensive part is connection
setup — not any single PRAGMA. Caching PRAGMAs instead would be wrong twice over: SQLite state
is per connection (so a fresh connection still needs them), and skipping them on a reused
connection would be right for the wrong reason if the pool ever handed out a mis-initialized
one. A pooled handle therefore carries exactly the initialization it was opened with, and the
pool refuses a handle whose FILE changed underneath it.

Correctness rules, in order of what would break without them:

* **File identity.** ``state.db`` is replaced out of band (``hermes snapshot restore``,
  ``mv``, a recreated store). A pooled read-only handle holds the old inode, so before handing
  one out the pool compares the recorded file identity and DISCARDS the handle on a mismatch —
  a read must never answer from a file the path no longer names.
* **Per-thread exclusivity.** A pooled connection is ``check_same_thread=False`` (WAL readers
  are used by whichever thread reads next), so a handle is checked out to exactly one caller at
  a time and returned only when that caller is done.
* **Bounds.** Idle handles are capped per path and process-wide (the descriptor ceilings the
  read pool already respects, ``_READ_POOL_MAX`` / ``_READ_POOL_PROCESS_MAX``) and evicted LRU —
  the longest-idle handle goes first, so a busy profile is not starved by a stale one elsewhere.
  TTL eviction keeps a quiet profile from holding descriptors forever. A pool miss is not an
  error: the caller opens as before.
* **Failure.** A handle that fails its probe is closed and never pooled; if the pool itself is
  unusable, callers fall back to the previous open-per-request path.
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("hermes_cli.web_server")

# Idle read-only attaches kept per database path. Small on purpose: dashboard requests are
# served by a handful of threadpool workers, and every idle handle holds a descriptor and the
# 8 MB page cache its initialization allocated.
_POOL_PER_PATH = 4
# Process-wide ceiling: a multiplexed gateway serves N profiles from one process, each with its
# own state.db, and descriptors are the scarce resource (#98573).
_POOL_PROCESS_MAX = 16
# An idle handle older than this is closed instead of reused: a profile the dashboard stopped
# polling must not keep a connection and its page cache alive indefinitely.
_POOL_IDLE_TTL_SECONDS = 120.0


class _PooledEntry:
    """One idle read-only handle plus the file identity it was opened against."""

    __slots__ = ("db", "identity", "idle_since")

    def __init__(self, db, identity, idle_since: float) -> None:
        self.db = db
        self.identity = identity
        self.idle_since = idle_since


def _handle_is_reusable(db) -> bool:
    """Whether a pooled read-only handle can serve another read right now.

    A handle parked in the pool can be killed while idle: ``SessionDB.close()`` (the test
    leak sweep, a shutdown sweep, an explicit close of a handle someone else wrapped) sets
    ``_conn = None``, and a structural-corruption read quarantines it with ``_db_corrupt``.
    Pooling either one hands the next request a read that fails on its first statement, so
    both are checked at BOTH ends — before returning a handle to the pool and before handing
    one out (it can die in between, while idle).
    """
    if db is None:
        return False
    if getattr(db, "_db_corrupt", False):
        return False
    return getattr(db, "_conn", None) is not None


class _ReadOnlyHandlePool:
    """Thread-safe bounded pool of read-only ``SessionDB`` handles, keyed by database path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[str, list[_PooledEntry]] = {}

    def _total_idle_locked(self) -> int:
        return sum(len(entries) for entries in self._idle.values())

    def _close_entry(self, entry: _PooledEntry) -> None:
        """Close a pooled handle; never raises (a failed close must not poison the pool)."""
        try:
            entry.db.close()
        except Exception:  # pragma: no cover - defensive: close() is best-effort by contract
            logger.debug("pooled read-only handle close failed", exc_info=True)

    def _pop_oldest_locked(self, exclude_key: Optional[str] = None) -> Optional[_PooledEntry]:
        """Remove and return the longest-idle entry process-wide (caller holds the lock)."""
        victim_key = None
        victim_since = None
        for candidate_key, entries in self._idle.items():
            if candidate_key == exclude_key or not entries:
                continue
            if victim_since is None or entries[0].idle_since < victim_since:
                victim_key, victim_since = candidate_key, entries[0].idle_since
        if victim_key is None:
            return None
        entries = self._idle[victim_key]
        entry = entries.pop(0)
        if not entries:
            self._idle.pop(victim_key, None)
        return entry

    def acquire(self, key: str, identity) -> Optional[object]:
        """A live idle handle for ``key`` with a matching file identity, or None.

        Expired handles are closed; a handle whose file identity moved is discarded (the path
        names a different file now, and a read must not answer from the old one). A handle that
        died while idle (its owner closed the SessionDB, or a read quarantined it as corrupt) is
        discarded too — handing it out would fail the caller's very first read.
        """
        now = time.monotonic()
        stale: list[_PooledEntry] = []
        picked: Optional[_PooledEntry] = None
        with self._lock:
            entries = self._idle.get(key)
            if entries:
                while entries:
                    entry = entries.pop(0)  # oldest first
                    if (now - entry.idle_since) > _POOL_IDLE_TTL_SECONDS:
                        stale.append(entry)
                        continue
                    if entry.identity != identity:
                        stale.append(entry)
                        continue
                    if not _handle_is_reusable(entry.db):
                        stale.append(entry)
                        continue
                    picked = entry
                    break
                if not entries:
                    self._idle.pop(key, None)
            # Counted INSIDE the lock: _idle is mutated under it by release()/_pop_oldest_locked()/
            # clear(), so walking it from a logging argument after the lock is a data race that
            # would raise between the pop above and the return below (losing the handle).
            idle_count = sum(len(group) for group in self._idle.values())
        for entry in stale:
            self._close_entry(entry)
        logger.debug("read-only handle pool %s for %s (idle=%d)",
                     "HIT" if picked is not None else "MISS", key, idle_count)
        if picked is None:
            return None
        try:
            return picked.db
        except BaseException:
            # Never strand a checked-out handle: whatever fails between the pop and the handoff,
            # the caller gets nothing and the pool must not keep (or leak) the entry.
            self._close_entry(picked)
            raise

    def release(self, key: str, db, identity) -> bool:
        """Return ``db`` to the pool; False when the handle is not reusable (caller closes it).

        At the per-path or process-wide ceiling the LONGEST-IDLE entry is evicted (LRU) to make
        room, so a busy profile is not starved of its own handles by a stale one elsewhere.
        Evicted handles are closed outside the lock: closing under it would serialize readers
        behind a checkpoint.
        """
        if not _handle_is_reusable(db):
            logger.debug("not pooling %s: handle is closed or quarantined", key)
            return False
        entry = _PooledEntry(db, identity, time.monotonic())
        evicted: list[_PooledEntry] = []
        with self._lock:
            entries = self._idle.setdefault(key, [])
            overflow = len(entries) >= _POOL_PER_PATH
            if overflow:
                evicted.append(entries.pop(0))
            elif self._total_idle_locked() >= _POOL_PROCESS_MAX:
                victim = self._pop_oldest_locked(exclude_key=key) or (entries.pop(0) if entries else None)
                if victim is None:
                    if not entries:
                        self._idle.pop(key, None)
                    return False
                evicted.append(victim)
            entries.append(entry)
        for old in evicted:
            self._close_entry(old)
        return True

    def discard(self, db) -> None:
        """Close a handle the pool must not reuse (failed probe, closed by its owner)."""
        try:
            db.close()
        except Exception:  # pragma: no cover
            logger.debug("discarded read-only handle close failed", exc_info=True)

    def clear(self) -> None:
        """Close every idle handle (shutdown / test isolation)."""
        with self._lock:
            entries = [entry for group in self._idle.values() for entry in group]
            self._idle = {}
        for entry in entries:
            self._close_entry(entry)

    def drop_path(self, key: str) -> None:
        """Close every idle handle for one path: raw file access about to claim exclusive use."""
        with self._lock:
            entries = self._idle.pop(key, [])
        for entry in entries:
            self._close_entry(entry)


_pool = _ReadOnlyHandlePool()


def get_pool() -> _ReadOnlyHandlePool:
    """The process-wide read-only handle pool (tests may clear it)."""
    return _pool


class _PooledHandle:
    """A ``SessionDB`` read-only attach whose ``close()`` RETURNS it to the pool.

    Delegates everything to the wrapped handle so existing callers (routers, helpers that read
    ``db._conn``/``db.db_path``) work unchanged. After ``close()`` the wrapper is inert: a later
    attribute access raises ``sqlite3.ProgrammingError``, exactly like the closed connection it
    replaces — the delegate may already be serving another request, so silence there would be a
    cross-request data race rather than a bug report.
    """

    __slots__ = ("_closed", "_db", "_identity", "_pool_key")

    def __init__(self, db, pool_key: str, identity) -> None:
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_pool_key", pool_key)
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_closed", False)

    def __getattr__(self, name):
        db = object.__getattribute__(self, "_db")
        if object.__getattribute__(self, "_closed"):
            raise _closed_error(name)
        return getattr(db, name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_db"), name, value)

    def close(self) -> None:
        """Return the handle to the pool (or close it when the pool is full); safe to call twice."""
        if object.__getattribute__(self, "_closed"):
            return
        object.__setattr__(self, "_closed", True)
        db = object.__getattribute__(self, "_db")
        pool = get_pool()
        if not pool.release(object.__getattribute__(self, "_pool_key"), db,
                            object.__getattribute__(self, "_identity")):
            pool.discard(db)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _closed_error(name: str):
    import sqlite3

    return sqlite3.ProgrammingError(
        f"pooled read-only SessionDB handle is closed (accessed {name!r}); "
        "open a new one through _open_session_db_for_profile")
