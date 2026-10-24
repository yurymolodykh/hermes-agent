"""F-4.4 (K1 red team H-4): silent artifact drops become observable.

Covers the three silent-drop paths flagged by the red team (t_d0bcecda):

  * ``hermes_cli/kanban_db.py:6416-6417`` — a non-string/blank entry in the
    ``artifacts`` catalog passed to ``complete_task`` drops out of the
    delivered list with no diagnostic.
  * ``gateway/delivery_engine.py:342`` — ``owui_root_artifact_paths`` (R1)
    silently skips a non-string entry, and (existing) a broken/nonexistent
    or relative path returns without a warn.
  * ``gateway/kanban_watchers.py:4193`` — ``_deliver_kanban_artifacts``
    silently skips a broken/nonexistent artifact path.

Spec contract: the bad entry is STILL excluded (delivery behavior does not
change — no crash, no bogus upload), but the skip is now logged at WARN level
so a missing deliverable is diagnosable instead of a silent drop.

Hermetic harness mirrors tests/gateway/test_dyn_redteam_part2_t9a7844b0.py —
real functions, tmp_path kanban DB, recording adapter, no deployment (P1).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _synthetic_assignees(isolated_kanban_assignee_registry):
    """Provide synthetic assignee registry for hermetic task creation."""
    return isolated_kanban_assignee_registry


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "f44.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, *, assignee="worker", initial_status="running"):
    return kb.create_task(
        conn, title=title, assignee=assignee, initial_status=initial_status,
    )


# ---------------------------------------------------------------------------
# 1. kanban_db.complete_task — non-string artifact-catalog → warn
# ---------------------------------------------------------------------------

def test_complete_task_nonstring_catalog_warns(env_db, caplog):
    """F-4.4: a non-string entry in the artifacts catalog must produce a
    warn-level log instead of silently dropping out of the delivered list."""
    tid = _mk(env_db, "nonstring-catalog")

    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        ok = kb.complete_task(
            env_db, tid, summary="done",
            metadata={
                "artifacts": [
                    "C:/definitely/real/file.txt",  # valid string path
                    12345,                            # non-string → warn
                    {"path": "x"},                    # non-string → warn
                    "   ",                            # blank string → warn
                ],
            },
        )
    assert ok is True
    warns = [r for r in caplog.records if r.levelno >= 30]
    assert warns, "expected at least one warn for non-string artifacts"
    combined = "\n".join(r.getMessage() for r in warns)
    assert "non-string/blank" in combined, combined
    assert tid in combined, combined

    # The completed event payload must still only carry valid str entries —
    # bad entries excluded (delivery behavior unchanged).
    row = env_db.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='completed' "
        "ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    payload = json.loads(row[0])
    artifacts = payload.get("artifacts") or []
    assert artifacts == ["C:/definitely/real/file.txt"], artifacts


def test_complete_task_clean_catalog_no_warn(env_db, caplog):
    """F-4.4 regression: a fully-valid artifact catalog must NOT warn."""
    tid = _mk(env_db, "clean-catalog")
    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        keb = kb.complete_task(
            env_db, tid, summary="done",
            metadata={"artifacts": ["C:/real/path/a.txt", "C:/real/path/b.txt"]},
        )
    assert keb is True
    warns = [r for r in caplog.records if r.levelno >= 30]
    assert not warns, f"no warn expected for clean catalog: {[r.getMessage() for r in warns]}"


# ---------------------------------------------------------------------------
# 2. _owui_root_artifact_paths — non-string + broken path → warn
# ---------------------------------------------------------------------------

def test_owui_root_broken_path_warns(tmp_path, monkeypatch, caplog):
    """F-4.4: a nonexistent artifact path in the root/branch lane must be
    logged as a warn (was silently dropped) and the bad path must NOT be
    delivered."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "root.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _mk(conn, "root-broken")
        real = tmp_path / "present.txt"
        real.write_text("hi", encoding="utf-8")
        broken = tmp_path / "missing.bin"
        kb.complete_task(
            conn, tid, summary="done",
            metadata={"artifacts": [str(real), str(broken)]},
        )
    finally:
        conn.close()

    from gateway.delivery_engine import owui_root_artifact_paths

    caplog.clear()
    with caplog.at_level("WARNING", logger="gateway.delivery_engine"):
        paths = owui_root_artifact_paths({"root_id": tid, "board": "default"})
    warns = [r for r in caplog.records if r.levelno >= 30]
    assert warns, "expected warn for missing root artifact"
    combined = "\n".join(r.getMessage() for r in warns)
    assert "does not exist" in combined or "missing on disk" in combined, combined
    assert str(real) in paths, f"valid artifact must be delivered: {paths}"
    assert str(broken) not in paths, f"broken path must not be delivered: {paths}"


def test_owui_root_nonstring_warns(tmp_path, monkeypatch, caplog):
    """F-4.4: a non-string entry in the root-lane artifacts must warn, and the
    valid string entries must still be delivered."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "root2.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _mk(conn, "root-nonstring")
        real = tmp_path / "present.txt"
        real.write_text("hi", encoding="utf-8")
        kb.complete_task(
            conn, tid, summary="done",
            metadata={"artifacts": [str(real), 999]},
        )
    finally:
        conn.close()

    from gateway.delivery_engine import owui_root_artifact_paths

    caplog.clear()
    with caplog.at_level("WARNING", logger="gateway.delivery_engine"):
        paths = owui_root_artifact_paths({"root_id": tid, "board": "default"})
    warns = [r for r in caplog.records if r.levelno >= 30]
    assert warns, "expected warn for non-string root artifact"
    combined = "\n".join(r.getMessage() for r in warns)
    assert "non-string" in combined, combined
    assert str(real) in paths, f"valid artifact must be delivered: {paths}"


# ---------------------------------------------------------------------------
# 3. _deliver_kanban_artifacts — broken path → warn (per-event lane)
# ---------------------------------------------------------------------------

class _RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, file_path, caption=None,
                            file_name=None, reply_to=None, metadata=None,
                            **kwargs):
        self.docs.append({
            "chat_id": chat_id, "file_path": str(file_path),
            "file_name": file_name or os.path.basename(str(file_path)),
            "metadata": metadata or {},
        })
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def send_multiple_images(self, chat_id, images, metadata=None):
        return None

    async def send_video(self, chat_id, video_path, metadata=None):
        return None


def test_deliver_kanban_artifacts_broken_path_warns(tmp_path, monkeypatch, caplog):
    """F-4.4: a broken path in the per-event payload must warn (was silently
    returned) and must not reach the adapter; a valid file must still upload."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "pe.db"))
    kb.init_db()

    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = _RecordingAdapter()
    real = tmp_path / "present.txt"
    real.write_text("hi", encoding="utf-8")
    broken = tmp_path / "missing.bin"

    caplog.clear()
    with caplog.at_level("WARNING", logger="gateway.run"):
        asyncio.run(runner._deliver_kanban_artifacts(
            adapter=adapter,
            chat_id="pe-chat",
            metadata={},
            event_payload={"artifacts": [str(real), str(broken)]},
            task=None,
        ))
    warns = [r for r in caplog.records if r.levelno >= 30]
    assert warns, "expected warn for broken per-event artifact"
    combined = "\n".join(r.getMessage() for r in warns)
    assert "does not exist" in combined, combined
    uploaded = [d["file_path"] for d in adapter.docs]
    assert str(real) in uploaded, f"valid artifact must upload: {uploaded}"
    assert str(broken) not in uploaded, (
        f"broken path must not upload: {uploaded}"
    )
