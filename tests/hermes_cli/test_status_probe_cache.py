"""``/api/status`` is a cheap, collapsed readiness probe.

Measured on the live gateway 2026-11-22: one probe cost 130-270 ms and eight simultaneous probes
took 2.0-2.1 s EACH — the HTTP layer serves one process on one loop, and every uncached pass runs
several blocking reads. The desktop polls this endpoint ~1/s for its state indicator and fires a
batch of them when a chat opens, so the queue was what kept the indicator in "checking".

The contract these tests hold the endpoint to:

* concurrent polls inside one window share ONE computation (single flight), so the queue is gone;
* a gateway restart / runtime-state transition is visible on the NEXT poll (well inside the 2 s the
  desktop tolerates), not one TTL later;
* the payload itself keeps every field and every gate it had before the cache.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest


@pytest.fixture
def status_router():
    from hermes_cli.web_routers import status as status_router

    status_router._STATUS_CACHE.clear()
    yield status_router
    status_router._STATUS_CACHE.clear()


def _record(**overrides) -> dict:
    record = {"gateway_state": "running", "pid": 1234, "start_time": 111.0,
              "updated_at": "2026-11-22T10:00:00+00:00", "platforms": {}, "active_agents": 0}
    record.update(overrides)
    return record


def _seams(monkeypatch, router, *, record: dict, pid: int | None = 1234):
    """Pin the cheap reads the probe and its cache signature share, without touching the disk."""
    import gateway.status as gw

    monkeypatch.setattr(gw, "get_running_pid_cached", lambda *a, **k: pid)
    monkeypatch.setattr(gw, "read_runtime_status", lambda *a, **k: record)
    monkeypatch.setattr(router, "read_runtime_status", lambda *a, **k: record, raising=False)
    monkeypatch.setattr(router, "get_running_pid_cached", lambda *a, **k: pid, raising=False)


async def _get_status(router):
    return await router.get_status(None, None)


def test_concurrent_probes_share_one_computation(status_router, monkeypatch):
    """Eight simultaneous polls must run the expensive pass ONCE (single flight)."""
    calls = []

    async def fake_collect(profile_dir, requested_profile, signed_out):
        calls.append(1)
        await asyncio.sleep(0.05)
        return {"version": "test", "gateway_running": True, "gateway_state": "running"}

    monkeypatch.setattr(status_router, "_collect_status", fake_collect)

    async def scenario():
        return await asyncio.gather(*(_get_status(status_router) for _ in range(8)))

    bodies = asyncio.run(scenario())

    assert len(calls) == 1, f"single flight collapsed to {len(calls)} computations, expected 1"
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["gateway_running"] is True


def test_snapshot_is_reused_inside_the_ttl_and_recomputed_after(status_router, monkeypatch):
    calls = []

    async def fake_collect(profile_dir, requested_profile, signed_out):
        calls.append(time.monotonic())
        return {"version": "test", "n": len(calls)}

    monkeypatch.setattr(status_router, "_collect_status", fake_collect)

    async def scenario():
        first = await _get_status(status_router)
        second = await _get_status(status_router)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second, "a poll inside the TTL must not recompute"
    assert len(calls) == 1

    # Age the entry past the TTL instead of sleeping through it.
    entry = next(iter(status_router._STATUS_CACHE._entries.values()))
    status_router._STATUS_CACHE._entries[next(iter(status_router._STATUS_CACHE._entries))] = type(entry)(
        entry.stored_at - status_router._STATUS_CACHE.ttl_seconds - 1.0, entry.signature, entry.value)

    third = asyncio.run(_get_status(status_router))
    assert third["n"] == 2
    assert len(calls) == 2


def test_gateway_restart_reaches_the_probe_on_the_next_poll(status_router, monkeypatch):
    """The 2 s guarantee: a restart flips ``running`` without waiting out the TTL.

    The cache signature carries the live liveness verdict (PID probe + runtime record), so the
    first poll after the gateway comes up reports it — the snapshot from before the restart is
    dropped, not served stale.
    """
    import gateway.status as gw

    state = {"pid": None, "record": _record(gateway_state="stopped")}
    calls = []

    async def fake_collect(profile_dir, requested_profile, signed_out):
        calls.append(1)
        running = state["pid"] is not None
        return {"gateway_running": running,
                "gateway_state": state["record"].get("gateway_state") if running else "stopped"}

    monkeypatch.setattr(gw, "get_running_pid_cached", lambda *a, **k: state["pid"])
    monkeypatch.setattr(gw, "read_runtime_status", lambda *a, **k: state["record"])
    monkeypatch.setattr(status_router, "_collect_status", fake_collect)

    async def scenario():
        before = await _get_status(status_router)
        # The gateway starts: PID file appears and the runtime record says running.
        state["pid"] = 4321
        state["record"] = dict(state["record"], gateway_state="running", pid=4321)
        after = await _get_status(status_router)
        return before, after

    before, after = asyncio.run(scenario())
    assert before["gateway_running"] is False
    assert after["gateway_running"] is True
    assert after["gateway_state"] == "running"
    assert len(calls) == 2, "a liveness change must force a recomputation, not serve the old snapshot"


def test_runtime_transition_to_draining_reaches_the_probe_immediately(status_router, monkeypatch):
    """Same guarantee for a state change inside a live gateway (running -> draining)."""
    import gateway.status as gw

    record = {"record": _record(gateway_state="running")}
    monkeypatch.setattr(gw, "get_running_pid_cached", lambda *a, **k: 1234)
    monkeypatch.setattr(gw, "read_runtime_status", lambda *a, **k: record["record"])

    async def fake_collect(profile_dir, requested_profile, signed_out):
        return {"gateway_running": True, "gateway_state": record["record"]["gateway_state"]}

    monkeypatch.setattr(status_router, "_collect_status", fake_collect)

    async def scenario():
        first = await _get_status(status_router)
        record["record"] = _record(gateway_state="draining")
        second = await _get_status(status_router)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["gateway_state"] == "running"
    assert second["gateway_state"] == "draining"


def test_signed_out_payload_stays_trimmed(status_router, monkeypatch):
    """The cache stores the FULL body; the signed-out trim still applies per request."""
    async def fake_collect(profile_dir, requested_profile, signed_out):
        return {"version": "test", "gateway_running": True, "hermes_home": "/secret", "install_id": "abc"}

    monkeypatch.setattr(status_router, "_collect_status", fake_collect)
    monkeypatch.setattr(status_router, "_webapp_caller_signed_out", lambda request: _async(True))

    body = asyncio.run(_get_status(status_router))
    assert body["version"] == "test"
    assert "hermes_home" not in body and "install_id" not in body


def test_cache_key_separates_requested_profiles(status_router, monkeypatch):
    """One entry per requested profile so ``?profile=x`` cannot serve another profile's body."""
    seen = []

    async def fake_collect(profile_dir, requested_profile, signed_out):
        seen.append(requested_profile)
        return {"profile": requested_profile}

    monkeypatch.setattr(status_router, "_collect_status", fake_collect)
    monkeypatch.setattr(status_router, "_resolve_profile_dir", lambda name: None)
    monkeypatch.setattr(status_router, "_config_profile_scope", lambda name: _NullScope())

    async def scenario():
        return [await status_router.get_status(name, None) for name in ("alpha", "beta", "alpha")]

    bodies = asyncio.run(scenario())
    assert [body["profile"] for body in bodies] == ["alpha", "beta", "alpha"]
    assert seen == ["alpha", "beta"], "the second alpha poll must come from the cache"


class _NullScope:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _async(value):
    async def _inner():
        return value

    return _inner()


def test_warm_probe_is_fast_and_concurrent_probes_do_not_queue(status_router, monkeypatch):
    """Size check against the measured regression: warm single call ≤50 ms, 8 parallel ≤200 ms each."""
    from starlette.testclient import TestClient

    from hermes_cli import web_server

    import gateway.status as gw

    monkeypatch.setattr(gw, "get_running_pid_cached", lambda *a, **k: 1234)
    monkeypatch.setattr(gw, "read_runtime_status", lambda *a, **k: _record())

    client = TestClient(web_server.app)
    for _ in range(3):  # warm the one-time imports the first probe pays
        assert client.get("/api/status").status_code == 200

    started = time.perf_counter()
    for _ in range(5):
        assert client.get("/api/status").status_code == 200
    single_ms = (time.perf_counter() - started) / 5 * 1000
    assert single_ms <= 50.0, f"warm single probe took {single_ms:.1f} ms (budget 50 ms)"

    import concurrent.futures

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.get("/api/status").status_code, range(8)))
    wall_ms = (time.perf_counter() - started) * 1000
    assert codes == [200] * 8
    assert wall_ms <= 200.0 * 8 / 8 + 200.0, f"8 parallel probes took {wall_ms:.0f} ms of wall time"
    assert wall_ms <= 1600.0, (
        f"8 parallel probes queued for {wall_ms:.0f} ms of wall time; "
        "before the cache they took ~2 s EACH")
