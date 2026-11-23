"""SSE primitives of the api_server platform: the cross-thread queue and the frame encoder.

Lifted out of ``platforms/api_server.py`` (line-cap offset, root AGENTS.md facade+siblings
layout). The facade re-imports both names, so ``gateway.platforms.api_server._sse_frame`` and
``ThreadSafeAsyncQueue`` stay valid importers' names — the OpenAI/Responses mixin, the
``/v1/runs`` event stream and the tests all reach them through the facade.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional


class ThreadSafeAsyncQueue(asyncio.Queue):
    """``asyncio.Queue`` a non-loop thread (run_conversation's executor) can push into via
    ``put_threadsafe``; the SSE consumer's ``await get()`` is woken by ``call_soon_threadsafe``."""

    def put_threadsafe(self, item, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        (loop or self._loop_ref).call_soon_threadsafe(self.put_nowait, item)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Always constructed inside a running async handler (the SSE
        # request handlers), so get_running_loop() is safe here.
        self._loop_ref = asyncio.get_running_loop()


def _sse_frame(
    data: Any, *, event: str | None = None, ensure_ascii: bool = True, id: Optional[int] = None
) -> bytes:
    """Encode one SSE frame (``id:``/``event:`` lines if given, then ``data: <json>\\n\\n``) for
    every SSE writer. ``ensure_ascii=False`` keeps raw non-ASCII on the wire."""
    prefix = (f"id: {id}\n" if id is not None else "") + (f"event: {event}\n" if event else "")
    return f"{prefix}data: {json.dumps(data, ensure_ascii=ensure_ascii)}\n\n".encode()
