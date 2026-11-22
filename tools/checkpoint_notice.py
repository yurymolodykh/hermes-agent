"""Put a checkpoint failure on the *card*, not only in ``agent.log``.

Why: the checkpoint manager's failures were logged and nothing else.  A killed
worker left ``indexes/<hash>.lock`` behind, every later ``git add -A`` of that
project failed with ``rc=128``, and 458 of those messages sat in three profiles'
``agent.log`` while the humans running the cards saw working checkpoints
silently stop.  Logging is not a visible channel.

A dispatched worker (``HERMES_KANBAN_TASK``) gets one ``kanban_comment`` on its
own card per distinct message: the comment lands in the card thread, in the
event stream (``commented``) and therefore in whatever the subscriber watches.
Everything is best-effort and throttled — a checkpoint must never fail, block,
or spam the board because the notice could not be delivered.
"""
from __future__ import annotations

import logging
import os
import zlib
from typing import Optional

logger = logging.getLogger(__name__)

AUTHOR = "checkpoint-manager"

_sent: set[int] = set()


def _worker_task() -> Optional[str]:
    """The card this process is working on, or ``None`` outside a dispatched worker."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return None
    try:
        from agent.delegation_context import is_delegated_child_context
    except ImportError:
        return task_id  # built outside a full tree: nothing to distinguish
    # A delegated child has no card of its own; its parent owns the notice.
    return None if is_delegated_child_context() else task_id


def post_worker_notice(message: str) -> bool:
    """Comment ``message`` on the worker's card once per process; ``True`` if stored."""
    task_id = _worker_task()
    if task_id is None or not (message or "").strip():
        return False
    digest = zlib.crc32(message.encode("utf-8", "replace"))
    if digest in _sent:
        return False
    _sent.add(digest)
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None
        conn = kbc.connect(board=board)
        try:
            kb.add_comment(conn, task_id, author=AUTHOR, body=message)
        finally:
            conn.close()
        return True
    except Exception:  # a notice must never break the turn
        logger.debug("Could not post checkpoint notice to card %s", task_id, exc_info=True)
        return False
