"""The event-loop blockers removed for web-UI latency (card t_35e39725).

One test per point, each asserting the property the fix exists for — not that a helper was called:

1. ``WSTransport``: a peer that stops reading parks only its own backlog. The loop must keep
   running (a stalled socket used to hold ``_send_lock`` across ``send_text``, parking every
   producer on the loop), order must be preserved, and the backlog must be bounded.
2. ``SessionAuthority.receipt``: the SQLite admission read runs off the loop thread, so an RPC
   keeps answering while the socket writer is blocked.
3. ``build_profile_secret_scope``: a repeat build does not resolve the home path again, and a
   home that is a symlink still compares correctly (the memo cannot break identity).
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway.ws import WSTransport


class _StalledSocket:
    """A peer that accepts frames but never acknowledges them: ``send_text`` parks until released."""

    def __init__(self):
        self.sent = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_text(self, text):
        self.sent.append(text)
        self.entered.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_stalled_socket_does_not_park_the_loop():
    """A blocked writer must not stop the loop from running other work."""
    socket = _StalledSocket()
    transport = WSTransport(socket, asyncio.get_running_loop())
    try:
        stalled = asyncio.create_task(transport._safe_send_many(["first"]))
        await asyncio.wait_for(socket.entered.wait(), timeout=5)

        # The writer is parked mid-frame. The loop must still schedule: this is the regression the
        # contract is about (a lock held across send_text parked every producer on the loop).
        ticks = 0
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            ticks += 1
        assert ticks > 10, f"loop starved while the socket was stalled (ticks={ticks})"
        assert not stalled.done()

        # A second producer enqueues behind the stalled one: it must not be inside the write, and it
        # must not resolve early.
        second = asyncio.create_task(transport._safe_send_many(["second"]))
        await asyncio.sleep(0.05)
        assert not second.done(), "second batch resolved before the first left the wire"
        assert socket.sent == ["first"], "second frame overtook the stalled first frame"

        socket.release.set()
        await asyncio.wait_for(asyncio.gather(stalled, second), timeout=5)
        assert socket.sent[:2] == ["first", "second"], socket.sent[:2]  # FIFO preserved
    finally:
        socket.release.set()
        transport.close()


@pytest.mark.asyncio
async def test_send_backlog_is_bounded_and_closes_the_peer(monkeypatch):
    """A peer that never drains must not grow an unbounded backlog."""
    import tui_gateway.ws as ws_module

    monkeypatch.setattr(ws_module, "_WS_SEND_QUEUE_MAX", 2)
    socket = _StalledSocket()
    transport = WSTransport(socket, asyncio.get_running_loop())
    try:
        tasks = [asyncio.create_task(transport._safe_send_many(["first"]))]
        await asyncio.wait_for(socket.entered.wait(), timeout=5)
        # Now the writer is parked mid-frame; two more fill the bound, the third overflows.
        tasks += [asyncio.create_task(transport._safe_send_many([f"batch-{i}"])) for i in range(2)]
        await asyncio.sleep(0.05)
        tasks.append(asyncio.create_task(transport._safe_send_many(["overflow"])))
        await asyncio.sleep(0.05)
        assert transport.closed, "overflow did not close the peer"
        assert len(transport._send_queue) <= 2, "backlog grew past its bound"
        # Nothing is left parked forever: every producer returns.
        socket.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    finally:
        socket.release.set()
        transport.close()


@pytest.mark.asyncio
async def test_admission_receipt_reads_off_the_loop(monkeypatch):
    """``receipt`` must not run the SQLite admission read on the loop thread."""
    from gateway import session_authority as sa_module

    loop_thread = threading.get_ident()
    seen = {}
    row = {
        "target_session_id": "s1",
        "principal_id": "p1",
        "status": "queued",
    }

    def fake_read(db, *, admission_id):
        seen["thread"] = threading.get_ident()
        seen["admission_id"] = admission_id
        return row

    monkeypatch.setattr(sa_module, "get_session_admission", fake_read)

    authority = SimpleNamespace(
        db=object(),
        authorize=lambda *a, **k: None,
        sessions={"s1": SimpleNamespace(
            event_stream=SimpleNamespace(lock=threading.Lock(), settling={}),
            settling={},
        )},
        _receipt=lambda r: r,
    )
    ref = SimpleNamespace(session_id="s1")
    actor = SimpleNamespace(subject="p1", capabilities=frozenset())

    receipt = await sa_module.SessionAuthority.receipt(authority, actor, ref, "adm-1")

    assert receipt is row
    assert seen["admission_id"] == "adm-1"
    assert seen["thread"] != loop_thread, "admission read still ran on the event-loop thread"


def test_secret_scope_repeat_build_does_not_resolve_again(tmp_path, monkeypatch):
    """A second scope build for the same home must not re-resolve the path."""
    from agent import secret_scope

    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("EXAMPLE_TOKEN=abc\n", encoding="utf-8")

    calls = []
    real_resolve = type(home).resolve

    def counting_resolve(self, *args, **kwargs):
        calls.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(type(home), "resolve", counting_resolve)
    monkeypatch.setattr(secret_scope, "_is_process_home", lambda _home: False)

    secret_scope.build_profile_secret_scope(home)
    after_first = len(calls)
    assert after_first >= 1, "first build must resolve the home"

    secret_scope.build_profile_secret_scope(home)
    assert len(calls) == after_first, (
        f"a repeat build resolved the home again ({len(calls) - after_first} extra calls): "
        "the memoised home key is not being used"
    )


def test_home_key_memo_is_identity_correct_through_a_symlink(tmp_path):
    """The memo must not make two different homes look equal (a symlinked home still matches)."""
    from hermes_cli.env_loader import _home_key

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert _home_key(real) == _home_key(link)
    assert _home_key(str(link)) == _home_key(real)
