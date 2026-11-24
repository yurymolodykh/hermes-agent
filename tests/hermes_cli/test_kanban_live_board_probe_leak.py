"""Regression: a probe's HERMES_HOME pin cannot reach the live board, and an
authorless card for a non-existent profile cannot land on it.

Field incident (t_37c3fd9a): review-probe scripts (``probe_park*.py``) did
``home = tempfile.mkdtemp(); os.environ["HERMES_HOME"] = home`` before importing
``hermes_cli`` — and still produced 24 cards on the operator's live
``~/.hermes/kanban.db``: empty body, ``assignee='worker'``, ``created_by=NULL``.
They entered the dispatcher queue and the board-graph report (19 findings in
sections D and F).

Two independent roots, both covered here:

1. ``kanban_home()`` ignores ``HERMES_HOME`` **by design** (one board shared by
   every profile), so moving the home does not move the board. The board needs
   its own pin (``HERMES_KANBAN_HOME``), which the test harness now enforces
   fail-closed for the whole run.
2. The anonymous-card fuse only knew the placeholder ids ``{"" , "default"}``.
   ``worker`` is a generic noun no profile directory carries, so the card passed
   the fuse and could never spawn — it only sat in the queue or crash-looped.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

from tests import conftest as _conftest


def _live_anon_unknown_count() -> int | None:
    """Rows on the operator's real board with no author and no real profile.

    Read-only, and only to prove nothing was written. ``None`` when the file is
    absent (no live board on this machine).
    """
    path = _conftest._REAL_KANBAN_ROOT / "kanban.db"
    if not path.exists():
        return None
    roster = sorted(kb.known_profile_names() or {"default"})
    marks = ",".join("?" * len(roster))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE created_by IS NULL "
                f"AND assignee NOT IN ({marks})",
                roster,
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ── 1. Board isolation: the path the harness actually opens ─────────────────


def test_board_path_inside_a_test_is_not_the_live_board():
    """The path every test opens is a tempdir, never the operator's board."""
    resolved = kb.kanban_db_path().expanduser().resolve()
    real_root = _conftest._REAL_KANBAN_ROOT.resolve()
    assert resolved != real_root / "kanban.db", (
        f"kanban_db_path() resolved to the LIVE board {resolved}"
    )
    assert not resolved.is_relative_to(real_root), (
        f"kanban_db_path() resolved under the live root: {resolved}"
    )


def test_sibling_kanban_roots_follow_the_board_pin():
    """Workspaces/attachments must not stay behind on the live root either.

    A card written to the temp board but whose workspace root still resolves to
    ``~/.hermes/kanban/workspaces`` creates directories in the operator's tree —
    invisible to a row-count check, which is why it is asserted separately.
    """
    real_root = _conftest._REAL_KANBAN_ROOT.resolve()
    for path in (kb.workspaces_root(), kb.attachments_root()):
        resolved = path.expanduser().resolve()
        assert not resolved.is_relative_to(real_root), (
            f"kanban sibling root {resolved} still points into the live tree"
        )


def test_a_probe_that_only_moves_hermes_home_still_lands_on_a_temp_board():
    """The exact probe shape from the incident, in a child process.

    A child is required: the point is what a script sees *before* any fixture
    runs. ``HERMES_HOME`` alone is moved; ``HERMES_KANBAN_HOME`` inherits the
    session sandbox the harness pinned, so the resolved DB must be inside that
    sandbox and never the live file.
    """
    script = textwrap.dedent(
        """
        import json, os, tempfile
        probe_home = tempfile.mkdtemp(prefix="probe-home-")
        os.environ["HERMES_HOME"] = probe_home
        os.environ.pop("HERMES_KANBAN_DB", None)
        from hermes_cli import kanban_db as kb
        print(json.dumps({
            "db": str(kb.kanban_db_path().expanduser().resolve()),
            "home": str(kb.kanban_home().expanduser().resolve()),
            "probe_home": probe_home,
        }))
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    proc = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = __import__("json").loads(proc.stdout.strip().splitlines()[-1])
    resolved = __import__("pathlib").Path(payload["db"])
    real_root = _conftest._REAL_KANBAN_ROOT.resolve()
    assert not resolved.is_relative_to(real_root), (
        f"a probe that moved only HERMES_HOME opened the live board: {resolved}"
    )
    # And it really did move the home it claimed to move — the isolation comes
    # from the board pin, not from the probe accidentally inheriting a good home.
    assert payload["probe_home"] in payload["home"] or payload["home"] != str(real_root)


@pytest.mark.allow_real_home_io  # reads the live board READ-ONLY to prove no write
def test_creating_a_card_in_a_test_leaves_the_live_board_untouched():
    """Positive control: writing here must not add an authorless row there."""
    before = _live_anon_unknown_count()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="isolation probe card", assignee="default")
        assert kb.get_task(conn, tid) is not None
    assert _live_anon_unknown_count() == before


# ── 2. Fuse: an authorless assignee no real profile can run ─────────────────


@pytest.fixture()
def live_board(tmp_path, monkeypatch):
    """A throwaway board declared to be the operator's live one.

    Also gives the board a two-profile roster (``default`` + ``worker-like``
    absence), because the fuse deliberately stands down on a roster that lists
    no named profile — a bare tempdir cannot distinguish "unknown name" from
    "single-profile install".
    """
    home = tmp_path / ".hermes"
    (home / "profiles" / "real-profile").mkdir(parents=True)
    (home / "profiles" / "real-profile" / "config.yaml").write_text("model: x\n", encoding="utf-8")
    (home / "profiles" / "real-profile" / "SOUL.md").write_text("soul\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(kb, "_real_platform_kanban_roots",
                        lambda: (home / "kanban.db", home / "kanban" / "boards"))
    # The roster is read through hermes_cli.profiles; pin it to the fixture tree
    # so the assertion is about the fuse's logic, not the host's profile store.
    monkeypatch.setattr(kb, "known_profile_names", lambda: frozenset({"default", "real-profile"}))
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path().resolve()))
    kb.init_db()
    conn = sqlite3.connect(str(kb.kanban_db_path()))
    conn.row_factory = sqlite3.Row
    yield home, conn
    conn.close()


def test_authorless_worker_card_is_refused_on_the_live_board(live_board):
    """RED (t_37c3fd9a): ``assignee="worker"`` passed the fuse and landed live."""
    _, conn = live_board
    with pytest.raises(ValueError, match="no assignee a real profile can run"):
        kb.create_task(conn, title="park spawn_failed, auto-continue ON", assignee="worker")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_authorless_card_for_a_real_profile_still_lands(live_board):
    """The fuse must not block a card addressed to a profile that exists."""
    _, conn = live_board
    tid = kb.create_task(conn, title="real work", assignee="real-profile")
    assert kb.get_task(conn, tid) is not None


def test_authorless_fuse_still_covers_the_placeholder_ids(live_board):
    """``default``/absent keep their original behaviour."""
    _, conn = live_board
    for assignee in ("default", None):
        with pytest.raises(ValueError, match="no assignee a real profile can run"):
            kb.create_task(conn, title="placeholder", assignee=assignee)


def test_an_authored_card_for_a_control_plane_lane_is_allowed(live_board):
    """A named author keeps ``claim_task`` puller lanes (``orion-cc``) working."""
    _, conn = live_board
    tid = kb.create_task(conn, title="lane work", assignee="orion-cc",
                         created_by="01-orchestrator")
    assert kb.get_task(conn, tid) is not None


def test_dispatcher_buckets_a_pre_existing_worker_card(live_board):
    """A ``worker`` row that predates the fuse must not consume a worker slot."""
    _, conn = live_board
    tid = kb.create_task(conn, title="lane seed", assignee="real-profile",
                         created_by="01-orchestrator")
    conn.execute(
        "UPDATE tasks SET created_by = NULL, assignee = 'worker', status = 'ready' WHERE id = ?",
        (tid,),
    )
    conn.commit()
    spawned = []
    res = kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1)
    assert spawned == []
    assert tid in res.skipped_anonymous


def test_throwaway_board_still_accepts_an_anonymous_card(tmp_path, monkeypatch):
    """Every test/probe board stays exempt — the suite's own fixtures rely on it."""
    home = tmp_path / "board"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(kb, "known_profile_names", lambda: frozenset({"default", "real-profile"}))
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path().resolve()))
    kb.init_db()
    conn = sqlite3.connect(str(kb.kanban_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        tid = kb.create_task(conn, title="fixture card", assignee="worker")
        assert kb.get_task(conn, tid) is not None
    finally:
        conn.close()


def test_an_unreadable_roster_stands_the_fuse_down(monkeypatch):
    """Fail-open: a roster that lists no named profile must not refuse cards.

    A single-profile install and a bare probe tempdir both look like this. The
    fuse cannot tell ``worker`` from a real route there, so it stays silent —
    the alternative (fail closed) would reject legitimate cards on a healthy
    install, which is the worse failure.
    """
    monkeypatch.setattr(kb, "known_profile_names", lambda: None)
    assert kb.names_no_real_profile("worker") is False
    assert kb.is_anonymous_placeholder_card("worker", None) is False
    # Placeholder ids stay refused regardless of the roster.
    assert kb.is_anonymous_placeholder_card("default", None) is True


def test_roster_lookup_ignores_a_default_only_store(monkeypatch):
    """``default`` alone carries no signal about an unknown name."""
    monkeypatch.setattr("hermes_cli.profiles.list_profile_names", lambda: ["default"])
    assert kb.known_profile_names() is None


def test_roster_lookup_reads_named_profiles(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.list_profile_names", lambda: ["default", "Real-Profile", "  "],
    )
    assert kb.known_profile_names() == frozenset({"default", "real-profile"})
