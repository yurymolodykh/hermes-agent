"""The hosted-room read path must not open a connection (or re-probe the schema) per call.

``list_rooms`` runs on the room cycle's every-5-second tick, and each call used to open a
fresh sqlite3 connection to the shared store. Opening a connection costs the readiness
probe, and on a cold memo that probe runs one ``PRAGMA table_info`` per required table —
the ``table_columns`` frame py-spy measured at 18.6% of the gateway's samples.

The contract pinned here is behavioural, not structural: reads keep returning the same
rows, and both the connection and the schema scan are paid ONCE for an unchanged file.
"""

from __future__ import annotations

import sqlite3

import pytest

from gateway import hosted_rooms


@pytest.fixture(autouse=True)
def _fresh_caches():
    hosted_rooms.clear_schema_cache()
    hosted_rooms.clear_identity_cache()
    hosted_rooms.clear_read_connections()
    yield
    hosted_rooms.clear_schema_cache()
    hosted_rooms.clear_identity_cache()
    hosted_rooms.clear_read_connections()


def _make_store(tmp_path):
    db = tmp_path / "shared-state.db"
    hosted_rooms.create_room(
        db, room_id="room-1", name="Release room",
        members=[{"profile": "default", "handle": "hermes"}],
        authority_gateway_id="gateway-a")
    hosted_rooms.clear_schema_cache()
    return db


def test_repeated_list_rooms_does_not_open_a_second_connection(tmp_path, monkeypatch):
    """Two consecutive reads of an unchanged store share ONE connection.

    This is the property the room cycle depends on: at 5 s per tick a per-call open is
    ~17 000 connections a day for data that did not change.
    """
    db = _make_store(tmp_path)
    opens = []
    real_open = hosted_rooms._open_shared_sqlite

    def counting_open(path):
        opens.append(str(path))
        return real_open(path)

    monkeypatch.setattr(hosted_rooms, "_open_shared_sqlite", counting_open)

    first = hosted_rooms.list_rooms(db)
    second = hosted_rooms.list_rooms(db)

    assert [row["room_id"] for row in first] == ["room-1"]
    assert second == first, "the reused connection returned different rows"
    assert len(opens) == 1, f"a repeat read opened {len(opens)} connections, not one"


def test_repeated_list_rooms_does_not_rescan_the_schema(tmp_path, monkeypatch):
    """The second read of an unchanged store runs no ``PRAGMA table_info``."""
    db = _make_store(tmp_path)
    scans = []
    real = hosted_rooms.table_columns

    def counting(conn, table):
        scans.append(table)
        return real(conn, table)

    monkeypatch.setattr(hosted_rooms, "table_columns", counting)

    hosted_rooms.list_rooms(db)
    first = len(scans)
    assert first > 0, "the first read of a cold store must probe the real schema"

    hosted_rooms.list_rooms(db)
    assert len(scans) == first, (
        f"a repeat read re-ran the schema scan ({len(scans) - first} extra table reads)")


def test_connection_is_reestablished_when_the_file_is_replaced(tmp_path):
    """A store replaced under the process (restore, move) must not be read through the old handle.

    Guards the reuse from silently serving a deleted inode's rows.
    """
    db = _make_store(tmp_path)
    assert [row["room_id"] for row in hosted_rooms.list_rooms(db)] == ["room-1"]

    db.unlink()
    hosted_rooms.create_room(
        db, room_id="room-2", name="Restored room",
        members=[{"profile": "default", "handle": "hermes"}],
        authority_gateway_id="gateway-a")

    assert [row["room_id"] for row in hosted_rooms.list_rooms(db)] == ["room-2"]


def test_migration_by_another_process_is_still_detected(tmp_path):
    """A DDL from another process flips the readiness verdict on the very next read.

    ``_store_ready`` re-reads ``PRAGMA schema_version`` on every call even though the
    connection is reused, so the cache cannot mask a concurrent migration.
    """
    db = _make_store(tmp_path)
    assert [row["room_id"] for row in hosted_rooms.list_rooms(db)] == ["room-1"]

    other = sqlite3.connect(db)
    try:
        other.execute("ALTER TABLE hosted_rooms RENAME COLUMN members_json TO members_json_gone")
        other.commit()
    finally:
        other.close()

    # The breakage must surface as the same error the store always raised, not as stale rows.
    with pytest.raises(hosted_rooms.HostedRoomError):
        hosted_rooms.list_rooms(db)


def test_read_connection_is_shared_across_threads_safely(tmp_path):
    """Concurrent readers on the shared handle see every room, none raises."""
    import threading

    db = _make_store(tmp_path)
    hosted_rooms.create_room(
        db, room_id="room-2", name="Second room",
        members=[{"profile": "default", "handle": "hermes"}],
        authority_gateway_id="gateway-a")

    errors: list[BaseException] = []
    seen: list[set[str]] = []

    def reader():
        try:
            seen.append({row["room_id"] for row in hosted_rooms.list_rooms(db)})
        except BaseException as exc:  # reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"{len(errors)} reader(s) failed, first: {errors[0]!r}"
    assert len(seen) == 8
    assert all(rooms == {"room-1", "room-2"} for rooms in seen), seen
