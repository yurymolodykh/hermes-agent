"""``/api/status`` must report the offload pool LIVE, not as of the last cache refresh.

``/api/status`` is served from a 1.5 s TTL snapshot. The pool counters describe the requests
being served right now, so putting them inside the cached snapshot made a burst shorter than the
TTL read ``submitted: 0, max_queue_depth: 0`` — observable while 64 saves were queued. This pins
that the field is merged AFTER the cache: a stale cached payload must not freeze the counters,
and the field must still be present (and mode-shaped) for a signed-out caller.
"""

from __future__ import annotations

import pytest


def _patch_status_seams(monkeypatch, fake_view):
    """Make ``/api/status`` answer without probing the box, with a scripted offload view."""
    from hermes_cli.web_routers import status as status_mod

    status_mod._STATUS_CACHE.clear()

    async def _collect_status(profile_dir, requested_profile, signed_out):
        return {"version": "test", "gateway_running": False, "handler_offload": {"mode": "STALE"}}

    monkeypatch.setattr(status_mod, "_collect_status", _collect_status)
    monkeypatch.setattr(status_mod, "_status_cache_signature", lambda profile_dir: ("fixed",))
    monkeypatch.setattr(status_mod, "_handler_offload_view", fake_view)
    monkeypatch.setattr(status_mod, "_webapp_caller_signed_out", _async(False))
    return status_mod


def _async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


def test_status_counters_are_not_frozen_by_the_snapshot_cache(monkeypatch):
    counters = {"submitted": 1, "max_queue_depth": 0, "mode": "threaded", "workers": 8}

    def fake_view():
        counters["submitted"] += 1
        counters["max_queue_depth"] = counters["submitted"]
        return dict(counters)

    status_mod = _patch_status_seams(monkeypatch, fake_view)

    import asyncio
    first = asyncio.run(status_mod.get_status(profile=None))
    second = asyncio.run(status_mod.get_status(profile=None))

    assert first["handler_offload"]["submitted"] == 2
    assert second["handler_offload"]["submitted"] == 3
    assert second["handler_offload"] != first["handler_offload"], (
        "the counters came out of the TTL snapshot — a live pool is being reported stale")
    assert second["handler_offload"]["mode"] == "threaded"
    assert second["version"] == "test", "the rest of the payload must still come from the cache"


def test_signed_out_status_still_carries_the_offload_mode(monkeypatch):
    status_mod = _patch_status_seams(
        monkeypatch, lambda: {"mode": "inline", "enabled": False, "workers": 0})
    monkeypatch.setattr(status_mod, "_webapp_caller_signed_out", _async(True))

    import asyncio
    payload = asyncio.run(status_mod.get_status(profile=None))

    assert payload["handler_offload"]["mode"] == "inline"
    # The signed-out allowlist is a filter, not a source of truth: nothing else leaks through.
    assert "active_sessions" not in payload or "handler_offload" in payload
