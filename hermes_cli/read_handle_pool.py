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
  read pool already respects, ``_READ_POOL_MAX`` / ``_READ_POOL_PROCESS_MAX``), with TTL
  eviction so a quiet profile does not hold descriptors forever. A pool miss is not an error:
  the caller opens as before.
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

    def _evict_locked(self, key: str) -> None:
        """Drop the oldest idle handle for ``key`` (caller holds the lock)."""
        entries = self._idle.get(key)
        if not entries:
            return
        entry = entries.pop(0)
        if not entries:
            self._idle.pop(key, None)
        # Closed outside the lock in acquire(); closing under the lock would serialize readers
        # behind a checkpoint. Callers pass the entry to _close_entry after releasing.
        threading.Thread(target=self._close_entry, args=(entry,), daemon=True).start()

    def acquire(self, key: str, identity) -> Optional[object]:
        """A live idle handle for ``key`` with a matching file identity, or None.

        Expired handles are closed; a handle whose file identity moved is discarded (the path
        names a different file now, and a read must not answer from the old one).
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
                    picked = entry
                    break
                if not entries:
                    self._idle.pop(key, None)
        for entry in stale:
            self._close_entry(entry)
        return picked.db if picked is not None else None

    def release(self, key: str, db, identity) -> bool:
        """Return ``db`` to the pool; False when the pool is full (caller closes it)."""
        entry = _PooledEntry(db, identity, time.monotonic())
        with self._lock:
            entries = self._idle.setdefault(key, [])
            if len(entries) >= _POOL_PER_PATH or self._total_idle_locked() >= _POOL_PROCESS_MAX:
                if not entries:
                    self._idle.pop(key, None)
                return False
            entries.append(entry)
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

    __slots__ = ("_db", "_pool_key", "_identity", "_closed")

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
