"""Why a Kanban worker stopped — one small file every stop path writes and every reader honours.

A worker that is SIGTERMed by the dispatcher, reclaimed, or archived dies with no chance to
say anything: the submitter had no ``SIGTERM`` handler, the owner saw only ``EOFError`` where
the child's frame stream ended, and the card's event log ended up saying "обрыв потока" for
four very different platform conditions (``limit_exceeded``, ``reassigned``, ``archived``,
``shutdown``). The board cannot tell a card that hit its time limit from one an operator
archived, and neither can whoever reads the log afterwards.

The transport is deliberately dumb and local: the stop path writes
``<workspace>/.stop-reason.json`` BEFORE it signals (best effort — never blocking the stop),
the worker's signal handler and the gateway's loss recorder read it back. The file lives in
the card's own workspace so no shared state, schema or lock is involved, and every writer is
wrapped so a missing/read-only workspace degrades to today's behaviour instead of failing a
kill.

The durable copy is written by the stop sites themselves into the run/event they already
emit (``timed_out``, ``reclaimed``, ``archived``, ``worker_lost``), because the workspace — and
with it this file — is removed when the card is archived or completed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Optional

#: Name of the reason file inside a card's workspace.
STOP_FILE_NAME = ".stop-reason.json"

#: Explicit override (absolute file path). Set for a worker whose workspace is not a plain
#: directory (or for tests); otherwise the path is derived from the workspace.
STOP_FILE_ENV = "HERMES_KANBAN_STOP_REASON"

#: The reasons a stop path may record. ``unknown`` is the ABSENCE of the file, not a value a
#: writer should produce: a SIGKILLed worker never gets to write one, and that is exactly what
#: ``unknown`` means on the board.
KNOWN = ("limit_exceeded", "reassigned", "archived", "shutdown")

#: Human text per reason, used when a writer supplies no ``detail``.
_DEFAULT_DETAIL = {
    "limit_exceeded": "остановлен: превышен лимит времени карточки",
    "reassigned": "остановлен: карточка перепризначена, попытка освобождена",
    "archived": "остановлен: карточка архивирована",
    "shutdown": "остановлен: останов шлюза или хоста",
}


def stop_file_path(workspace: Optional[os.PathLike | str] = None) -> Optional[Path]:
    """Path of the reason file for ``workspace`` (``STOP_FILE_ENV`` wins when set)."""
    pin = os.environ.get(STOP_FILE_ENV, "").strip()
    if pin:
        return Path(pin).expanduser()
    if workspace is None:
        return None
    return Path(workspace).expanduser() / STOP_FILE_NAME


def write_reason(
    path: Optional[os.PathLike | str],
    reason: str,
    *,
    detail: Optional[str] = None,
    source: Optional[str] = None,
    pid: Optional[int] = None,
    task_id: Optional[str] = None,
) -> bool:
    """Atomically record ``reason`` for a stopping worker; True when the file landed.

    Never raises: the caller is a kill path, and a workspace that is missing, read-only or
    unmounted must not turn "the worker could not be told why" into "the worker was not
    stopped". ``os.replace`` makes the read side never observe a half-written file.
    """
    if not path:
        return False
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "reason": str(reason),
            "detail": str(detail) if detail else _DEFAULT_DETAIL.get(str(reason), ""),
            "source": source,
            "pid": int(pid) if pid else None,
            "task_id": task_id,
            "ts": int(time.time()),
        }
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".stop-reason.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def write_reason_for_task(
    task_id: str,
    reason: str,
    *,
    detail: Optional[str] = None,
    source: Optional[str] = None,
    pid: Optional[int] = None,
    workspace: Optional[os.PathLike | str] = None,
    workspaces_root: Optional[os.PathLike | str] = None,
) -> bool:
    """``write_reason`` for a card whose workspace path may be unknown.

    ``workspace`` is the persisted ``tasks.workspace_path``; when it is empty (a card that was
    never dispatched, or a scratch card whose dir is derived) the per-task directory under
    ``workspaces_root`` is the same path the dispatcher will use.
    """
    path = stop_file_path(workspace)
    if path is None:
        if workspaces_root is None:
            return False
        path = Path(workspaces_root).expanduser() / str(task_id) / STOP_FILE_NAME
    return write_reason(path, reason, detail=detail, source=source, pid=pid, task_id=task_id)


def read_reason(path: Optional[os.PathLike | str]) -> Optional[dict[str, Any]]:
    """Payload written by a stop path, or None when absent/unreadable/malformed."""
    if not path:
        return None
    try:
        with open(Path(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    reason = str(data.get("reason") or "").strip()
    if not reason:
        return None
    data["reason"] = reason
    return data


def read_reason_for_workspace(workspace: Optional[os.PathLike | str]) -> Optional[dict[str, Any]]:
    """``read_reason`` at the workspace-derived path (``STOP_FILE_ENV`` honoured)."""
    return read_reason(stop_file_path(workspace))


def describe(payload: Optional[dict[str, Any]], *, fallback: str = "unknown") -> str:
    """One-line board/log text for a reason payload: ``<reason>: <detail>``.

    ``fallback`` is used when no payload exists at all (a SIGKILLed worker): it must be the
    bare ``unknown`` word, so an unrecorded stop keeps reading as one, not as a fake cause.
    """
    if not payload:
        return str(fallback)
    reason = str(payload.get("reason") or fallback)
    detail = str(payload.get("detail") or _DEFAULT_DETAIL.get(reason, "") or "").strip()
    return f"{reason}: {detail}" if detail else reason


def is_known(payload: Optional[dict[str, Any]]) -> bool:
    """True when the payload carries one of the reasons a stop path is allowed to record."""
    return bool(payload) and str(payload.get("reason")) in KNOWN
