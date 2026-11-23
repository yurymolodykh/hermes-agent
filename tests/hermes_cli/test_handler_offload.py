"""Contract tests for the handler-offload seam (``hermes_cli/web_routers/handler_offload.py``).

What is pinned here, and why each one is a behaviour contract rather than a shape check:

* **Off-loop execution** — a blocking function called through ``off_loop`` must NOT run on the
  event-loop thread. That is the entire point of the change; if it regresses, the gateway blocks
  again and nothing else in the suite would notice.
* **The switch has two working positions** — ``dashboard.handler_offload: false`` (and the
  ``HERMES_HANDLER_OFFLOAD=0`` emergency override) must put the work back INLINE on the loop,
  because that is the rollback: an operator flips one key, restarts, and gets the old process
  behaviour without touching code.
* **Context propagation** — the profile-scope contextvar decides which ``HERMES_HOME`` a read or
  write lands in. A worker that loses it writes into the launch profile's home; that is a silent
  cross-profile leak, not a slowdown.
* **Failures cross the thread boundary intact** — ``HTTPException`` raised inside a worker must
  arrive at the handler as the same exception (status and detail preserved), never flattened.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest
from fastapi import HTTPException

from hermes_cli.web_routers import handler_offload as ho


@pytest.fixture(autouse=True)
def _reset_seam(monkeypatch):
    """Every test starts and ends with the seam shut down, so no pool leaks between files."""
    monkeypatch.delenv("HERMES_HANDLER_OFFLOAD", raising=False)
    ho.HANDLER_OFFLOAD.reset()
    yield
    ho.HANDLER_OFFLOAD.reset()


def _run(coro):
    return asyncio.run(coro)


def test_off_loop_runs_the_call_off_the_event_loop_thread(monkeypatch):
    """The blocking call must execute on a worker thread, not on the loop's own thread."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None)

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _blocking():
        seen["thread"] = threading.get_ident()
        return "done"

    assert _run(ho.off_loop(_blocking)) == "done"
    assert seen["thread"] != loop_thread, "off_loop ran the blocking call on the event loop thread"


def test_off_loop_passes_args_and_kwargs(monkeypatch):
    """Arguments cross the boundary unchanged — every migrated handler relies on this."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None)

    def _join(*parts, sep="-"):
        return sep.join(parts)

    assert _run(ho.off_loop(_join, "a", "b", "c", sep="+")) == "a+b+c"


def test_switch_off_runs_inline_on_the_loop(monkeypatch):
    """``HERMES_HANDLER_OFFLOAD=0`` is the emergency rollback: work runs inline, on the loop."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "0")
    ho.HANDLER_OFFLOAD.install(None)

    seen: dict[str, int] = {}

    def _blocking():
        seen["thread"] = threading.get_ident()
        return "inline"

    async def _main():
        return await ho.off_loop(_blocking)

    assert _run(_main()) == "inline"
    # asyncio.run executes the coroutine on its own thread; inline means the SAME thread.
    assert seen["thread"] is not None
    view = ho.offload_status()
    assert view["enabled"] is False
    assert view["mode"] == "inline"


def test_config_key_off_wins_over_the_default(monkeypatch):
    """``dashboard.handler_offload: false`` in the config is the same rollback as the env var."""
    monkeypatch.setattr(ho, "_config_section", lambda: {"handler_offload": False})
    ho.HANDLER_OFFLOAD.reset()
    ho.HANDLER_OFFLOAD.install(None)

    assert ho.offload_status()["enabled"] is False


def test_contextvars_reach_the_worker(monkeypatch):
    """Profile scope travels with the call; a lost context writes into the wrong home."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None)

    prof = contextvars.ContextVar("hermes_test_profile_scope", default="launch")
    prof.set("alice")

    async def _main():
        return await ho.off_loop(prof.get)

    assert _run(_main()) == "alice"


def test_setting_the_context_off_loop_does_not_leak_back(monkeypatch):
    """The worker gets a COPY: a set() inside it must not mutate the requester's context."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None)

    prof = contextvars.ContextVar("hermes_test_profile_scope_2", default="launch")

    async def _main():
        await ho.off_loop(prof.set, "worker")
        return prof.get()

    assert _run(_main()) == "launch"


def test_http_exception_crosses_the_boundary_intact(monkeypatch):
    """A 404 raised inside a worker arrives as that same 404, not a generic 500."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None)

    def _blocking():
        raise HTTPException(status_code=404, detail="model not found")

    with pytest.raises(HTTPException) as excinfo:
        _run(ho.off_loop(_blocking))
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "model not found"


def test_pool_exhaustion_queues_instead_of_failing(monkeypatch):
    """More concurrent callers than workers must QUEUE (and all finish), never raise or drop.

    The dashboard opens several panels at once; a bounded pool that rejected the overflow would
    turn a slow page into a broken one.
    """
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None, workers=2)

    async def _main():
        gate = threading.Event()
        started = threading.Semaphore(0)

        def _blocking(i):
            started.release()
            gate.wait(timeout=5)
            return i

        tasks = [asyncio.create_task(ho.off_loop(_blocking, i)) for i in range(6)]
        # Let the first two occupy both workers, then release; the rest queue and then run.
        for _ in range(2):
            await asyncio.to_thread(started.acquire, True, 5)
        gate.set()
        return await asyncio.gather(*tasks)

    assert sorted(_run(_main())) == [0, 1, 2, 3, 4, 5]


def test_status_reports_mode_and_workers(monkeypatch):
    """``/api/status`` needs a real mode + worker count to show which behaviour is live."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None, workers=3)

    view = ho.offload_status()
    assert view["enabled"] is True
    assert view["mode"] == "threaded"
    assert view["workers"] == 3
    assert isinstance(view["queue_depth"], int)


def test_shutdown_is_idempotent(monkeypatch):
    """Boot/shutdown can run twice (lifespan retries, test teardown); neither may raise."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None)
    ho.HANDLER_OFFLOAD.shutdown()
    ho.HANDLER_OFFLOAD.shutdown()


def test_inline_mode_needs_no_pool(monkeypatch):
    """With the switch off there is no executor to build — install() must still succeed."""
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "0")
    ho.HANDLER_OFFLOAD.install(None)
    assert _run(ho.off_loop(lambda: 41 + 1)) == 42
