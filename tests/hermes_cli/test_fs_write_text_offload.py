"""Concurrent saves of ONE file through ``_fs_write_text_blocking`` must all succeed.

Regression contract for a defect the offload itself introduced: the body staged to
``.<name>.hermes-tmp-<pid>``, a name fixed for the whole process. Running on the event loop that
was harmless (one call at a time). On the worker pool, two savers of the same file pick the same
temp path, the first ``os.replace`` consumes it and the second raises ENOENT *and* its error
branch unlinks whichever file the winner left behind.

The contract: N concurrent saves of the same target all return ok, the file ends up containing
exactly one of the payloads in full (never a mix, never truncated), and no staging file survives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hermes_cli.web_routers import handler_offload as ho
from hermes_cli.web_routers.files import _fs_write_text_blocking


@pytest.fixture(autouse=True)
def _reset_seam(monkeypatch):
    monkeypatch.delenv("HERMES_HANDLER_OFFLOAD", raising=False)
    ho.HANDLER_OFFLOAD.reset()
    yield
    ho.HANDLER_OFFLOAD.reset()


def test_concurrent_saves_of_the_same_path_all_succeed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1")
    ho.HANDLER_OFFLOAD.install(None, workers=8)
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")

    payloads = [f"payload-{i}-" + "x" * 20000 for i in range(8)]

    async def burst():
        return await asyncio.gather(*[
            ho.off_loop(_fs_write_text_blocking, target, text) for text in payloads
        ])

    results = asyncio.run(burst())

    assert len(results) == len(payloads)
    assert all(r["ok"] and r["path"] == str(target) for r in results)
    body = target.read_text(encoding="utf-8")
    assert body in payloads, "the surviving file is not exactly one of the payloads"
    leftovers = [p.name for p in tmp_path.iterdir() if "hermes-tmp" in p.name]
    assert leftovers == [], f"staging files left behind: {leftovers}"
