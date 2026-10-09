"""Durable long-task routing for OpenAI-compatible chat surfaces.

A first-line marker turns a normal chat turn into a persistent Kanban worker.
Small, explicit ``/task`` commands provide a transport-neutral control plane
that works from Open WebUI without keeping the original HTTP stream alive.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Optional

from hermes_cli import kanban_db as kb


BOARD = "durable-tasks"
_CONTROL_AUTHOR = "openwebui"
_STOP_MARKER = "[durable-control:stopped]"
_PAUSE_MARKER = "[durable-control:paused]"
_RESUME_MARKER = "[durable-control:resumed]"
_CHANGE_MARKER = "[durable-control:change]"
_LONG_MARKERS = {
    "длинная задача",
    "[длинная задача]",
    "long task",
    "[long task]",
    "durable task",
    "[durable task]",
}
_ACTION_ALIASES = {
    "list": "list",
    "список": "list",
    "status": "status",
    "статус": "status",
    "pause": "pause",
    "пауза": "pause",
    "приостановить": "pause",
    "resume": "resume",
    "продолжить": "resume",
    "возобновить": "resume",
    "stop": "stop",
    "cancel": "stop",
    "остановить": "stop",
    "прервать": "stop",
    "change": "change",
    "modify": "change",
    "изменить": "change",
}
_TASK_ID_RE = re.compile(r"^t_[0-9a-f]+$")


@dataclass(frozen=True)
class DurableChatCommand:
    action: str
    task_id: Optional[str] = None
    payload: Optional[str] = None


@dataclass(frozen=True)
class DurableChatResult:
    ok: bool
    message: str
    task_id: Optional[str] = None
    state: Optional[str] = None
    workspace: Optional[str] = None


def parse_durable_chat_command(text: object) -> Optional[DurableChatCommand]:
    """Parse an opt-in long-task marker or an explicit control command."""
    if not isinstance(text, str) or not text.strip():
        return None
    first_line = text.splitlines()[0].strip().casefold().rstrip(":")
    if first_line in _LONG_MARKERS:
        return DurableChatCommand(action="create", payload=text)

    stripped = text.strip()
    match = re.match(r"^/(?:task|задача)(?:\s+(.+))?$", stripped, re.I | re.S)
    if not match:
        return None
    rest = (match.group(1) or "").strip()
    if not rest:
        return DurableChatCommand("help")
    parts = rest.split(None, 2)
    action = _ACTION_ALIASES.get(parts[0].casefold())
    if not action:
        return DurableChatCommand("help")
    if action == "list":
        return DurableChatCommand("list")
    task_id = parts[1] if len(parts) >= 2 else None
    payload = parts[2].strip() if len(parts) >= 3 else None
    return DurableChatCommand(action, task_id, payload)


def _ensure_board() -> None:
    kb.create_board(
        BOARD,
        name="Durable Tasks",
        description="Long-running tasks submitted from Open WebUI",
    )


def _latest_control(comments) -> Optional[str]:
    for comment in reversed(comments):
        body = comment.body or ""
        if body.startswith(_STOP_MARKER):
            return "stopped"
        if body.startswith(_PAUSE_MARKER):
            return "paused"
        if body.startswith(_RESUME_MARKER):
            return "resumed"
    return None


def _public_state(task, comments) -> str:
    control = _latest_control(comments)
    if control == "stopped":
        return "stopped"
    if task.status == "done":
        return "completed"
    if control == "paused" and task.status in {"scheduled", "blocked", "triage"}:
        return "paused"
    return task.status


def _workspace_for(idempotency_key: str) -> str:
    root = kb.kanban_home() / "durable-tasks" / "workspaces"
    path = root / idempotency_key[-24:]
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


def _persist_prompt(workspace: str, prompt: str) -> Path:
    """Atomically persist the complete prompt without rewriting or truncating it."""
    path = Path(workspace) / "TASK_PROMPT.md"
    if path.exists():
        if path.read_text(encoding="utf-8") != prompt:
            raise RuntimeError(f"durable prompt collision at {path}")
        return path
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(prompt, encoding="utf-8", newline="")
    tmp.replace(path)
    return path


def _worker_body(prompt_path: Path, digest: str) -> str:
    return (
        "This is a durable long task. Before doing anything else, read the COMPLETE "
        f"authoritative prompt from `{prompt_path}` with a file tool; do not rely on a "
        "truncated preview. Follow that file verbatim and keep all checkpoints/results "
        f"inside this workspace. Prompt SHA-256: `{digest}`. "
        "Before finishing, verify the requested result. Then call kanban_complete with "
        "all deliverable file paths in artifacts and a concise summary containing three "
        "explicit parts: (1) execution result, (2) produced artifacts, and (3) what is "
        "still required from the user to continue, using 'nothing' when no input is "
        "needed. If blocked, call kanban_block and state exactly what user input is "
        "required. Do not end with only a progress note or an acknowledgement."
    )


def _task_title(prompt: str) -> str:
    lines = [line.strip().lstrip("#").strip() for line in prompt.splitlines()[1:]]
    title = next((line for line in lines if line), "Длинная задача")
    return title[:120]


def _format_status(task, comments, runs) -> DurableChatResult:
    state = _public_state(task, comments)
    changes = [
        c.body[len(_CHANGE_MARKER):].strip()
        for c in comments
        if (c.body or "").startswith(_CHANGE_MARKER)
    ]
    last_run = runs[-1] if runs else None
    details = [
        f"Задача: {task.id}",
        f"Состояние: {state}",
        f"Этап Kanban: {task.status}",
        f"Рабочая папка: {task.workspace_path or 'не задана'}",
        f"Процесс: {task.worker_pid or 'не запущен'}",
    ]
    if task.last_heartbeat_at:
        details.append(f"Последний heartbeat: {task.last_heartbeat_at}")
    if last_run:
        details.append(
            "Последний запуск: "
            f"run={last_run.id}, status={last_run.status}, "
            f"outcome={last_run.outcome or '—'}"
        )
    if task.last_failure_error:
        details.append(f"Последняя ошибка: {task.last_failure_error}")
    if changes:
        details.append(f"Последнее изменение: {changes[-1]}")
    if task.result:
        details.append(f"Результат: {task.result}")
    return DurableChatResult(
        True,
        "\n".join(details),
        task_id=task.id,
        state=state,
        workspace=task.workspace_path,
    )


def _load_task(conn, task_id: Optional[str]):
    if not task_id or not _TASK_ID_RE.fullmatch(task_id):
        return None
    return kb.get_task(conn, task_id)


def execute_durable_chat_command(
    command: DurableChatCommand,
    *,
    session_id: Optional[str] = None,
    assignee: Optional[str] = "default",
    notifier_profile: Optional[str] = None,
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
) -> DurableChatResult:
    """Execute a parsed command against the persistent Kanban board."""
    _ensure_board()

    if command.action == "help":
        return DurableChatResult(
            True,
            "Команды: /task list; /task status <id>; /task pause <id>; "
            "/task resume <id>; /task change <id> <уточнение>; /task stop <id>. "
            "Для запуска начните промпт отдельной строкой: ДЛИННАЯ ЗАДАЧА",
        )

    if command.action == "create":
        prompt = command.payload or ""
        digest = hashlib.sha256(
            ((session_id or "") + "\0" + prompt).encode("utf-8")
        ).hexdigest()
        idem = f"openwebui-durable-{digest}"
        workspace = _workspace_for(idem)
        prompt_path = _persist_prompt(workspace, prompt)
        with kb.connect_closing(board=BOARD) as conn:
            task_id = kb.create_task(
                conn,
                title=_task_title(prompt),
                body=_worker_body(
                    prompt_path,
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                ),
                assignee=assignee,
                priority=100,
                created_by="openwebui",
                workspace_kind="dir",
                workspace_path=workspace,
                idempotency_key=idem,
                skills=["durable-long-task-execution"],
                max_retries=3,
                model_override=model_override,
                provider_override=provider_override,
                goal_mode=True,
                goal_max_turns=80,
                session_id=session_id,
                board=BOARD,
            )
            # This marker route bypasses the normal agent turn and therefore
            # kanban_create's session-aware auto-subscribe hook. Persist the
            # raw API session explicitly so terminal events wake the exact
            # conversation that created the task. add_notify_sub is an
            # idempotent upsert, so deduplicated/retried creates remain safe.
            if session_id:
                kb.add_notify_sub(
                    conn,
                    task_id=task_id,
                    platform="api_server",
                    chat_id=session_id,
                    chat_type="dm",
                    # The worker can run under a routed runtime profile while
                    # the API listener belongs to another multiplex profile.
                    # The notifier must use the transport owner so its watcher
                    # can claim and self-post the terminal wake.  Direct callers
                    # retain the historical assignee default.
                    notifier_profile=notifier_profile or assignee,
                    delivery_mode="wake",
                    delivery_metadata={
                        "chat_type": "dm",
                        "durable_completion_contract": (
                            "summary-artifacts-next-input-v1"
                        ),
                    },
                )
            task = kb.get_task(conn, task_id)
            actual_workspace = task.workspace_path if task else workspace
            actual_state = (
                _public_state(task, kb.list_comments(conn, task_id))
                if task
                else "ready"
            )
        return DurableChatResult(
            True,
            "Длинная задача поставлена в устойчивую очередь.\n"
            f"ID: {task_id}\nРабочая папка: {actual_workspace}\n"
            f"Статус: /task status {task_id}\n"
            f"Пауза: /task pause {task_id}\n"
            f"Изменение: /task change {task_id} <уточнение>\n"
            f"Продолжить: /task resume {task_id}\n"
            f"Остановить: /task stop {task_id}\n"
            + (
                "По завершении итог будет автоматически записан в эту сессию."
                if session_id
                else "Вызов не привязан к сессии; итог останется на доске."
            ),
            task_id=task_id,
            state=actual_state,
            workspace=actual_workspace,
        )

    with kb.connect_closing(board=BOARD) as conn:
        if command.action == "list":
            tasks = kb.list_tasks(conn, include_archived=True, order_by="updated", limit=30)
            if session_id:
                scoped = [t for t in tasks if t.session_id == session_id]
                tasks = scoped or tasks
            if not tasks:
                return DurableChatResult(True, "Устойчивых задач пока нет.")
            rows = []
            for task in tasks:
                comments = kb.list_comments(conn, task.id)
                rows.append(f"{task.id} — {_public_state(task, comments)} — {task.title}")
            return DurableChatResult(True, "\n".join(rows))

        task = _load_task(conn, command.task_id)
        if task is None:
            return DurableChatResult(False, "Задача не найдена или ID некорректен.")
        comments = kb.list_comments(conn, task.id)
        state = _public_state(task, comments)

        if command.action == "status":
            return _format_status(task, comments, kb.list_runs(conn, task.id))

        if command.action == "pause":
            if state == "stopped":
                return DurableChatResult(False, "Остановленную задачу нельзя возобновлять или ставить на паузу.", task.id, state)
            if task.status == "done":
                return DurableChatResult(False, "Задача уже завершена.", task.id, "completed")
            if task.status == "running" or task.claim_lock:
                kb.reclaim_task(conn, task.id, reason="paused from Open WebUI")
            current = kb.get_task(conn, task.id)
            if current and current.status != "scheduled":
                kb.schedule_task(conn, task.id, reason="paused from Open WebUI")
            kb.add_comment(conn, task.id, _CONTROL_AUTHOR, f"{_PAUSE_MARKER} Пауза запрошена пользователем")
            return DurableChatResult(True, f"Задача {task.id} приостановлена. Уже сохраненные файлы и журнал сохранены.", task.id, "paused", task.workspace_path)

        if command.action == "change":
            if not command.payload:
                return DurableChatResult(False, "После ID укажите текст изменения.", task.id, state)
            if state == "stopped":
                return DurableChatResult(False, "Задача остановлена; изменения не применены.", task.id, state)
            if task.status == "running" or task.claim_lock:
                kb.reclaim_task(conn, task.id, reason="requirements changed from Open WebUI")
            current = kb.get_task(conn, task.id)
            if current and current.status != "scheduled":
                kb.schedule_task(conn, task.id, reason="requirements changed from Open WebUI")
            kb.add_comment(conn, task.id, _CONTROL_AUTHOR, f"{_CHANGE_MARKER} {command.payload}")
            kb.add_comment(conn, task.id, _CONTROL_AUTHOR, f"{_PAUSE_MARKER} Изменение сохранено; требуется явное возобновление")
            return DurableChatResult(True, f"Изменение сохранено, задача {task.id} поставлена на паузу. Проверьте статус и выполните /task resume {task.id}.", task.id, "paused", task.workspace_path)

        if command.action == "resume":
            if state == "stopped":
                return DurableChatResult(False, "Задача была окончательно остановлена; автоматическое возобновление запрещено.", task.id, "stopped", task.workspace_path)
            if task.status == "done":
                return DurableChatResult(False, "Задача уже завершена.", task.id, "completed", task.workspace_path)
            if task.status not in {"scheduled", "blocked", "triage"}:
                return DurableChatResult(True, f"Задача {task.id} уже в состоянии {task.status}.", task.id, task.status, task.workspace_path)
            if not kb.unblock_task(conn, task.id):
                return DurableChatResult(False, "Не удалось вернуть задачу в очередь.", task.id, state, task.workspace_path)
            kb.add_comment(conn, task.id, _CONTROL_AUTHOR, f"{_RESUME_MARKER} Возобновлено пользователем")
            current = kb.get_task(conn, task.id)
            return DurableChatResult(True, f"Задача {task.id} возвращена в очередь с последнего сохраненного состояния.", task.id, current.status if current else "ready", task.workspace_path)

        if command.action == "stop":
            if state == "stopped":
                return DurableChatResult(True, f"Задача {task.id} уже остановлена.", task.id, "stopped", task.workspace_path)
            if task.status == "running" or task.claim_lock:
                kb.reclaim_task(conn, task.id, reason="stopped from Open WebUI")
            current = kb.get_task(conn, task.id)
            if current and current.status != "scheduled" and current.status != "done":
                kb.schedule_task(conn, task.id, reason="stopped from Open WebUI")
            kb.add_comment(conn, task.id, _CONTROL_AUTHOR, f"{_STOP_MARKER} Остановлено пользователем")
            return DurableChatResult(True, f"Задача {task.id} остановлена. Рабочая папка и журнал сохранены; повторный запуск заблокирован.", task.id, "stopped", task.workspace_path)

    return DurableChatResult(False, "Неизвестная команда устойчивой задачи.")
