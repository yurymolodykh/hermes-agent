"""The descriptor-headroom guard must not walk ``/proc/self/fd`` on the read path.

Every pooled read-connection checkout used to list the process's fd directory to decide
whether it could spare a descriptor. On the live gateway that directory held ~710 entries
and listing it cost 314 of 2049 py-spy samples (15% of process time) — for a number that
only needs to be right to within the reserved headroom.

The guard's BEHAVIOUR is unchanged: refuse BEFORE opening when the table is measurably
full, fail open when it cannot be measured. Only the measuring moved to a background
sampler.
"""

from __future__ import annotations

import os

import pytest

import hermes_state_readpool as readpool


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    """Every test starts from a cold sample and a stopped sampler."""
    readpool.stop_fd_reconciler()
    monkeypatch.setattr(readpool, "_fd_usage_cache", (0.0, None))
    monkeypatch.setattr(readpool, "_fd_pool_live", 0)
    monkeypatch.setattr(readpool, "_fd_pool_live_at_probe", 0)
    yield
    readpool.stop_fd_reconciler()


def test_headroom_check_does_not_list_the_fd_directory(monkeypatch):
    """With a fresh sample in hand, the check is pure arithmetic — no syscall at all."""
    listings = []

    def counting_listdir(path="."):
        listings.append(path)
        return os.listdir(path)

    monkeypatch.setattr(readpool.os, "listdir", counting_listdir)
    monkeypatch.setattr(readpool, "_fd_soft_limit", lambda: 1024)
    monkeypatch.setattr(readpool, "_open_fd_count", lambda: 100)
    assert readpool._fd_headroom_ok() is True  # cold sample: this call may measure once

    listings.clear()
    for _ in range(50):
        assert readpool._fd_headroom_ok() is True
    assert listings == [], f"the request path listed {len(listings)} director(ies): {listings[:3]}"


def test_background_sampler_keeps_the_estimate_fresh(monkeypatch):
    """The estimate tracks the published sample rather than a value frozen at start-up."""
    monkeypatch.setattr(readpool, "_fd_soft_limit", lambda: 1024)
    monkeypatch.setattr(readpool, "_open_fd_count", lambda: 100)
    assert readpool._fd_headroom_ok() is True

    monkeypatch.setattr(readpool, "_open_fd_count", lambda: 900)
    readpool._sample_fd_usage()
    assert readpool._fd_headroom_ok() is True  # 1024 - 900 = 124 > 64

    monkeypatch.setattr(readpool, "_open_fd_count", lambda: 1000)
    readpool._sample_fd_usage()
    assert readpool._fd_headroom_ok() is False, "100 fds left is inside the 64-fd reserve"


def test_shortfall_still_refuses_before_opening(monkeypatch):
    """The refusal happens on the decision, and leaves the counter the runbook reads."""
    before = readpool._read_open_denied_fd_headroom
    monkeypatch.setattr(readpool, "_fd_soft_limit", lambda: 256)
    monkeypatch.setattr(readpool, "_open_fd_count", lambda: 250)

    budget = readpool._PathReadBudget()
    assert budget.acquire(None) is False, "a permit was granted with 6 fds left"
    assert readpool._read_open_denied_fd_headroom == before + 1, (
        "the guard refused without leaving a trace to diagnose it from")


def test_fail_open_is_preserved(monkeypatch):
    """Unmeasurable is not tight: no RLIMIT (Windows) and an unreadable probe both fail OPEN."""
    monkeypatch.setattr(readpool, "_fd_soft_limit", lambda: None)
    assert readpool._fd_headroom_ok() is True

    monkeypatch.setattr(readpool, "_fd_soft_limit", lambda: 256)
    monkeypatch.setattr(readpool, "_open_fd_count", lambda: None)
    assert readpool._fd_headroom_ok() is True


def test_pool_growth_since_the_sample_is_counted(monkeypatch):
    """Connections this module opened AFTER the sample are subtracted, not ignored."""
    monkeypatch.setattr(readpool, "_fd_soft_limit", lambda: 1024)
    monkeypatch.setattr(readpool, "_open_fd_count", lambda: 1000)  # 24 left, inside the reserve
    assert readpool._fd_headroom_ok() is False

    # A sample taken when the pool had 0 live connections, then 3 grown since.
    readpool._sample_fd_usage()
    monkeypatch.setattr(readpool, "_open_fd_count", lambda: 900)  # 124 left, roomy
    readpool._sample_fd_usage()
    assert readpool._fd_headroom_ok() is True
    for _ in range(3):
        readpool._note_fd_delta(1)
    assert readpool._fd_headroom_ok() is True  # 124 - 3 still above the reserve
    for _ in range(3):
        readpool._note_fd_delta(-1)
    assert readpool._fd_headroom_ok() is True


def test_reconciler_thread_starts_once_and_stops(monkeypatch):
    """Lazy single start: a process that never reads pays nothing, and the stop is clean."""
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            started.append(name)
            self.target, self.args = target, args

        def start(self):
            pass  # never runs the loop: this test is about the registry, not the sample

    monkeypatch.setattr(readpool.threading, "Thread", FakeThread)
    monkeypatch.setattr(readpool, "_fd_reconciler_started", False)
    readpool._fd_reconciler_stop_event.clear()

    assert readpool.ensure_fd_reconciler() is True
    assert readpool.ensure_fd_reconciler() is True
    assert started == ["readpool-fd-reconciler"], f"started {len(started)} samplers"

    readpool.stop_fd_reconciler()
    assert readpool._fd_reconciler_started is False
    assert not readpool._fd_reconciler_stop_event
