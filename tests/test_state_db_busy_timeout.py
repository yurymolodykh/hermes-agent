"""PRAGMA busy_timeout floor on the session-store connection (#t_7b540600).

The 2026-10-09 01:21:13 incident: an on-loop SessionDB write (`_execute_write`
-> `BEGIN IMMEDIATE`) starved under lock contention, froze the event loop past
the liveness window, and the watchdog hard-exited the gateway with code 75.
Setting a >=20s busy_timeout at the session-store connect points makes a
legitimate lock holder wait inside SQLite's busy handler instead of surfacing
"database is locked" on millisecond contention.
"""
from __future__ import annotations

import sqlite3

from hermes_state import (
    _MIN_STATE_DB_BUSY_TIMEOUT_MS,
    _apply_state_db_busy_timeout,
)


def test_min_busy_timeout_floor_is_20s():
    assert _MIN_STATE_DB_BUSY_TIMEOUT_MS == 20_000


def test_apply_state_db_busy_timeout_defaults_to_floor(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_STATE_BUSY_TIMEOUT_MS", raising=False)
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    try:
        _apply_state_db_busy_timeout(con)
        (busy,) = con.execute("PRAGMA busy_timeout").fetchone()
        assert busy >= _MIN_STATE_DB_BUSY_TIMEOUT_MS
    finally:
        con.close()


def test_apply_state_db_busy_timeout_env_override_floored(tmp_path, monkeypatch):
    # A too-small env value must be floored to the 20s minimum.
    monkeypatch.setenv("HERMES_STATE_BUSY_TIMEOUT_MS", "1")
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    try:
        _apply_state_db_busy_timeout(con)
        (busy,) = con.execute("PRAGMA busy_timeout").fetchone()
        assert busy >= _MIN_STATE_DB_BUSY_TIMEOUT_MS
    finally:
        con.close()


def test_apply_state_db_busy_timeout_accepts_larger_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_BUSY_TIMEOUT_MS", "60000")
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    try:
        _apply_state_db_busy_timeout(con)
        (busy,) = con.execute("PRAGMA busy_timeout").fetchone()
        assert busy == 60_000
    finally:
        con.close()


def test_apply_state_db_busy_timeout_garbage_env_is_safe(tmp_path, monkeypatch):
    # Malformed env must fall back to the floor without raising.
    monkeypatch.setenv("HERMES_STATE_BUSY_TIMEOUT_MS", "not-a-number")
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    try:
        _apply_state_db_busy_timeout(con)
        (busy,) = con.execute("PRAGMA busy_timeout").fetchone()
        assert busy >= _MIN_STATE_DB_BUSY_TIMEOUT_MS
    finally:
        con.close()


def test_apply_state_db_busy_timeout_never_raises_on_bad_con(tmp_path):
    # A closed connection must not propagate an exception.
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.close()
    _apply_state_db_busy_timeout(con)  # must not raise
