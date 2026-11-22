"""TTL + single-flight cache for one ``/api/status`` snapshot.

``/api/status`` is the readiness indicator the desktop app and the dashboards poll about once a
second, and every uncached pass runs a pile of blocking reads (config re-parse, spawn ledger,
state.db probes, disk/memory rollups, topology walk) through the shared thread pool. The API is
served by one process on one event loop, so eight simultaneous polls used to run eight full
passes that queued behind each other — each took ~2 s, the indicator sat in "checking" and a chat
open (6-10 requests) stretched for seconds.

Two properties make the probe cheap without changing what it reports:

* **TTL** — a snapshot younger than ``ttl`` seconds is served as-is. The TTL is the hard bound on
  how long a change can hide, so it stays well under the 2 s a client tolerates before the
  gateway state flips in the UI.
* **Single flight** — concurrent callers inside one window share ONE computation instead of
  stampeding the moment the entry expires.

Staleness is bounded by more than the clock: callers pass a ``signature`` — cheap facts (file
stamps of the gateway identity/runtime files, the identity of each injected seam) that change
whenever the answer would. A snapshot is reusable only while its signature still matches, so a
gateway restart, a runtime-state transition or a swapped test seam drops it immediately instead
of waiting out the TTL.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Hashable, Optional

__all__ = ["TtlSingleFlightCache", "STATUS_CACHE_TTL_SECONDS", "STATUS_CACHE_MAX_ENTRIES"]

#: 1.5 s: inside the 2 s a client tolerates after a gateway restart, and long enough that a 1/s
#: poller hits the cache on every tick instead of recomputing the whole probe.
STATUS_CACHE_TTL_SECONDS = 1.5
#: One entry per requested profile (plus the unscoped/signed-out shapes); the cap only guards
#: against a client walking arbitrary ``?profile=`` names.
STATUS_CACHE_MAX_ENTRIES = 64


@dataclass(frozen=True)
class _Entry:
    stored_at: float
    signature: Any
    value: Any


class TtlSingleFlightCache:
    """Snapshot cache keyed on the call shape; concurrent misses collapse into one computation."""

    def __init__(self, *, ttl_seconds: float = STATUS_CACHE_TTL_SECONDS,
                 max_entries: int = STATUS_CACHE_MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[Hashable, _Entry] = {}
        self._inflight: dict[Hashable, asyncio.Task] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    async def get_or_compute(
        self,
        key: Hashable,
        factory: Callable[[], Awaitable[Any]],
        *,
        signature: Optional[Callable[[], Any]] = None,
    ) -> Any:
        """Cached value for ``key``, or one shared computation that every waiter receives."""
        cached = self._entries.get(key)
        if cached is not None and self._fresh(cached, signature):
            return cached.value

        loop = asyncio.get_running_loop()
        task = self._inflight.get(key)
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(self._compute(key, factory, signature))
            self._inflight[key] = task
            task.add_done_callback(lambda done, key=key: self._settle(key, done))
        # A caller that walks away (client disconnect, timeout) must not cancel the computation
        # the remaining waiters are on.
        return await asyncio.shield(task)

    def clear(self) -> None:
        """Drop every snapshot (a caller that already knows the world changed)."""
        self._entries.clear()

    def _fresh(self, entry: _Entry, signature: Optional[Callable[[], Any]]) -> bool:
        if self._clock() - entry.stored_at > self._ttl:
            return False
        return signature is None or entry.signature == signature()

    async def _compute(self, key: Hashable, factory: Callable[[], Awaitable[Any]],
                       signature: Optional[Callable[[], Any]]) -> Any:
        value = await factory()
        if key not in self._entries and len(self._entries) >= self._max_entries:
            for stale in sorted(self._entries, key=lambda k: self._entries[k].stored_at)[
                    : max(1, self._max_entries // 4)]:
                self._entries.pop(stale, None)
        self._entries[key] = _Entry(self._clock(), signature() if signature is not None else None, value)
        return value

    def _settle(self, key: Hashable, task: asyncio.Task) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            # Retrieve the outcome so a flight nobody is awaiting does not log "never retrieved".
            task.exception()
