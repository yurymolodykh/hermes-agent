"""Invalidate read-once assumptions when a task loses transcript content."""

import logging

logger = logging.getLogger(__name__)


def invalidate_context_read_dedup(task_id: str | None) -> None:
    """Allow full file/skill reloads after a committed prune or compaction.

    The tool reset APIs interpret an absent task as a process-wide reset.
    Never let a missing boundary scope evict unrelated conversations.
    """
    if not task_id:
        return
    try:
        from tools.file_tools import reset_file_dedup

        reset_file_dedup(task_id)
    except Exception:
        logger.debug("Could not reset file-read dedup after context loss", exc_info=True)
    try:
        from tools.skills_tool import reset_skill_view_dedup

        reset_skill_view_dedup(task_id)
    except Exception:
        logger.debug("Could not reset skill-view dedup after context loss", exc_info=True)
