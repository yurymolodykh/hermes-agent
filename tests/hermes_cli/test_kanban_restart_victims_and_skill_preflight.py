"""Root-cause fixes for the 2026-10-10 gateway-restart incident (t_d9451f51).

Covers the two dispatcher defect classes with isolated DB copies:

A1 — restart victims auto-release. A ``running`` card claimed by a PREVIOUS
dispatcher (dead claimer pid in ``claim_lock`` / foreign
``gateway_generation``) whose Windows exit status is unknown must be
requeued to its source phase with NO needs_input question and NO failure
counter tick — mirroring the provider_unavailable auto-release pattern.

A2 — skill spawn preflight. A card whose force-loaded skills cannot resolve
in the assignee profile's context must be blocked BEFORE spawn, with the
skill name and candidate paths in the event / last_failure_error, instead of
spawning a worker that exit-1s on startup and crash-loops (t_9b53a6bc).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a kanban DB (mirrors test_generation_reclaim)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _connect(home) -> sqlite3.Connection:
    conn = sqlite3.connect(
        kb.kanban_db_path(),
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _running_restart_victim(
    conn,
    *,
    generation: "str | None",
    claimer_pid: int,
    worker_pid: int = 999999,
):
    """Create a task whose running claim belongs to a DEAD dispatcher.

    ``worker_pid`` defaults to a pid that is not alive on any host, so the
    liveness check in ``detect_crashed_workers`` fires deterministically.
    """
    tid = kb.create_task(conn, title="restart victim", assignee="alice")
    kb.claim_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET gateway_generation = ?, worker_pid = ?, "
        "claim_lock = ? WHERE id = ?",
        (
            generation,
            worker_pid,
            f"{kb.socket_hostname()}:{claimer_pid}",
            tid,
        ),
    )
    conn.commit()
    return tid


def _events(conn, tid, kind=None):
    q = "SELECT kind, payload FROM task_events WHERE task_id = ?"
    args: tuple = (tid,)
    if kind:
        q += " AND kind = ?"
        args = (tid, kind)
    rows = conn.execute(q + " ORDER BY id", args).fetchall()
    return [(r["kind"], json.loads(r["payload"] or "{}")) for r in rows]


# ---------------------------------------------------------------------------
# A2: skill preflight blocks doomed spawns with a precise reason
# ---------------------------------------------------------------------------


class TestSkillSpawnPreflight:
    @pytest.fixture(autouse=True)
    def _route_resolver(self, isolated_kanban_route_resolver):
        """Stub provider-route checks so dispatch reaches the skill preflight
        (otherwise the provider pre-flight blocks the card first — that is
        the correct order in production, but these tests target skills)."""
        yield

    @pytest.fixture
    def assignee_profile(self, kanban_home, monkeypatch):
        """An 'alice' assignee whose profile home resolves to an isolated dir.

        ``resolve_profile_env`` is stubbed (same pattern as
        test_kanban_assignee_route_fallback) so ``_assignee_home`` switches
        HERMES_HOME to a directory whose skills/config the preflight reads.
        """
        import hermes_cli.profiles as profiles

        monkeypatch.setattr(
            profiles, "profile_exists", lambda _n: True
        )
        root = Path(kanban_home).parent
        prof = root / "profiles" / "alice"
        (prof / "skills" / "useful-skill").mkdir(parents=True, exist_ok=True)
        (prof / "skills" / "useful-skill" / "SKILL.md").write_text(
            "---\nname: useful-skill\ndescription: test skill\n---\nbody\n",
            encoding="utf-8",
        )
        (prof / "config.yaml").write_text(
            "model:\n  provider: test\n  default: m1\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            profiles, "resolve_profile_env", lambda _name: str(prof)
        )
        return prof

    def _task_with_skills(self, skills):
        class _T:
            id = "t_test_skill"
            assignee = "alice"
            workspace_kind = None

        t = _T()
        t.skills = skills
        return t

    def test_resolving_skill_passes(self, kanban_home, assignee_profile):
        task = self._task_with_skills(["useful-skill"])
        assert kb._preflight_task_skills(task, str(assignee_profile)) is None

    def test_missing_skill_blocks_with_reason_and_candidates(
        self, kanban_home, assignee_profile
    ):
        task = self._task_with_skills(["nope-skill"])
        reason = kb._preflight_task_skills(task, str(assignee_profile))
        assert reason is not None
        assert "nope-skill" in reason
        assert "missing" in reason.lower() or "does not resolve" in reason
        assert "alice" in reason

    def test_collision_reports_both_paths(
        self, kanban_home, assignee_profile
    ):
        # Second copy of the same skill name in an external dir.
        ext = Path(assignee_profile).parent / "ext-skills"
        (ext / "useful-skill").mkdir(parents=True, exist_ok=True)
        (ext / "useful-skill" / "SKILL.md").write_text(
            "---\nname: useful-skill\ndescription: dup\n---\nbody\n",
            encoding="utf-8",
        )
        cfg = Path(assignee_profile) / "config.yaml"
        cfg.write_text(
            "model:\n  provider: test\n  default: m1\n"
            f"skills:\n  external_dirs:\n    - {ext.as_posix()}\n",
            encoding="utf-8",
        )
        task = self._task_with_skills(["useful-skill"])
        reason = kb._preflight_task_skills(task, str(assignee_profile))
        assert reason is not None
        assert "collision" in reason
        assert str(ext) in reason, "both competing paths must be named"
        assert "useful-skill" in reason

    def test_review_lane_sdlc_review_missing_blocks_before_spawn(
        self, kanban_home, assignee_profile, monkeypatch
    ):
        """End-to-end review-lane shape (t_9b53a6bc): a review card whose
        assignee cannot resolve sdlc-review is blocked at dispatch instead
        of spawning a doomed exit-1 worker."""
        conn = _connect(kanban_home)
        try:
            tid = kb.create_task(
                conn,
                title="review doom",
                assignee="alice",
                skills=["sdlc-review"],
                model_override="m1",
                provider_override="test",
            )
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (tid,)
            )
            conn.commit()
            spawned = []
            result = kb.dispatch_once(
                conn,
                spawn_fn=lambda task, ws, **kw: spawned.append(task.id) or 12345,
                dry_run=False,
            )
            assert spawned == [], "doomed review worker must NOT spawn"
            assert tid in result.auto_blocked
            row = conn.execute(
                "SELECT status, block_kind, last_failure_error FROM tasks "
                "WHERE id = ?",
                (tid,),
            ).fetchone()
            assert row["status"] == "blocked"
            assert row["block_kind"] == "capability"
            assert "sdlc-review" in (row["last_failure_error"] or "")
            row = conn.execute(
                "SELECT consecutive_failures FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            assert (
                row["consecutive_failures"] == 0
            ), "retry budget must not burn on a pre-spawn block"
            kinds = [k for k, _ in _events(conn, tid)]
            assert "blocked" in kinds
            payload = dict(_events(conn, tid, "blocked")[0][1])
            assert "sdlc-review" in payload.get("reason", "")
        finally:
            conn.close()

    def test_review_lane_collision_blocks_before_spawn(
        self, kanban_home, assignee_profile
    ):
        """Collision half of A2 (the 09-independent-reviewer shape of the
        t_9b53a6bc incident): a local sdlc-review copy PLUS an external one
        via skills.external_dirs make the resolver treat the skill as
        missing. The card must be blocked BEFORE spawn with BOTH competing
        paths on the board, and the retry budget untouched."""
        prof = Path(assignee_profile)
        # Local copy inside the assignee profile (the flat v1.2.0 shape).
        (prof / "skills" / "sdlc-review").mkdir(parents=True, exist_ok=True)
        (prof / "skills" / "sdlc-review" / "SKILL.md").write_text(
            "---\nname: sdlc-review\ndescription: local v1.2.0\n---\nbody\n",
            encoding="utf-8",
        )
        # External copy via skills.external_dirs (the v1.1.0 shape).
        ext = prof.parent / "ext-review-skills"
        (ext / "sdlc-review").mkdir(parents=True, exist_ok=True)
        (ext / "sdlc-review" / "SKILL.md").write_text(
            "---\nname: sdlc-review\ndescription: external v1.1.0\n---\nbody\n",
            encoding="utf-8",
        )
        (prof / "config.yaml").write_text(
            "model:\n  provider: test\n  default: m1\n"
            f"skills:\n  external_dirs:\n    - {ext.as_posix()}\n",
            encoding="utf-8",
        )
        conn = _connect(kanban_home)
        try:
            tid = kb.create_task(
                conn,
                title="review collision",
                assignee="alice",
                model_override="m1",
                provider_override="test",
            )
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (tid,)
            )
            conn.commit()
            spawned = []
            result = kb.dispatch_once(
                conn,
                spawn_fn=lambda task, ws, **kw: spawned.append(task.id) or 12345,
                dry_run=False,
            )
            assert spawned == [], "doomed collision review worker must NOT spawn"
            assert tid in result.auto_blocked
            row = conn.execute(
                "SELECT status, block_kind, last_failure_error, "
                "consecutive_failures FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            assert row["status"] == "blocked"
            assert row["block_kind"] == "capability"
            err = row["last_failure_error"] or ""
            assert "sdlc-review" in err
            assert "collision" in err
            assert str(prof / "skills" / "sdlc-review") in err, (
                "local competing path must be named"
            )
            assert str(ext / "sdlc-review") in err, (
                "external competing path must be named"
            )
            assert (
                row["consecutive_failures"] == 0
            ), "retry budget must not burn on a pre-spawn block"
            kinds = [k for k, _ in _events(conn, tid)]
            assert "blocked" in kinds
            payload = dict(_events(conn, tid, "blocked")[0][1])
            assert "collision" in payload.get("reason", "")
        finally:
            conn.close()

    def test_ready_lane_missing_skill_blocks_before_spawn(
        self, kanban_home, assignee_profile
    ):
        conn = _connect(kanban_home)
        try:
            tid = kb.create_task(
                conn,
                title="ready doom",
                assignee="alice",
                skills=["nope"],
                model_override="m1",
                provider_override="test",
            )
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,)
            )
            conn.commit()
            spawned = []
            result = kb.dispatch_once(
                conn,
                spawn_fn=lambda task, ws, **kw: spawned.append(task.id) or 12345,
                dry_run=False,
            )
            assert spawned == [], "doomed ready worker must NOT spawn"
            assert tid in result.auto_blocked
            row = conn.execute(
                "SELECT last_failure_error, consecutive_failures "
                "FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            assert "nope" in (row["last_failure_error"] or "")
            assert (
                row["consecutive_failures"] == 0
            ), "retry budget must not burn on a pre-spawn block"
        finally:
            conn.close()
