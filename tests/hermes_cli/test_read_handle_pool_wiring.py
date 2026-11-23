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


# --- Liveness of a pooled handle -------------------------------------------------------------
# A handle parked in the pool can die while idle: the suite's leak sweep closes every SessionDB
# it finds (tests/conftest.py::_close_leaked_session_dbs), a shutdown sweep does the same, and a
# structural-corruption read quarantines the handle. Handing one of those out turns the NEXT
# request into a 500, so the pool checks liveness at both ends.

def test_pool_never_hands_out_a_closed_handle(seeded_home):
    """A handle closed while idle is discarded, and the next read opens a fresh working one."""
    from hermes_cli.read_handle_pool import get_pool

    assert len(_read_once(seeded_home)) == 12
    pool = get_pool()
    idle = [entry for group in pool._idle.values() for entry in group]
    assert len(idle) == 1, "expected exactly one parked handle"

    # Exactly what the conftest sweep does to a handle nobody closed.
    idle[0].db.close()

    assert len(_read_once(seeded_home)) == 12, "a closed pooled handle was handed to the next read"


def test_pool_never_hands_out_a_quarantined_handle(seeded_home):
    """A handle quarantined as corrupt is dropped, not handed to the next read."""
    from hermes_cli.read_handle_pool import get_pool
    from hermes_state_common import stat_db_file_identity

    assert len(_read_once(seeded_home)) == 12
    pool = get_pool()
    idle = [entry for group in pool._idle.values() for entry in group]
    assert len(idle) == 1
    idle[0].db._db_corrupt = True

    key = idle[0].db.db_path.resolve().as_posix()
    assert pool.acquire(key, stat_db_file_identity(seeded_home)) is None, (
        "a quarantined handle was handed out")
    assert pool._total_idle_locked() == 0, "a quarantined handle stayed parked in the pool"
    # And the read path recovers on a fresh handle.
    assert len(_read_once(seeded_home)) == 12


def test_pool_does_not_park_a_dead_handle(seeded_home):
    """release() refuses a handle that died while checked out, instead of pooling it."""
    from hermes_cli.read_handle_pool import _PooledHandle, get_pool
    from hermes_cli.web_server_sessions import _open_session_db_at_path

    db = _open_session_db_at_path(seeded_home, read_only=True)
    assert len(db.list_sessions_rich(limit=5, compact_rows=True)) == 5
    # The wrapped handle dies mid-request (e.g. a shutdown sweep).
    db._db.close()
    db.close()
    assert get_pool()._total_idle_locked() == 0, "a dead handle was parked in the pool"


def test_pool_logs_hit_and_miss_at_debug(seeded_home, caplog):
    """The hit/miss metric the task asks for is on the debug log, one line per acquire."""
    import logging

    from hermes_cli.read_handle_pool import get_pool

    with caplog.at_level(logging.DEBUG, logger="hermes_cli.web_server"):
        assert len(_read_once(seeded_home)) == 12  # MISS + park
        assert len(_read_once(seeded_home)) == 12  # HIT + park
        get_pool().clear()
        assert len(_read_once(seeded_home)) == 12  # MISS again
    lines = [record.getMessage() for record in caplog.records
             if "read-only handle pool" in record.getMessage()]
    verdicts = [line.split("pool ")[1].split()[0] for line in lines]
    assert verdicts == ["MISS", "HIT", "MISS"], verdicts


@pytest.fixture(scope="module")
def shared_state_db(tmp_path_factory):
    """ONE state.db reused by two tests in this module.

    This is the shape the previous revision failed on: the first test parks a read-only handle,
    the suite sweep closes it, and the second test's first read must still work.
    """
    home = tmp_path_factory.mktemp("shared") / "home"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  default: m\n", encoding="utf-8")
    db_path = home / "state.db"
    db = SessionDB(db_path=db_path)
    for i in range(3):
        db.create_session(session_id=f"x{i}", source="cli", model="m")
    db.close()
    from hermes_cli.read_handle_pool import get_pool
    get_pool().clear()
    return db_path


class TestSharedFixtureAcrossTests:
    """Two tests on ONE state.db: the second must not inherit a dead handle from the first."""

    def test_first_read_parks_a_handle(self, shared_state_db):
        assert len(_read_once(shared_state_db)) == 3

    def test_second_read_still_works(self, shared_state_db):
        assert len(_read_once(shared_state_db)) == 3


# --- WAL probe: a failed probe is not an answer ----------------------------------------------

def test_transient_probe_failure_is_not_memoized(seeded_home, monkeypatch):
    """One unlucky ``PRAGMA journal_mode`` must not pin ``None`` on the connection.

    ``None`` means "could not read" (locked/busy, a transient EIO) and callers fail closed on
    it. Memoizing it would disable the lock-free read pool and the WAL-generation guard for the
    life of the connection, so a later call must probe again and see the real mode.
    """
    import hermes_state_wal as wal_mod

    db = SessionDB(db_path=seeded_home, read_only=True)
    try:
        conn = db._conn
        assert conn is not None
        real_probe = wal_mod._probe_journal_mode
        calls = {"n": 0}

        def flaky_probe(c):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # the transient failure
            return real_probe(c)

        _unset = wal_mod._JOURNAL_PROBE_UNSET
        monkeypatch.setattr(wal_mod, "_probe_journal_mode", flaky_probe)
        assert _on_disk_journal_mode(conn) is None
        assert not hasattr(conn, "_journal_mode_memo") or conn._journal_mode_memo is _unset, (
            "the failed probe was memoized")
        assert _on_disk_journal_mode(conn) == real_probe(conn)
        assert calls["n"] >= 2, "the unknown journal mode was not re-probed"
    finally:
        db.close()


# --- concurrent acquire/release: the pool's own bookkeeping must be thread-safe ---------------

def test_concurrent_acquire_release_never_raises_and_leaks_no_descriptors(seeded_home, monkeypatch):
    """8 threads against 3 keys for >1 s: no exception, no descriptor growth, handles preserved.

    Regression for the reviewed defect where ``acquire()`` counted idle entries OUTSIDE ``self._lock``
    (as a logging argument) while iterating the live ``_idle`` dict that ``release()``,
    ``_pop_oldest_locked()`` and ``clear()`` mutate under that lock: ``RuntimeError: dictionary
    changed size during iteration`` flew on the hot path between the pop of the picked entry and the
    return, so the handle was neither handed out nor given back (25 of 25 lost in the reviewer's repro).

    Handles are pre-parked so every iteration is a pool HIT: this test is about the pool's own
    bookkeeping, not about connect cost. A lost handle shows up as a falling live count and as
    descriptors that no longer come back.
    """
    import threading
    import time

    from hermes_cli.read_handle_pool import _ReadOnlyHandlePool
    from hermes_state_readpool import _open_fd_count
    from hermes_state import SessionDB

    pool = _ReadOnlyHandlePool()
    keys = [str(seeded_home), str(seeded_home) + ".b", str(seeded_home) + ".c"]
    identity = ("dev", "ino")  # identical and constant: every parked handle stays valid
    parked = []
    for key in keys:
        db = SessionDB(db_path=seeded_home, read_only=True)
        parked.append(db)
        assert pool.release(key, db, identity) is True

    fd_before = _open_fd_count()
    errors: list[BaseException] = []
    hits = {"n": 0}
    stop = threading.Event()

    def worker(key: str) -> None:
        try:
            while not stop.is_set():
                db = pool.acquire(key, identity)
                if db is None:
                    continue
                hits["n"] += 1
                pool.release(key, db, identity)
        except BaseException as exc:  # reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(keys[i % len(keys)],), daemon=True)
               for i in range(8)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"{len(errors)} exceptions on the hot path, first: {errors[0]!r}"
    assert hits["n"] > 0, "no pool hits at all — the stress loop never exercised reuse"

    # Every parked handle is back in the pool exactly where it started (nothing lost, nothing leaked).
    idle = [entry.db for group in pool._idle.values() for entry in group]
    assert len(idle) == len(parked), f"pool lost handles: {len(idle)} of {len(parked)}"
    assert all(db._conn is not None for db in idle), "a closed handle was parked back in the pool"

    fd_after = _open_fd_count()
    if fd_before is not None and fd_after is not None:
        assert fd_after <= fd_before, f"descriptor count grew {fd_before} -> {fd_after}"

    pool.clear()
