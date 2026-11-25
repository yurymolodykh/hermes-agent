"""The hosted-room readiness check must stay cheap AND keep detecting a schema change.

``_store_ready`` runs on every store open, and it used to re-read ``PRAGMA table_info`` for every
required table plus two index lookups each time — measured at ~12% of the gateway's hot loop. The
verdict is now memoised per (database file identity, ``PRAGMA schema_version``): SQLite bumps that
counter on every DDL statement, so a migration performed by ANY process sharing the database must
still flip the verdict on the very next open, while a steady-state open costs two cheap reads.

The behaviour the store promises is unchanged: a database whose schema is behind raises
``HostedRoomError`` from the same ladder as before.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from gateway import hosted_rooms


@pytest.fixture(autouse=True)
def _fresh_schema_cache():
    hosted_rooms.clear_schema_cache()
    yield
    hosted_rooms.clear_schema_cache()


def _make_store(tmp_path):
    """A current hosted-room store, then drop the cache so the next probe reads it for real."""
    db = tmp_path / "shared-state.db"
    hosted_rooms.create_room(
        db, room_id="room-1", name="Release room",
        members=[{"profile": "default", "handle": "hermes"}],
        authority_gateway_id="gateway-a")
    hosted_rooms.clear_schema_cache()
    return db


def test_steady_state_open_skips_the_table_info_scan(tmp_path, monkeypatch):
    """The memoised path must not run the per-table column probe twice for one schema version."""
    db = _make_store(tmp_path)
    scans = []
    real = hosted_rooms.table_columns

    def counting(conn, table):
        scans.append(table)
        return real(conn, table)

    monkeypatch.setattr(hosted_rooms, "table_columns", counting)

    assert hosted_rooms._store_ready(hosted_rooms.open_sqlite(db), db) is True
    first = len(scans)
    assert first > 0, "the first probe on a cold cache must read the real schema"

    hosted_rooms._store_ready(hosted_rooms.open_sqlite(db), db)
    assert len(scans) == first, (
        "a second open with an unchanged schema_version re-ran the table_info scan "
        f"({len(scans) - first} extra table reads)")


def test_ddl_change_is_detected_on_the_next_open(tmp_path):
    """A column added by another process must be seen immediately — not after a TTL.

    This is the guard against "caching the schema silently stops noticing migrations".
    """
    db = _make_store(tmp_path)

    conn = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._store_ready(conn, db) is True  # prime the memo for this version
    finally:
        conn.close()

    # Another process migrates the shared database (a plain DDL statement bumps schema_version).
    other = sqlite3.connect(db)
    try:
        other.execute("ALTER TABLE hosted_rooms ADD COLUMN probe_added_column TEXT")
        other.commit()
    finally:
        other.close()

    conn = hosted_rooms.open_sqlite(db)
    try:
        version_after = conn.execute("PRAGMA schema_version").fetchone()[0]
        assert hosted_rooms._schema_is_current_cached(conn, db) is True
        # The cache entry now names the NEW version, so the verdict is fresh, not inherited.
        identity = hosted_rooms._file_identity(db)
        assert identity is not None
        assert hosted_rooms._schema_cache[identity[0]][1] == version_after
    finally:
        conn.close()

    # And a DDL that REMOVES a required column must fail the check, exactly as before the cache.
    other = sqlite3.connect(db)
    try:
        other.execute("ALTER TABLE hosted_rooms RENAME COLUMN members_json TO members_json_gone")
        other.commit()
    finally:
        other.close()

    conn = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._schema_is_current_cached(conn, db) is False
        assert hosted_rooms._store_ready(conn, db) is False
    finally:
        conn.close()


def test_missing_schema_is_healed_then_current_exactly_as_before(tmp_path):
    """An uninitialised database keeps the same contract: not ready, then healed by the store open."""
    db = tmp_path / "shared-state.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    hosted_rooms.clear_schema_cache()

    probe = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._store_ready(probe, db) is False, "an empty schema is not ready"
        assert hosted_rooms._schema_is_current_cached(probe, db) is False
    finally:
        probe.close()

    # The public path initialises it (one IMMEDIATE transaction) — unchanged behaviour.
    assert hosted_rooms.list_rooms(db) == []
    healed = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._store_ready(healed, db) is True
        assert hosted_rooms._schema_version(healed) is not None
    finally:
        healed.close()


def test_replaced_file_is_not_served_from_the_old_verdict(tmp_path):
    """A different inode (a restored/moved database) must not inherit the previous file's verdict."""
    db = _make_store(tmp_path)
    conn = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._store_ready(conn, db) is True
    finally:
        conn.close()

    db.unlink()
    empty = sqlite3.connect(db)
    try:
        empty.execute("CREATE TABLE unrelated (x INTEGER)")
        empty.commit()
    finally:
        empty.close()

    conn = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._store_ready(conn, db) is False
    finally:
        conn.close()


def test_read_path_still_lists_rooms_after_the_cache(tmp_path):
    """The public read path keeps working (and keeps its read-only contract)."""
    db = _make_store(tmp_path)
    rows = hosted_rooms.list_rooms(db)
    assert [row["room_id"] for row in rows] == ["room-1"]


def test_fleet_larger_than_the_old_ceiling_keeps_every_entry(tmp_path, monkeypatch):
    """A fleet of 41 databases stays fully memoised: pass 1 scans, passes 2-3 do not.

    Regression for the live fleet (41 databases): with the old ``_SCHEMA_CACHE_MAX_FILES = 16``
    ceiling, every pass evicted the entries the previous pass had just written, so 123 of 123
    probes over three passes ran the full ``PRAGMA table_info`` scan (~2928 then ~656 µs each).
    """
    fleet = 41
    dbs = []
    for index in range(fleet):
        db = tmp_path / f"db-{index}.db"
        hosted_rooms.create_room(
            db, room_id=f"room-{index}", name=f"Room {index}",
            members=[{"profile": "default", "handle": "hermes"}],
            authority_gateway_id="gateway-a")
        dbs.append(db)
    hosted_rooms.clear_schema_cache()
    hosted_rooms.clear_identity_cache()

    scans = []
    real = hosted_rooms._schema_is_current
    monkeypatch.setattr(hosted_rooms, "_schema_is_current",
                        lambda conn: (scans.append(1), real(conn))[1])

    conns = [(db, hosted_rooms.open_sqlite(db)) for db in dbs]
    try:
        per_pass = []
        for _ in range(3):
            scans.clear()
            for db, conn in conns:
                assert hosted_rooms._schema_is_current_cached(conn, db) is True
            per_pass.append(len(scans))
    finally:
        for _, conn in conns:
            conn.close()

    assert per_pass == [fleet, 0, 0], f"full scans per pass {per_pass}, expected [{fleet}, 0, 0]"
    metrics = hosted_rooms.schema_cache_metrics()
    assert metrics["evictions"] == 0, metrics
    assert metrics["entries"] == fleet, metrics


def test_metrics_report_entries_and_evictions_instead_of_silently_trimming(tmp_path, monkeypatch):
    """The ceiling is an observable bound: evictions are counted and logged, never a silent trim."""
    monkeypatch.setattr(hosted_rooms, "_SCHEMA_CACHE_MAX_FILES", 2)
    hosted_rooms.clear_schema_cache()
    hosted_rooms.clear_identity_cache()
    before = hosted_rooms.schema_cache_metrics()["evictions"]
    for index in range(4):
        db = tmp_path / f"db-{index}.db"
        hosted_rooms.create_room(
            db, room_id=f"room-{index}", name=f"Room {index}",
            members=[{"profile": "default", "handle": "hermes"}],
            authority_gateway_id="gateway-a")
        conn = hosted_rooms.open_sqlite(db)
        try:
            assert hosted_rooms._store_ready(conn, db) is True
        finally:
            conn.close()
    metrics = hosted_rooms.schema_cache_metrics()
    assert metrics["ceiling"] == 2
    assert metrics["entries"] <= 2
    assert metrics["evictions"] == before + 2, metrics
    assert metrics["scans"] >= 4


def test_changed_mtime_ns_invalidates_the_entry(tmp_path, monkeypatch):
    """A rewrite that keeps the inode (and the schema) must be re-probed: mtime_ns is part of the key.

    The stat identity in the key is what catches a file replaced *in place* — same ``st_dev``/
    ``st_ino``, but a new ``st_mtime_ns`` — which a (dev, ino) key alone would answer from stale state.
    """
    db = _make_store(tmp_path)
    conn = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._schema_is_current_cached(conn, db) is True
        first = hosted_rooms._file_identity(db)
        assert first is not None
    finally:
        conn.close()

    scans = []
    real = hosted_rooms._schema_is_current
    monkeypatch.setattr(hosted_rooms, "_schema_is_current",
                        lambda c: (scans.append(1), real(c))[1])

    stat = os.stat(db)
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    conn = hosted_rooms.open_sqlite(db)
    try:
        assert hosted_rooms._schema_is_current_cached(conn, db) is True
        second = hosted_rooms._file_identity(db)
        assert second is not None and second[1:] != first[1:], "the stat identity did not move"
    finally:
        conn.close()
    assert scans, "a changed mtime_ns left the entry valid — mtime_ns is not in the key"
