"""api_server-cut-path: absolute local paths are removed from visible content.

Remote OpenAI-compatible frontends (Open WebUI) cannot read host-local file
paths, so a final response that names an existing local artifact must:
  * have the absolute path removed from every streamed ``delta.content`` and
    from the non-stream ``message.content``, and
  * still deliver the file bytes via the SSE ``files`` delta as
    ``name`` + base64 ``data`` (built from the RAW final response).

Matching the "existing local path" contract of task t_bc0ed52d:
  * a non-existing path stays visible (the model may be discussing it),
  * a path inside a backtick code example stays visible (code samples are
    never mutilated).

Module is deliberately self-contained.  It was written while
``tests/gateway/test_api_server.py`` could not even be collected because of a
missing ``_classify_openwebui_service_request`` symbol (a separate OWUI
feature); card t_635176e3 removed those tests, so the blocker is gone and the
self-containment is now just a fast, focused regression slice.

When run against the pre-task-1 source (HEAD of
``release/t_b0291572-owui-origin-binding``), the content assertions FAIL
because the raw path leaks into ``delta.content`` / ``message.content``; the
file-delta assertions still pass.  After the task-1 path-stripping change is
applied, all assertions are green.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("aiohttp")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _sse_chunks(body: str):
    """Parse the ``data: {...}`` SSE payloads out of a raw response body."""
    chunks = []
    for line in body.splitlines():
        if line.startswith("data: {"):
            chunks.append(json.loads(line[len("data: "):]))
    return chunks


def _joined_content(chunks) -> list:
    """Concatenated delta.content over a list of SSE chunks."""
    parts = []
    for chunk in chunks:
        for choice in chunk.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if isinstance(content, str):
                parts.append(content)
    return "".join(parts)


def _file_deltas(chunks) -> list:
    files = []
    for chunk in chunks:
        for choice in chunk.get("choices", []):
            delta_files = choice.get("delta", {}).get("files")
            if delta_files:
                files.extend(delta_files)
    return files


@pytest.mark.asyncio
async def test_stream_cut_path_from_delta_content_keeps_file_delta(tmp_path):
    """The streamed delta.content must not expose the existing local path,
    while the SSE files delta still carries name + base64 data."""
    docx = tmp_path / "anonymized.docx"
    docx.write_bytes(b"PK\x03\x04test-docx")
    nonexist = tmp_path / "missing.docx"
    final = f"\u0413\u043e\u0442\u043e\u0432\u044b\u0439 \u0444\u0430\u0439\u043b: {docx}\n\u041d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d: {nonexist}"

    adapter = _make_adapter()
    app = _create_app(adapter)

    async def _mock_run_agent(**kwargs):
        callback = kwargs.get("stream_delta_callback")
        if callback:
            callback(final)
        return (
            {"final_response": final, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "\u0441\u043e\u0437\u0434\u0430\u0439 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442"}],
                    "stream": True,
                },
            )
            assert resp.status == 200
            body = await resp.text()

    chunks = _sse_chunks(body)
    joined = _joined_content(chunks)
    # Existing absolute local path must be removed from streamed content.
    assert str(docx) not in joined
    # Non-existing path stays visible (the model may be discussing it).
    assert str(nonexist) in joined
    # The file is still delivered via the SSE files delta.
    files = _file_deltas(chunks)
    assert len(files) == 1
    assert files[0]["type"] == "file"
    assert files[0]["name"] == "anonymized.docx"
    assert files[0]["content_type"] == DOCX_MIME
    assert files[0]["data"].startswith(f"data:{DOCX_MIME};base64,")


@pytest.mark.asyncio
async def test_non_stream_cut_path_from_message_content(tmp_path):
    """Non-streaming message.content must not expose the existing local path;
    a backtick code example must still show it verbatim."""
    docx = tmp_path / "anonymized.docx"
    docx.write_bytes(b"PK\x03\x04test-docx")
    example = tmp_path / "example.txt"
    example.write_bytes(b"plain")
    final = (
        f"\u0413\u043e\u0442\u043e\u0432\u044b\u0439 \u0444\u0430\u0439\u043b: {docx}\n"
        f"\u041f\u0440\u0438\u043c\u0435\u0440: `{example}`"
    )

    adapter = _make_adapter()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        with patch.object(
            adapter,
            "_run_agent",
            new=AsyncMock(
                return_value=(
                    {"final_response": final, "messages": [], "api_calls": 1},
                    {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            ),
        ):
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "\u0441\u043e\u0437\u0434\u0430\u0439 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442"}],
                    "stream": False,
                },
            )
            assert resp.status == 200
            data = await resp.json()

    content = data["choices"][0]["message"]["content"]
    # Existing absolute local path removed from message.content.
    assert str(docx) not in content
    # Path inside inline backticks stays visible (code sample preserved).
    assert str(example) in content
