"""Empty-session hygiene: the "Delete empty (N)" badge and its sweep (SessionDB mixin).

Split out of ``hermes_state_sessions.py`` (which is at its line cap) when the dashboard's empty
sweep became owner-scoped: the badge and the deletion must agree on which rows are "empty", so
they share one WHERE clause, and both now take the caller's owner scope.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from hermes_state_common import (
    _id_chunks, _SQL_IN_CHUNK, _placeholders as _session_ids_placeholders, emit_session_deleted,
)
from hermes_state_owner_scope import scoped_where


class SessionEmptyHygieneMixin:
    """``count_empty_sessions`` / ``delete_empty_sessions`` for a :class:`~hermes_state.SessionDB`."""

    # Shared by count_empty_sessions / delete_empty_sessions so badge and sweep agree. message_count
    # counts live rows only (rewind/compaction keep dropped turns as active = 0): NOT EXISTS is
    # the authority, ``message_count = 0`` stays as a cheap prefilter. (#95868)
    _EMPTY_SESSION_WHERE = (
        "message_count = 0 AND ended_at IS NOT NULL AND archived = 0 AND NOT EXISTS ("
        "SELECT 1 FROM messages WHERE messages.session_id = sessions.id)"
    )

    def count_empty_sessions(self, user_id: str | Sequence[str] | None = None) -> int:
        """Count of empty, ended, non-archived sessions; ended_at guards a fresh session's first message.
        ``user_id`` scopes the count to one owner's rows (``None`` unscoped, empty = nothing)."""
        where, params = scoped_where(self._EMPTY_SESSION_WHERE, "user_id", user_id)
        return self._read_one(f"SELECT COUNT(*) FROM sessions WHERE {where}", params)[0]

    def delete_empty_sessions(self, sessions_dir: Optional[Path] = None,
                              user_id: str | Sequence[str] | None = None) -> int:
        """Delete every empty, ended, non-archived session in one transaction, orphaning (not cascading)
        children; transcript files are swept too. ``user_id`` restricts the sweep to one owner's rows
        (``None`` = the whole store, the CLI's own behaviour)."""
        scope_where, scope_params = scoped_where(self._EMPTY_SESSION_WHERE, "user_id", user_id)
        removed_ids: list[str] = []

        def _do(conn):
            session_ids = {row["id"] for row in conn.execute(
                f"SELECT id FROM sessions WHERE {scope_where}", scope_params
            ).fetchall()}
            from hermes_state_mutation_retirement import retire_prunable
            session_ids = retire_prunable(conn, sorted(session_ids))
            if not session_ids:
                return 0
            for chunk in _id_chunks(session_ids):
                ph = _session_ids_placeholders(chunk)
                conn.execute(f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({ph})", chunk)
                # DELETE FROM messages: a row inserted between the SELECT and here
                # would otherwise dangle (clean FK state).
                conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", chunk)
                removed_ids.extend(chunk)
            self._delete_unreferenced_system_prompts(conn)
            return len(session_ids)

        count = self._execute_write(_do)
        if count:
            emit_session_deleted(self, removed_ids, reason="empty_sweep", sessions_dir=sessions_dir)
        from hermes_state_media import collect_retired_media
        collect_retired_media(self)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count
