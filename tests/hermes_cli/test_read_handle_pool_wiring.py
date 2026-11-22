"""t_f8450188 — the dashboard read path must REUSE a read-only handle, and the WAL probe must be
paid once per connection.

Two contracts, both facts a reviewer can check by counting:

* **Pool reuse.** A series of dashboard reads (``_open_session_db_at_path(read_only=True)`` then
  ``close()``, exactly what the routers do) must issue exactly ONE ``sqlite3.connect`` for the whole
  series instead of one per read. Proved by wrapping ``sqlite3.connect``.
* **Same initialization for every pooled handle.** SQLite keeps ``PRAGMA`` state per connection, so
  a pool that hands out a mis-initialized handle changes the data semantics silently. A handle the
  pool reuses must carry the same pragmas a freshly opened one gets, and the WAL probe must have
  been paid on it exactly once (the memo), while a NEW connection probes again.
"""

import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_wal import _on_disk_journal_mode, _probe_journal_mode


@pytest.fixture()
def seeded_home(tmp_path, monkeypatch):
    """A profile home with an initialized state.db holding a few sessions."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
        "  synchronous: FULL\nsessions:\n  auto_archive: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = home / "state.db"
    db = SessionDB(db_path=db_path)
    for i in range(12):
        db.create_session(session_id=f"s{i}", source="cli", model="m")
        db.append_message(f"s{i}", role="user", content=f"hello {i}")
    db.close()
    from hermes_cli.read_handle_pool import get_pool
    get_pool().clear()
    yield db_path
    get_pool().clear()


def _read_once(db_path):
    from hermes_cli.web_server_sessions import _open_session_db_at_path
    db = _open_session_db_at_path(db_path, read_only=True)
    try:
        return db.list_sessions_rich(limit=50, compact_rows=True)
    finally:
        db.close()


def test_dashboard_reads_reuse_one_connection(seeded_home, monkeypatch):
    """20 sequential dashboard reads must open ONE connection, not 20."""
    opens = {"n": 0}
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        opens["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    first = _read_once(seeded_home)
    assert len(first) == 12
    after_first = opens["n"]
    assert after_first == 1, f"first read should open exactly one connection, opened {after_first}"

    for _ in range(19):
        assert len(_read_once(seeded_home)) == 12

    assert opens["n"] == 1, (
        f"pool must reuse its handle: 20 reads opened {opens['n']} connections "
        f"(one per read means the pool is not being used)")


def test_pooled_handle_keeps_fresh_connection_pragmas(seeded_home):
    """A reused handle must carry the same PRAGMA state a freshly opened read-only handle gets.

    SQLite stores these per connection, so this is the check that the pool is not handing out a
    connection initialized differently from a cold open.
    """
    from hermes_cli.web_server_sessions import _open_session_db_at_path

    def pragmas(db):
        conn = db._conn
        return {
            "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
            "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
            "cache_size": conn.execute("PRAGMA cache_size").fetchone()[0],
            "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
        }

    cold = _open_session_db_at_path(seeded_home, read_only=True)
    try:
        fresh_state = pragmas(cold)
    finally:
        cold.close()

    warm = _open_session_db_at_path(seeded_home, read_only=True)
    try:
        warm_state = pragmas(warm)
    finally:
        warm.close()

    assert warm_state == fresh_state, (
        "pooled read-only handle was initialized differently from a fresh one: "
        f"fresh={fresh_state} pooled={warm_state}")


def test_wal_probe_is_paid_once_per_connection(seeded_home):
    """The journal-mode probe is memoized ON the connection: a second ask costs no PRAGMA.

    A brand-new connection has no memo, so the probe is mandatory on (re)connect by construction —
    that is the rule, not a reset someone must remember to call.
    """
    db = SessionDB(db_path=seeded_home, read_only=True)
    try:
        conn = db._conn
        assert conn is not None
        assert _on_disk_journal_mode(conn) == _probe_journal_mode(conn)
        assert getattr(conn, "_journal_mode_memo", None) is not None

        calls = {"n": 0}
        real_probe = _probe_journal_mode

        import hermes_state_wal as wal_mod

        def counting_probe(c):
            calls["n"] += 1
            return real_probe(c)

        original = wal_mod._probe_journal_mode
        wal_mod._probe_journal_mode = counting_probe
        try:
            for _ in range(5):
                _on_disk_journal_mode(conn)
        finally:
            wal_mod._probe_journal_mode = original
        assert calls["n"] == 0, "memoized journal mode re-probed the connection"
    finally:
        db.close()

    fresh = SessionDB(db_path=seeded_home, read_only=True)
    try:
        assert fresh._conn is not None
        assert getattr(fresh._conn, "_journal_mode_memo", None) is None, (
            "a fresh connection must have no memo — the probe is mandatory on reconnect")
        assert _on_disk_journal_mode(fresh._conn) is not None
        assert fresh._conn._journal_mode_memo is not None
    finally:
        fresh.close()
