"""Auto-continue / auto-split: a burned retry counter must not park the work.

Owner's rule: "if purely by the retry counter — the card goes to work on its own,
and if it has to, it decomposes into separate cards so it gets to the end."

Pinned here (one test per acceptance criterion of the card):

1. a pure counter trip returns the card to ``ready`` — no ``blocked``, no
   ``gave_up``;
2. a second burn in the same generation splits the card: child rows exist,
   parent->child links exist, the root is ``todo``, and ``auto_split`` names the
   children;
3. ``auto_continue_max_splits`` is honoured: at generation N+1 the card is
   ``blocked`` with ``auto_continue_exhausted``;
4. no regression: ``rate_limited`` / ``host_interrupted`` / ``owner_lost`` /
   ``infrastructure`` still count no failure, and ``auto_continue: false``
   restores the old ``blocked`` + ``gave_up``;
5. an unavailable decomposer leaves the card in ``ready`` with the reason in the
   journal and its workspace untouched;
6. generation 0 -> split happens on the SECOND burn, not the first.

The decomposer's aux LLM is replaced by a stub in every split test: no network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_auto_continue as kac
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated board inside a throwaway HERMES_HOME."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(board: Path):
    connection = kbc.connect()
    try:
        yield connection
    finally:
        connection.close()


def _settings(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True, max_splits: int = 2) -> None:
    """Pin the policy for one test (the breaker reads it from config)."""
    monkeypatch.setattr(
        kac, "resolve_settings",
        lambda: kac.AutoContinueSettings(enabled=enabled, max_splits=max_splits),
    )


def _events(conn, task_id: str) -> list[str]:
    return [e.kind for e in kb.list_events(conn, task_id)]


def _payload(conn, task_id: str, kind: str) -> dict:
    """Newest event payload of ``kind`` (list_events already decodes JSON)."""
    for event in reversed(kb.list_events(conn, task_id)):
        if event.kind == kind:
            return event.payload or {}
    raise AssertionError(f"no {kind} event for {task_id}")


def _task(conn, task_id: str):
    return conn.execute(
        "SELECT status, consecutive_failures, block_kind, workspace_path "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()


def _burn_attempt(conn, task_id: str, *, attempt: int, error: str | None = None) -> bool:
    """One full worker attempt that dies on its budget: claim -> trip the breaker.

    Mirrors what the timeout sweep does: the task is ``running`` with a worker
    pid, then ``_record_task_failure`` books ``timed_out`` with the claim
    released.
    """
    row = _task(conn, task_id)
    if row["status"] in ("todo", "blocked"):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    claimed = kb.claim_task(conn, task_id, claimer=f"{kb._claimer_id().split(':')[0]}:worker")
    if claimed is None:
        return False
    kbd._set_worker_pid(conn, task_id, 980_000 + attempt)
    return kbd._record_task_failure(
        conn, task_id,
        error=error or f"Iteration budget exhausted ({attempt}/1)",
        outcome="timed_out",
        release_claim=True,
        end_run=True,
    )


def _new_card(conn, **kwargs) -> str:
    tid = kb.create_task(conn, title=kwargs.pop("title", "big job"), assignee="worker", **kwargs)
    conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (f"/tmp/ws/{tid}", tid))
    return tid


# ---------------------------------------------------------------------------
# 1. a pure counter trip continues the card
# ---------------------------------------------------------------------------


def test_counter_exhaustion_returns_card_to_ready_without_gave_up(conn, monkeypatch):
    """`failure_limit` reached on a card-internal cause -> ``ready``, event logged."""
    _settings(monkeypatch, max_splits=2)
    tid = _new_card(conn)

    assert _burn_attempt(conn, tid, attempt=1) is False, "first burn is below the limit"
    assert _task(conn, tid)["status"] == "ready"

    assert _burn_attempt(conn, tid, attempt=2) is False, "the trip must not report a give-up"
    row = _task(conn, tid)
    assert row["status"] == "ready", "a burned counter alone must not park the card"
    assert row["block_kind"] is None
    kinds = _events(conn, tid)
    assert "gave_up" not in kinds
    assert "auto_continued" in kinds, "the auto-continuation must be visible in the journal"

    payload = _payload(conn, tid, "auto_continued")
    assert payload["reason"] == "counter_exhausted"
    assert payload["attempt"] == 1
    assert payload["failures"] == kbd.DEFAULT_FAILURE_LIMIT

    # The attempt itself still closes with its own outcome, so history is honest.
    outcomes = [r.outcome for r in kb.list_runs(conn, tid)]
    assert "timed_out" in outcomes
    assert "gave_up" not in outcomes


def test_a_continued_card_is_claimable_again(conn, monkeypatch):
    """The point of `ready` is that the dispatcher respawns it with no human step."""
    _settings(monkeypatch)
    tid = _new_card(conn)
    _burn_attempt(conn, tid, attempt=1)
    _burn_attempt(conn, tid, attempt=2)
    assert _task(conn, tid)["status"] == "ready"
    claimed = kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':')[0]}:worker")
    assert claimed is not None and claimed.id == tid


# ---------------------------------------------------------------------------
# 2. a second burn splits the card
# ---------------------------------------------------------------------------


def _stub_split(conn, *, parent_id: str, generation: int, titles=("part A", "part B")):
    """Create real child rows the way the decomposer does, then report success."""
    def _decompose(task_id: str, *, generation: int = 1, **_kw):
        with kbc.connect_closing() as own:
            child_ids = []
            for idx, title in enumerate(titles):
                cid = kb.create_task(own, title=f"{title} g{generation}", assignee="worker")
                own.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (task_id, cid),
                )
                child_ids.append(cid)
            own.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'", (task_id,),
            )
        return SimpleNamespace(ok=True, reason="stub", child_ids=child_ids, fanout=True)
    return _decompose


def test_second_burn_splits_card_into_linked_children(conn, monkeypatch):
    """Generation 0: burn, continue, burn again -> children + links + auto_split."""
    _settings(monkeypatch, max_splits=2)
    tid = _new_card(conn)

    _burn_attempt(conn, tid, attempt=1)          # counter reaches the limit
    _burn_attempt(conn, tid, attempt=2)          # -> auto_continued
    assert _task(conn, tid)["status"] == "ready"
    assert not kac.read_state(conn, tid).split_pending

    _burn_attempt(conn, tid, attempt=3)          # second burn of the generation
    assert kac.read_state(conn, tid).split_pending is True
    assert _task(conn, tid)["status"] == "ready"

    split, failed = kac.run_split_phase(conn, decompose_fn=_stub_split(conn, parent_id=tid, generation=1))
    assert failed == [] and split == [tid]

    children = kb.child_ids(conn, tid)
    assert len(children) == 2
    assert _task(conn, tid)["status"] == "todo", "the root waits for its children"
    for cid in children:
        assert kb.parent_ids(conn, cid) == [tid]

    kinds = _events(conn, tid)
    assert "auto_split" in kinds and "gave_up" not in kinds
    payload = _payload(conn, tid, "auto_split")
    assert sorted(payload["child_ids"]) == sorted(children)
    assert payload["generation"] == 1
    assert kac.read_state(conn, tid).generation == 1


# ---------------------------------------------------------------------------
# 3. the split budget is bounded
# ---------------------------------------------------------------------------


def test_split_budget_is_bounded_and_blocks_with_exhausted_reason(conn, monkeypatch):
    """At generation max_splits a further burn blocks typed, not silently."""
    _settings(monkeypatch, max_splits=1)
    tid = _new_card(conn)

    _burn_attempt(conn, tid, attempt=1)   # counter reaches the limit
    _burn_attempt(conn, tid, attempt=2)   # -> auto_continued (generation 0)
    assert _task(conn, tid)["status"] == "ready"
    assert kac.read_state(conn, tid).split_pending is False

    _burn_attempt(conn, tid, attempt=3)   # second burn of the generation -> split
    assert kac.read_state(conn, tid).split_pending is True
    split, _ = kac.run_split_phase(conn, decompose_fn=_stub_split(conn, parent_id=tid, generation=1))
    assert split == [tid]
    assert kac.read_state(conn, tid).generation == 1

    _burn_attempt(conn, tid, attempt=4)   # first burn of generation 1 -> continue
    assert _task(conn, tid)["status"] == "ready"
    tripped = _burn_attempt(conn, tid, attempt=5)   # budget spent, continuation burned
    assert tripped is True

    row = _task(conn, tid)
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input", "a plain counter block would be auto-promoted back"

    blocked = [e.payload or {} for e in kb.list_events(conn, tid) if e.kind == "blocked"]
    assert blocked and blocked[-1]["reason"] == kac.EXHAUSTED_REASON

    # Sticky: recompute_ready must not promote it back into a respawn loop.
    assert kb.recompute_ready(conn, failure_limit=1) == 0
    assert _task(conn, tid)["status"] == "blocked"


def test_exhausted_block_survives_dispatcher_recompute(conn, monkeypatch):
    """A second tick after the block must not resurrect the card."""
    _settings(monkeypatch, max_splits=1)
    tid = _new_card(conn)
    _burn_attempt(conn, tid, attempt=1)
    _burn_attempt(conn, tid, attempt=2)
    _burn_attempt(conn, tid, attempt=3)
    kac.run_split_phase(conn, decompose_fn=_stub_split(conn, parent_id=tid, generation=1))
    _burn_attempt(conn, tid, attempt=4)
    _burn_attempt(conn, tid, attempt=5)
    assert _task(conn, tid)["status"] == "blocked"
    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, failure_limit=1)
    assert result.spawned == [] and _task(conn, tid)["status"] == "blocked"


# ---------------------------------------------------------------------------
# 4. no regression on the causes that were already exempt, and the off switch
# ---------------------------------------------------------------------------


def test_auto_continue_false_restores_blocked_and_gave_up(conn, monkeypatch):
    """The explicit off switch must behave exactly as before this change."""
    _settings(monkeypatch, enabled=False)
    tid = _new_card(conn)
    _burn_attempt(conn, tid, attempt=1)
    tripped = _burn_attempt(conn, tid, attempt=2)

    assert tripped is True
    row = _task(conn, tid)
    assert row["status"] == "blocked"
    kinds = _events(conn, tid)
    assert "gave_up" in kinds
    assert "auto_continued" not in kinds


def test_rate_limited_requeue_counts_no_failure(conn, monkeypatch):
    """A quota wall is not the card's fault and must not start the automation."""
    _settings(monkeypatch)
    tid = _new_card(conn)
    kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':')[0]}:worker")
    kbd._set_worker_pid(conn, tid, 981_000)

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL, consecutive_failures = 0 "
            "WHERE id = ?", (tid,),
        )
        kb._append_event(conn, tid, "rate_limited", {"pid": 981_000})

    row = _task(conn, tid)
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == 0
    assert "auto_continued" not in _events(conn, tid)


@pytest.mark.parametrize("extra", [{"infrastructure": True}, {"terminal_provider": True}, {"sticky": True}])
def test_non_card_causes_keep_the_old_block(conn, monkeypatch, extra):
    """Infrastructure, a terminal provider error and a systemic wave still block."""
    _settings(monkeypatch)
    tid = _new_card(conn)
    _burn_attempt(conn, tid, attempt=1)
    tripped = kbd._record_task_failure(
        conn, tid, error="host refused the spawn", outcome="spawn_failed",
        release_claim=False, end_run=False, event_payload_extra=dict(extra),
        force_trip=bool(extra.get("terminal_provider") or extra.get("sticky")),
    )
    assert tripped is True
    assert _task(conn, tid)["status"] == "blocked"
    kinds = _events(conn, tid)
    assert "gave_up" in kinds and "auto_continued" not in kinds


def test_infrastructure_spawn_failure_is_not_counted_twice(conn, monkeypatch):
    """`infrastructure=True` already skips the counter; automation must not fire."""
    _settings(monkeypatch)
    tid = _new_card(conn)
    tripped = kbd._record_task_failure(
        conn, tid, error="no restart-safe scope", outcome="spawn_failed",
        failure_limit=1, release_claim=True, end_run=True, infrastructure=True,
    )
    assert tripped is False
    assert _task(conn, tid)["status"] == "ready"
    assert "auto_continued" not in _events(conn, tid)


# ---------------------------------------------------------------------------
# 5. a decomposer outage continues the card and keeps its work
# ---------------------------------------------------------------------------


def test_unavailable_decomposer_continues_card_and_records_reason(conn, monkeypatch):
    """No aux client: the card must keep working, not park, and not lose its tree."""
    _settings(monkeypatch, max_splits=2)
    tid = _new_card(conn)
    workspace = _task(conn, tid)["workspace_path"]
    _burn_attempt(conn, tid, attempt=1)
    _burn_attempt(conn, tid, attempt=2)
    _burn_attempt(conn, tid, attempt=3)
    assert kac.read_state(conn, tid).split_pending is True

    def _no_client(task_id: str, *, generation: int = 1, **_kw):
        return SimpleNamespace(ok=False, reason="auxiliary client unavailable", child_ids=None)

    split, failed = kac.run_split_phase(conn, decompose_fn=_no_client)
    assert split == [] and failed == [tid]

    row = _task(conn, tid)
    assert row["status"] == "ready", "a missing decomposer must not park the work"
    assert row["workspace_path"] == workspace, "the attempt's workspace is kept"
    kinds = _events(conn, tid)
    assert "blocked" not in kinds and "gave_up" not in kinds
    detail = _payload(conn, tid, "auto_continued")
    assert detail["reason"] == "split_unavailable"
    assert "auxiliary client" in detail["detail"]
    assert kac.read_state(conn, tid).split_failures == 1


def test_repeated_decomposer_outage_is_bounded(conn, monkeypatch):
    """A permanent decomposer outage must not become an eternal retry loop."""
    _settings(monkeypatch, max_splits=1)
    tid = _new_card(conn)

    def _no_client(task_id: str, *, generation: int = 1, **_kw):
        return SimpleNamespace(ok=False, reason="LLM returned malformed JSON", child_ids=None)

    _burn_attempt(conn, tid, attempt=1)
    _burn_attempt(conn, tid, attempt=2)          # split queued
    kac.run_split_phase(conn, decompose_fn=_no_client)   # outage -> continue
    _burn_attempt(conn, tid, attempt=3)          # split queued again
    kac.run_split_phase(conn, decompose_fn=_no_client)   # outage again: budget spent
    tripped = _burn_attempt(conn, tid, attempt=4)

    assert tripped is True
    row = _task(conn, tid)
    assert row["status"] == "blocked" and row["block_kind"] == "needs_input"
    blocked = [e.payload or {} for e in kb.list_events(conn, tid) if e.kind == "blocked"]
    assert blocked[-1]["reason"] == kac.EXHAUSTED_REASON
    assert blocked[-1].get("decomposer_unavailable") is True


# ---------------------------------------------------------------------------
# 6. generation 0 continues first, splits only on the second burn
# ---------------------------------------------------------------------------


def test_generation_zero_continues_before_any_split(conn, monkeypatch):
    """The first trip must never subdivide: the card gets its own extra run."""
    _settings(monkeypatch, max_splits=2)
    tid = _new_card(conn)
    _burn_attempt(conn, tid, attempt=1)
    _burn_attempt(conn, tid, attempt=2)

    state = kac.read_state(conn, tid)
    assert state.generation == 0
    assert state.continuations == 1
    assert state.split_pending is False
    assert kb.child_ids(conn, tid) == []


def test_plan_modes_are_pure_and_ordered(conn, monkeypatch):
    """The decision table itself: block -> continue -> split -> exhausted."""
    settings = kac.AutoContinueSettings(enabled=True, max_splits=2)
    base = dict(outcome="timed_out", extra=None, failures=2, effective_limit=2, limit_source="dispatcher")

    assert kac.plan_after_failure(**base, generation=0, continuations=0, settings=settings).mode == "continue"
    assert kac.plan_after_failure(**base, generation=0, continuations=1, settings=settings).mode == "split"
    plan = kac.plan_after_failure(**base, generation=2, continuations=1, settings=settings)
    assert plan.mode == "block" and plan.reason == kac.EXHAUSTED_REASON
    # An empty split budget means "continue once, then block".
    zero = kac.AutoContinueSettings(enabled=True, max_splits=0)
    assert kac.plan_after_failure(**base, generation=0, continuations=0, settings=zero).mode == "continue"
    assert kac.plan_after_failure(
        **base, generation=0, continuations=1, settings=zero,
    ).reason == kac.EXHAUSTED_REASON
    # A spawn failure is not a card budget problem at all.
    assert kac.plan_after_failure(
        **{**base, "outcome": "spawn_failed"}, generation=0, continuations=0, settings=settings,
    ).mode == "block"
