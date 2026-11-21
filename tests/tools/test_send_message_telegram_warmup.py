"""The one-shot Telegram send establishes the Bot API tunnel before the state-changing send.

Field defect (t_7a6e8032): cron delivery to ``telegram:125805788`` failed with
``Telegram send failed: Timed out`` at 20.3 s — exactly PTB's read timeout — while curl to
the same Bot API through the same proxy answered in 0.4 s. Measured against the production
proxy path with the real PTB client: the FIRST request on a fresh client stalled the full
read timeout in 5 of 40 runs, while the SECOND request on that same warm client never took
more than 0.21 s (n=36). The tunnel setup, not the send, is what hangs; the live gateway
never notices because its long-poll already holds a warm connection.

So the one-shot path warms a client with a bounded ``getMe`` (side-effect free) before
sending, discarding the client between attempts — a probe cancelled mid-connect leaves the
abandoned connection in that pool, so retrying on the same client can hang again. The send
itself is never retried here: it may already be in the chat.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

#: Extra dict the tests pass to the sender; the warm-up budget is config, not env.
EXTRA = {"warm_attempts": 3, "warm_attempt_seconds": 0.05}


class _Bot:
    """Bot stand-in whose ``get_me`` stalls (past the per-attempt bound) a set number of times."""

    def __init__(self, stall_sequence):
        self.stalls = list(stall_sequence)   # one flag per getMe call
        self.calls = 0
        self.closed = False
        self.sent = []
        self.stall_gate = asyncio.Event()    # never set: a stalling probe hangs until cancelled

    async def get_me(self):
        self.calls += 1
        stall = self.stalls[self.calls - 1] if self.calls <= len(self.stalls) else False
        if stall:
            await self.stall_gate.wait()   # outlives the per-attempt bound, no clock involved
        return SimpleNamespace(username="probe_bot")

    async def shutdown(self):
        self.closed = True


class _Harness:
    """Serves a different _Bot per build() so a fresh client per attempt is observable."""

    def __init__(self, stall_sequences):
        self.bots = [_Bot(seq) for seq in stall_sequences]
        self.built = 0

    def build(self, token, extra=None):
        bot = self.bots[min(self.built, len(self.bots) - 1)]
        self.built += 1
        return bot


@pytest.fixture(autouse=True)
def _stub_client(monkeypatch):
    from plugins.platforms.telegram import standalone
    monkeypatch.setattr(standalone, "_telegram_bot", lambda token, extra=None: _HARNESS[0].build(token, extra))
    monkeypatch.setattr(standalone, "_telegram_format", lambda message: (message, None, False))


_HARNESS: list = []


def _send(bot_seq, extra=None):
    from plugins.platforms.telegram import standalone
    _HARNESS[:] = [_Harness(bot_seq)]
    return asyncio.run(standalone._send_telegram("tok", "123", "hello world",
                                                 extra=dict(EXTRA if extra is None else extra)))


def test_a_stalled_first_client_is_replaced_and_the_send_goes_out(monkeypatch):
    """The stall costs one discarded client, never the message."""
    from plugins.platforms.telegram import standalone

    sender = AsyncMock(return_value=SimpleNamespace(message_id=7))
    monkeypatch.setattr(standalone, "_telegram_send_text_chunk", sender)

    result = _send([[True], []])          # first client stalls, the second is healthy

    assert result["success"] is True
    assert standalone._warm_attempts(EXTRA) == 3
    assert _HARNESS[0].built == 2, "a stalled client must be replaced, not reused"
    assert _HARNESS[0].bots[0].closed is True, "the discarded client's pool is released"
    sender.assert_awaited_once()          # the real send happens on the warmed client


def test_retries_never_reuse_a_cancelled_client(monkeypatch):
    """Same-client retry silently re-hangs on the abandoned connection — every attempt must
    build a new one (the reason this is a build() loop, not a getMe() loop)."""
    from plugins.platforms.telegram import standalone

    monkeypatch.setattr(standalone, "_telegram_send_text_chunk",
                        AsyncMock(return_value=SimpleNamespace(message_id=7)))

    result = _send([[True], [True], []])   # two stalled clients, third healthy

    assert result["success"] is True
    assert _HARNESS[0].built == 3
    assert all(b.calls == 1 for b in _HARNESS[0].bots[:3]), "one probe per client, never on a dead one"


def test_warm_up_is_side_effect_free_and_disabled_by_zero(monkeypatch):
    from plugins.platforms.telegram import standalone

    monkeypatch.setattr(standalone, "_telegram_send_text_chunk",
                        AsyncMock(return_value=SimpleNamespace(message_id=7)))

    result = _send([[True], [True], [True]], {"warm_attempts": 0})   # every client would stall

    assert result["success"] is True
    bot = _HARNESS[0].bots[0]
    assert bot.calls == 0, "attempts=0 must skip the probe entirely"
    assert bot.sent == [], "getMe only — a warm-up never posts to a chat"
    assert "warnings" not in result


def test_a_send_that_still_times_out_reports_an_error_and_is_not_retried(monkeypatch):
    """Probes must not paper over a genuinely broken path, and must never resend a message
    that may already be in the chat."""
    from plugins.platforms.telegram import standalone

    send = AsyncMock(side_effect=TimeoutError("Timed out"))
    monkeypatch.setattr(standalone, "_telegram_send_text_chunk", send)

    result = _send([[]])

    assert "Timed out" in result["error"] and "success" not in result
    assert send.await_count == 1, "an ambiguous send is never repeated"


def test_warm_up_budget_comes_from_platform_extra_and_tolerates_junk():
    """Platform config, not a new HERMES_* env var (behavioural settings belong in config.yaml)."""
    from plugins.platforms.telegram import standalone

    assert standalone._warm_attempts(None) == 3
    assert standalone._warm_attempt_seconds(None) == 8.0
    assert standalone._warm_attempts({}) == 3
    assert standalone._warm_attempt_seconds({}) == 8.0
    # Junk must fall back to the default instead of raising inside a send.
    assert standalone._warm_attempts({"warm_attempts": "garbage"}) == 3
    assert standalone._warm_attempt_seconds({"warm_attempt_seconds": "-3"}) == 8.0
    assert standalone._warm_attempts({"warm_attempts": 1}) == 1
    assert standalone._warm_attempt_seconds({"warm_attempt_seconds": 2}) == 2.0


def test_an_exhausted_warm_up_still_sends_and_reports_the_real_outcome(monkeypatch):
    """Warming is best effort: a path that never warms must surface the send's own error, not
    a manufactured one — and must not pretend the send was skipped."""
    from plugins.platforms.telegram import standalone

    send = AsyncMock(return_value=SimpleNamespace(message_id=9))
    monkeypatch.setattr(standalone, "_telegram_send_text_chunk", send)

    result = _send([[True], [True], [True]])

    assert result["success"] is True, "the send still runs on the last client"
    assert result["message_id"] == "9"
    assert _HARNESS[0].built == 3
    send.assert_awaited_once()
