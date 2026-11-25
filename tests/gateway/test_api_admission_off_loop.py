"""API admission never blocks the shared owner loop on a held SQLite writer (every HTTP door).

The measurement is relative, not a wall-clock ceiling: the loop's gap distribution while an
admission waits on a held writer is compared with the distribution the same loop produces on an
idle control window, and the comparison uses the median of several repetitions' p95. Foreign load
on the host inflates both sides, so a busy machine shifts the bound instead of failing the test —
but an admission that blocks the loop produces gaps an order of magnitude above the control, which
no amount of noise can hide. The first, request-path-warming round is paid before any measurement:
imports and the first sqlite open are a boot cost, not a per-admission stall.
"""
import asyncio
import sqlite3
import threading
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

KEY = 'off-loop-admission-key-long-enough'
HOLD_S = 1.5
POLL_S = 0.02
REPETITIONS = 3
# The admission's wait is only evidence while the writer really is held: a request that returned
# early never met the contention this test is about.
MIN_WAIT_S = HOLD_S * 0.5
# Loop noise is whatever the control window measured; a blocked admission is ~HOLD_S, far above.
NOISE_FACTOR = 6.0
NOISE_FLOOR_S = 0.25
AUTH = {'Authorization': 'Bearer ' + KEY}

DOORS = [
    ('chat', '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]}, {}, 200),
    ('responses', '/v1/responses', {'input': 'hi'}, {'Idempotency-Key': 'resp-1'}, 200),
    ('runs', '/v1/runs', {'input': 'hi'}, {}, 202),
]


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


async def _sample_gaps(seconds):
    """Owner-loop tick gaps over a window with no request in flight — the host's own noise."""
    gaps, last = [], time.monotonic()
    deadline = last + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        now = time.monotonic()
        gaps.append(now - last)
        last = now
    return gaps


async def _hold_writer(db_path, seconds):
    """Another process holds the profile's state.db writer; the timer releases it after *seconds*.

    The writer may still be held by the previous round's worker for a moment, so taking it is a
    bounded retry rather than a single attempt with ``timeout=0``.
    """
    holder = sqlite3.connect(db_path, timeout=0, isolation_level=None, check_same_thread=False)
    deadline = time.monotonic() + 10.0
    while True:
        try:
            holder.execute('BEGIN IMMEDIATE')
            break
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                holder.close()
                raise
            await asyncio.sleep(0.02)
    timer = threading.Timer(seconds, lambda: holder.execute('COMMIT'))
    timer.start()
    return holder, timer


async def _request_gaps(client, path, body, headers):
    """Tick gaps of the owner loop while one request waits on the held writer."""
    started = time.monotonic()
    request = asyncio.create_task(client.post(path, json=body, headers={**AUTH, **headers}))
    gaps, last = [], time.monotonic()
    while not request.done():
        await asyncio.sleep(POLL_S)
        now = time.monotonic()
        gaps.append(now - last)
        last = now
    return await request, gaps, time.monotonic() - started


@pytest.mark.asyncio
@pytest.mark.parametrize('name,path,body,headers,expected', DOORS, ids=[d[0] for d in DOORS])
async def test_admission_waits_for_the_writer_off_the_owner_loop(api, owner, monkeypatch, name, path, body,
                                                                headers, expected):
    from gateway.platforms import api_server_runs

    async def handle(event):
        from gateway.session_results import execution_result
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    async def no_execution(*args, **kwargs):
        pass
    owner.runner._handle_message = handle
    monkeypatch.setattr(api_server_runs, '_execute_run', no_execution)
    api._api_key = KEY
    app = web.Application()
    app.router.add_post(path, getattr(api, {'chat': '_handle_chat_completions', 'responses': '_handle_responses',
                                            'runs': '_handle_runs'}[name]))
    async with TestClient(TestServer(app)) as client:
        # Boot cost paid up front, unheld: imports, the profile's first sqlite open and the route's
        # one-time setup. A real server pays this once at first use; it is not an admission stall.
        warmup = await client.post(path, json=body, headers={**AUTH, **headers})
        assert warmup.status == expected, await warmup.text()

        # The same loop, the same sampling, no writer held: this IS the machine's noise level.
        control = await _sample_gaps(HOLD_S)
        noise = _percentile(control, 0.95)

        p95s, waits = [], []
        for _ in range(REPETITIONS):
            holder, timer = await _hold_writer(owner.db.db_path, HOLD_S)
            try:
                response, gaps, waited = await _request_gaps(client, path, body, headers)
            finally:
                timer.join()
                holder.close()
            assert response.status == expected, await response.text()
            assert waited >= MIN_WAIT_S, (
                f'{name}: the admission returned after {waited:.2f}s, so the writer was never held '
                'long enough for this measurement to mean anything')
            p95s.append(_percentile(gaps, 0.95))
            waits.append(waited)
        stalled = _median(p95s)
    # The bound follows the loop's own measured noise, so neighbouring load moves it instead of
    # breaking the test; an admission on the loop would stall for the whole hold and blow past it.
    ceiling = max(noise * NOISE_FACTOR, noise + NOISE_FLOOR_S)
    assert stalled <= ceiling, (
        f'{name}: owner loop stalled {stalled:.3f}s behind the admission write '
        f'(median p95 of {REPETITIONS} holds; control noise {noise:.3f}s, ceiling {ceiling:.3f}s)')
