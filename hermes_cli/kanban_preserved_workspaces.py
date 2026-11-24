"""Durable registry of scratch workspaces the unsaved-work guard kept on disk.

Provenance used to live ONLY in ``task_events`` (kind
``workspace_cleanup_preserved_unsaved_work``). That table is retention-scoped
(``kanban_db.gc_events``, 30 days by default) and its rows for a card are
dropped outright by a hard delete — while the directory itself stays on disk
forever. A month later nothing names the directory, so the documented "clean it
up by hand" step is impossible and the guard turns into a slow disk leak
(verdict t_896b6548, Д3).

Every preserved directory is therefore also recorded in a small JSON file next
to the board's ``kanban.db``, which outlives both the event and the card.
``hermes kanban gc`` prints those paths so the operator can act on them, and an
entry whose directory is gone is dropped together with its retained event —
that is what keeps the retention exception bounded (Д7: recording is
idempotent per (task, path), so repeated ``gc`` runs add nothing).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from hermes_cli.kanban_db_connect import _main_db_file

#: Event kind completion/gc/archive record when they refuse to delete a dir.
PRESERVED_KIND = "workspace_cleanup_preserved_unsaved_work"
REGISTRY_NAME = "kanban-preserved-workspaces.json"
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05


def registry_path(conn: sqlite3.Connection) -> Optional[Path]:
    """``<board>/kanban-preserved-workspaces.json``; None for an in-memory DB.

    Keyed off the connection's own file rather than the ambient board pin: the
    registry must land beside the DB whose rows it describes (boards, an
    ``HERMES_KANBAN_DB``-pinned worker and a temp test board all differ).
    """
    db_file = _main_db_file(conn)
    return Path(db_file).with_name(REGISTRY_NAME) if db_file else None


def _lock_handle(handle) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_handle(handle) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _registry_lock(path: Path):
    """Exclusive lock on ``<registry>.lock``; yields False when it timed out.

    Bounded on purpose (the same trade the board's init lock makes): a stalled
    holder must not wedge a worker's completion. Without the lock a concurrent
    writer can lose its read-modify-write, which loses a registry line — never
    the directory, and the event row still names it.
    """
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError:
        yield False
        return
    acquired = False
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            if _lock_handle(handle):
                acquired = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_LOCK_POLL_SECONDS)
        yield acquired
    finally:
        if acquired:
            _unlock_handle(handle)
        handle.close()


def _read(path: Path) -> list[dict]:
    """Registry entries, sorted by path; an unreadable/corrupt file reads empty.

    Losing the file must never block completion: the directory stays on disk
    and the event row still points at it, so the next ``record`` rewrites it.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("path") and e.get("task_id")]


def _write(path: Path, entries: list[dict]) -> None:
    """Atomic replace so a crash mid-write cannot leave a truncated registry."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(sorted(entries, key=lambda e: (e["path"], e["task_id"])), indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _event_paths(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Paths already named by this task's preserved events."""
    try:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, PRESERVED_KIND),
        ).fetchall()
    except sqlite3.Error:
        return []
    found: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("path"):
            found.append(str(payload["path"]))
    return found


def record(
    conn: sqlite3.Connection, task_id: str, path: Path | str, repo: Path | str, reason: str
) -> None:
    """Record one preserved directory; idempotent per (task, path).

    Both halves are guarded: the event is appended only when this task has no
    event naming the same path yet (each ``gc`` run re-runs the preserve check
    on an archived card, and used to add a row per run), and the registry entry
    is upserted rather than appended.
    """
    entry = {
        "task_id": task_id,
        "path": str(path),
        "repo": str(repo),
        "reason": reason,
        "recorded_at": int(time.time()),
    }
    if entry["path"] not in _event_paths(conn, task_id):
        try:
            from hermes_cli.kanban_db import _append_event

            _append_event(
                conn,
                task_id,
                PRESERVED_KIND,
                {"path": entry["path"], "repo": entry["repo"], "reason": reason},
            )
        except sqlite3.Error:
            pass
    registry = registry_path(conn)
    if registry is None:
        return
    with _registry_lock(registry) as locked:
        if not locked:
            from hermes_cli.kanban_db import _log

            _log.warning(
                "Preserved-workspace registry %s stayed locked for %.0fs; "
                "recording %s without it", registry, _LOCK_TIMEOUT_SECONDS, path,
            )
        entries = [
            e
            for e in _read(registry)
            if (e["task_id"], e["path"]) != (task_id, entry["path"])
        ]
        entries.append(entry)
        with contextlib.suppress(OSError):
            _write(registry, entries)


def entries(conn: sqlite3.Connection) -> list[dict]:
    """Registry entries for this board (empty when nothing was ever preserved)."""
    registry = registry_path(conn)
    return _read(registry) if registry is not None else []


def _forget_events(conn: sqlite3.Connection, task_id: str, path: str) -> None:
    """Drop this task's retained events that name *path* (its dir is gone)."""
    rows = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, PRESERVED_KIND),
    ).fetchall()
    stale = [row[0] for row in rows if _payload_path(row[1]) == path]
    if not stale:
        return
    conn.executemany(
        "DELETE FROM task_events WHERE id = ?", [(row_id,) for row_id in stale]
    )


def directory_is_gone(path: str) -> bool:
    """True only on a *reliable* negative: the path is absent and its parent lists.

    ``Path.is_dir()`` is also ``False`` for a transient stat failure — an
    unmounted volume, ``EACCES`` on an ancestor, an NFS hiccup — so reading it
    as "gone" deletes the registry line AND the retained event while the
    directory is still on disk. That is the one outcome the registry exists to
    prevent, so the rule here is the same one the unsaved-work scan already
    uses: unreadable means "cannot tell", never "clean".
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False  # EACCES / EIO / ENOTDIR on an ancestor: cannot tell
    else:
        return False  # present as a dir, a file, or even a dangling symlink
    parent = os.path.dirname(path.rstrip("/\\")) or "."
    try:
        with os.scandir(parent):
            return True
    except FileNotFoundError:
        # The parent itself is missing: nothing can bring the child back, so
        # this is the reliable negative the docs promise ("once the path is
        # gone"), not the unreadable-ancestor case below.
        return True
    except OSError:
        return False  # absent from a parent we cannot list is still "cannot tell"


def adopt(conn: sqlite3.Connection, task_id: Optional[str] = None) -> int:
    """Copy preserved *events* into the registry; returns how many were seen.

    Cards preserved before the registry existed carry their provenance only in
    ``task_events``, and a hard delete wipes exactly that. Calling this before
    the task rows go means ``delete_archived_task`` can never be the step that
    erases the last pointer to a directory still on disk; ``gc`` calls it too,
    so an existing board migrates on the next sweep. Idempotent: ``record``
    upserts per (task, path) and appends no duplicate event.
    """
    sql = "SELECT task_id, payload FROM task_events WHERE kind = ?"
    params: list[object] = [PRESERVED_KIND]
    if task_id is not None:
        sql += " AND task_id = ?"
        params.append(task_id)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return 0
    adopted = 0
    for row in rows:
        try:
            payload = json.loads(row[1] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload.get("path"):
            continue
        path = str(payload["path"])
        if directory_is_gone(path):
            continue
        record(
            conn,
            str(row[0]),
            path,
            payload.get("repo") or path,
            payload.get("reason") or "preserved scratch workspace",
        )
        adopted += 1
    return adopted


def reap_orphan_events(conn: sqlite3.Connection) -> int:
    """Drop preserved events whose directory is gone and has no registry line.

    ``adopt`` cannot help here: it skips exactly the events whose directory is
    already gone (there is nothing left to migrate), and ``prune_gone`` only
    walks registry entries. An event written before the registry existed, for a
    directory deleted since, was therefore reaped by no branch — and retention
    exempts :data:`PRESERVED_KIND` forever (verdict t_12e58882, Д-нов-4).

    Deliberately conservative: only a *reliable* negative
    (:func:`directory_is_gone`) with no entry naming the same path goes, so a
    live directory or one reported by the registry never loses its pointer.
    """
    try:
        rows = conn.execute(
            "SELECT id, task_id, payload FROM task_events WHERE kind = ?",
            (PRESERVED_KIND,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    known = {(e["task_id"], e["path"]) for e in entries(conn)}
    orphan_ids = [
        row[0]
        for row in rows
        if (str(row[1]), _payload_path(row[2])) not in known
        and _payload_path(row[2])
        and directory_is_gone(_payload_path(row[2]))
    ]
    if not orphan_ids:
        return 0
    with contextlib.suppress(sqlite3.Error):
        conn.executemany(
            "DELETE FROM task_events WHERE id = ?", [(row_id,) for row_id in orphan_ids]
        )
        return len(orphan_ids)
    return 0


def _payload_path(payload) -> str:
    try:
        parsed = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return ""
    return str(parsed.get("path", "")) if isinstance(parsed, dict) else ""


def prune_gone(conn: sqlite3.Connection) -> list[dict]:
    """Drop entries whose directory is *reliably* gone; returns what went.

    The directory is the only thing worth keeping a line for, so an entry the
    operator already removed (recovered by hand, exactly what the docs ask)
    disappears together with the event retention now has to spare. "Gone" is
    :func:`directory_is_gone` — a path we merely cannot stat (unmounted volume,
    unreadable ancestor) keeps its line and its event, because deleting them
    would strand a live directory with no provenance at all.
    """
    registry = registry_path(conn)
    if registry is None:
        return []
    with _registry_lock(registry) as locked:
        if not locked:
            # The registry cannot be rewritten without the lock, so dropping
            # the events here would publish a path whose only pointer is gone —
            # `adopt` cannot restore an event. Nothing is reaped; the next
            # sweep retries once the holder is done (Д-нов-2).
            return []
        current = _read(registry)
        gone_keys = {
            (e["task_id"], e["path"]) for e in current if directory_is_gone(e["path"])
        }
        gone = [e for e in current if (e["task_id"], e["path"]) in gone_keys]
        if not gone:
            return []
        keep = [e for e in current if (e["task_id"], e["path"]) not in gone_keys]
        with contextlib.suppress(OSError):
            _write(registry, keep)
    for entry in gone:
        with contextlib.suppress(sqlite3.Error):
            _forget_events(conn, entry["task_id"], entry["path"])
    return gone
