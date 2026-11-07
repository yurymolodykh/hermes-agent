"""api_server: пустой финальный ответ не должен уходить клиенту как "".

Инцидент 2026-11-06 (Open WebUI): агент завершал ход без текста и без флагов
failed/partial (единственный диагностический статус висел в display-буфере,
который клиенту не отдаётся). api_server возвращал 200 с
``message.content == ""`` — фронтенд рисовал пустой пузырь без ошибки, и
пользователь видел «вообще никакой ответ не приходит».

Контракт после фикса:
  * пустой final_response без флага провала -> 200 с ЯВНЫМ текстом
    (уведомление агента о исчерпании transient-ретраев, иначе — дефолтная
    формулировка);
  * если агент всё же честно сообщил failed/partial — сохраняется прежняя
    жёсткая ветка 502 agent_incomplete (текст не подменяется);
  * непустой ответ не трогается.
"""

import pytest

pytest.importorskip("aiohttp")

from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _usage():
    return {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}


async def _post(adapter, payload, *, stream: bool):
    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", new=AsyncMock(return_value=payload)):
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "привет"}],
                    "stream": stream,
                },
            )
            return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_empty_response_gets_explicit_agent_notice():
    """Уведомление агента о исчерпании ретраев доходит до content."""
    adapter = _make_adapter()
    notice = (
        "❌ Connection to provider failed after 3 attempts. "
        "The provider may be experiencing issues — try again in a moment."
    )
    status, body = await _post(
        adapter,
        (
            {
                "final_response": "",
                "messages": [],
                "api_calls": 1,
                "failure_notice": notice,
            },
            _usage(),
        ),
        stream=False,
    )
    assert status == 200
    content = body["choices"][0]["message"]["content"]
    assert content.strip()
    assert "3 attempts" in content


@pytest.mark.asyncio
async def test_empty_response_without_notice_gets_fallback_text():
    """Без уведомления подставляется дефолтная внятная формулировка."""
    adapter = _make_adapter()
    status, body = await _post(
        adapter,
        ({"final_response": "", "messages": [], "api_calls": 1}, _usage()),
        stream=False,
    )
    assert status == 200
    content = body["choices"][0]["message"]["content"]
    assert content.strip()
    assert "Модель не вернула" in content


@pytest.mark.asyncio
async def test_hard_failure_branch_unchanged():
    """Честный failed/partial + пустой текст -> прежняя ветка 502."""
    adapter = _make_adapter()
    status, body = await _post(
        adapter,
        (
            {
                "final_response": "",
                "messages": [],
                "api_calls": 1,
                "failed": True,
                "error": "provider exploded",
            },
            _usage(),
        ),
        stream=False,
    )
    assert status == 502
    assert body["error"]["code"] == "agent_incomplete"


@pytest.mark.asyncio
async def test_non_empty_response_untouched():
    """Нормальный ответ не подменяется."""
    adapter = _make_adapter()
    status, body = await _post(
        adapter,
        ({"final_response": "готово", "messages": [], "api_calls": 1}, _usage()),
        stream=False,
    )
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "готово"
