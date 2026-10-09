from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.durable_chat_tasks import (
    DurableChatCommand,
    DurableChatResult,
    execute_durable_chat_command,
    parse_durable_chat_command,
)
from gateway.platforms.api_server import APIServerAdapter
from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return tmp_path


def test_first_line_long_task_marker_preserves_prompt_verbatim() -> None:
    text = "ДЛИННАЯ ЗАДАЧА\n# Полный промпт\nНе изменяй этот текст."
    parsed = parse_durable_chat_command(text)
    assert parsed == DurableChatCommand(action="create", payload=text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/task list", DurableChatCommand("list")),
        ("/задача статус t_abc123", DurableChatCommand("status", "t_abc123")),
        ("/task pause t_abc123", DurableChatCommand("pause", "t_abc123")),
        ("/task resume t_abc123", DurableChatCommand("resume", "t_abc123")),
        ("/task stop t_abc123", DurableChatCommand("stop", "t_abc123")),
        (
            "/task change t_abc123 Используй PostgreSQL вместо SQLite",
            DurableChatCommand("change", "t_abc123", "Используй PostgreSQL вместо SQLite"),
        ),
    ],
)
def test_control_command_parsing(text: str, expected: DurableChatCommand) -> None:
    assert parse_durable_chat_command(text) == expected


def test_create_is_deduplicated_and_uses_persistent_workspace(isolated_home: Path) -> None:
    prompt = "ДЛИННАЯ ЗАДАЧА\n# Создай проект\n" + ("Проверяй каждый этап.\n" * 1000)
    first = execute_durable_chat_command(
        DurableChatCommand("create", payload=prompt),
        session_id="openwebui-chat-1",
        assignee="default",
    )
    second = execute_durable_chat_command(
        DurableChatCommand("create", payload=prompt),
        session_id="openwebui-chat-1",
        assignee="default",
    )

    assert first.ok and second.ok
    assert first.task_id == second.task_id
    assert first.workspace == second.workspace
    assert first.workspace and Path(first.workspace).is_dir()

    with kb.connect_closing(board="durable-tasks") as conn:
        task = kb.get_task(conn, first.task_id)
        assert task is not None
        prompt_path = Path(first.workspace) / "TASK_PROMPT.md"
        assert prompt_path.read_text(encoding="utf-8") == prompt
        assert str(prompt_path) in task.body
        assert len(task.body) < 1_000
        assert task.status == "ready"
        assert task.goal_mode is True
        assert task.workspace_kind == "dir"
        assert task.workspace_path == first.workspace
        assert task.session_id == "openwebui-chat-1"
        assert "execution result" in task.body
        assert "produced artifacts" in task.body
        assert "required from the user" in task.body
        subs = list(kb.list_notify_subs(conn, first.task_id))
        assert len(subs) == 1
        assert subs[0]["platform"] == "api_server"
        assert subs[0]["chat_id"] == "openwebui-chat-1"
        assert subs[0]["notifier_profile"] == "default"
        assert subs[0]["delivery_mode"] == "wake"
        assert subs[0]["delivery_metadata"]["durable_completion_contract"] == (
            "summary-artifacts-next-input-v1"
        )


def test_create_without_session_stays_unsubscribed(isolated_home: Path) -> None:
    created = execute_durable_chat_command(
        DurableChatCommand("create", payload="ДЛИННАЯ ЗАДАЧА\nСделай работу"),
        assignee="default",
    )
    assert created.ok and created.task_id
    assert "не привязан к сессии" in created.message
    with kb.connect_closing(board="durable-tasks") as conn:
        assert list(kb.list_notify_subs(conn, created.task_id)) == []


def test_pause_change_resume_and_stop_are_durable(isolated_home: Path) -> None:
    created = execute_durable_chat_command(
        DurableChatCommand("create", payload="ДЛИННАЯ ЗАДАЧА\nСделай работу"),
        session_id="openwebui-chat-2",
        assignee="default",
    )
    task_id = created.task_id
    assert task_id

    paused = execute_durable_chat_command(DurableChatCommand("pause", task_id))
    assert paused.ok and paused.state == "paused"

    changed = execute_durable_chat_command(
        DurableChatCommand("change", task_id, "Не удаляй старые файлы")
    )
    assert changed.ok and changed.state == "paused"

    resumed = execute_durable_chat_command(DurableChatCommand("resume", task_id))
    assert resumed.ok and resumed.state == "ready"

    stopped = execute_durable_chat_command(DurableChatCommand("stop", task_id))
    assert stopped.ok and stopped.state == "stopped"

    refused = execute_durable_chat_command(DurableChatCommand("resume", task_id))
    assert not refused.ok
    assert refused.state == "stopped"

    status = execute_durable_chat_command(DurableChatCommand("status", task_id))
    assert status.ok and status.state == "stopped"
    assert "Не удаляй старые файлы" in status.message


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_completion_routes_marker_without_starting_inline_agent(stream: bool) -> None:
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    result = DurableChatResult(
        True,
        "queued t_deadbeef",
        task_id="t_deadbeef",
        state="ready",
        workspace="C:/durable/t_deadbeef",
    )

    with (
        patch(
            "gateway.durable_chat_tasks.execute_durable_chat_command",
            return_value=result,
        ) as execute,
        patch.object(adapter, "_run_agent") as run_agent,
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-agent",
                    "stream": stream,
                    "messages": [
                        {"role": "user", "content": "ДЛИННАЯ ЗАДАЧА\nВыполни проект"}
                    ],
                },
            )
            assert response.status == 200
            assert response.headers["X-Hermes-Task-Id"] == "t_deadbeef"
            body = await response.text()

    execute.assert_called_once()
    run_agent.assert_not_called()
    assert "queued t_deadbeef" in body
    if stream:
        assert "data: [DONE]" in body


@pytest.mark.asyncio
async def test_marker_keeps_runtime_assignee_but_subscribes_transport_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed API turn must wake the listener that actually owns its socket."""
    from gateway.platforms.api_server import _api_request_profile
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-api-key"}))
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    result = DurableChatResult(True, "queued t_deadbeef", task_id="t_deadbeef", state="ready")
    token = _api_request_profile.set("01-orchestrator")
    try:
        with (
            patch(
                "gateway.durable_chat_tasks.execute_durable_chat_command",
                return_value=result,
            ) as execute,
            patch.object(adapter, "_check_auth", return_value=None),
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer test-api-key",
                        "X-Hermes-Session-Id": "owui-routed-session",
                    },
                    json={
                        "model": "hermes-agent",
                        "stream": False,
                        "messages": [{"role": "user", "content": "ДЛИННАЯ ЗАДАЧА\nПроверка"}],
                    },
                )
                assert response.status == 200
    finally:
        _api_request_profile.reset(token)

    execute.assert_called_once_with(
        ANY,
        session_id="owui-routed-session",
        assignee="01-orchestrator",
        notifier_profile="default",
        model_override=None,
        provider_override=None,
    )


@pytest.mark.asyncio
async def test_http_control_round_trip_uses_persistent_board(isolated_home: Path) -> None:
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)

    async with TestClient(TestServer(app)) as client:
        async def send(text: str, session_id: str | None = None):
            headers = {"Authorization": "Bearer test-api-key"}
            if session_id:
                headers["X-Hermes-Session-Id"] = session_id
            return await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "hermes-agent",
                    "stream": False,
                    "messages": [{"role": "user", "content": text}],
                },
            )

        created_response = await send("ДЛИННАЯ ЗАДАЧА\nСохрани контрольную точку")
        assert created_response.status == 200
        task_id = created_response.headers["X-Hermes-Task-Id"]
        session_id = created_response.headers["X-Hermes-Session-Id"]

        paused_response = await send(f"/task pause {task_id}", session_id)
        assert paused_response.status == 200
        assert (await paused_response.json())["hermes"]["state"] == "paused"

        changed_response = await send(
            f"/task change {task_id} Не перезаписывай результат",
            session_id,
        )
        assert changed_response.status == 200
        assert (await changed_response.json())["hermes"]["state"] == "paused"

        status_response = await send(f"/task status {task_id}", session_id)
        status_body = await status_response.json()
        assert status_body["hermes"]["state"] == "paused"
        assert "Не перезаписывай результат" in status_body["choices"][0]["message"]["content"]

        resumed_response = await send(f"/task resume {task_id}", session_id)
        assert (await resumed_response.json())["hermes"]["state"] == "ready"

        stopped_response = await send(f"/task stop {task_id}", session_id)
        assert (await stopped_response.json())["hermes"]["state"] == "stopped"

    with kb.connect_closing(board="durable-tasks") as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
        assert task is not None
        prompt_path = Path(task.workspace_path) / "TASK_PROMPT.md"
        assert prompt_path.read_text(encoding="utf-8") == (
            "ДЛИННАЯ ЗАДАЧА\nСохрани контрольную точку"
        )
        assert str(prompt_path) in task.body
        assert any("Не перезаписывай результат" in comment.body for comment in comments)
