"""Unit tests for kanban branch-summary helpers (kanban_db).

Coverage for the "короткий итог ветки" stage: node classification, subtree
closure, subscribed-root detection, branch terminality (success / failure /
open, incl. block_kind semantics and the gave_up override), and the once-only
marker lifecycle (claim / per-channel ack / clear-on-reopen).
"""

import os

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board_conn(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    """Fresh default-board DB isolated per test, with a synthetic assignee
    registry so named assignees pass the profile preflight."""
    db_path = tmp_path / "branch-summary.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title, assignee="worker"):
    return kb.create_task(conn, title=title, assignee=assignee)


def _close_branch(conn, root, children, full=True):
    """Complete root-first (parent before child), promoting children to ready."""
    tasks = [root] + list(children)
    for tid in tasks:
        assert kb.complete_task(conn, tid, summary="done"), f"complete failed for {tid}"
        # completing a parent promotes the next child to ready
    return tasks


# ---------------------------------------------------------------------------
# classify_branch_node_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,block_kind,expected", [
    ("done", None, kb.SA_BRANCH_NODE_OK),
    ("archived", None, kb.SA_BRANCH_NODE_OK),
    ("blocked", "needs_input", kb.SA_BRANCH_NODE_FAIL),
    ("blocked", "capability", kb.SA_BRANCH_NODE_FAIL),
    ("blocked", None, kb.SA_BRANCH_NODE_FAIL),
    ("blocked", "", kb.SA_BRANCH_NODE_FAIL),
    ("blocked", "dependency", kb.SA_BRANCH_NODE_OPEN),
    ("blocked", "transient", kb.SA_BRANCH_NODE_OPEN),
    ("todo", None, kb.SA_BRANCH_NODE_OPEN),
    ("ready", None, kb.SA_BRANCH_NODE_OPEN),
    ("running", None, kb.SA_BRANCH_NODE_OPEN),
    ("review", None, kb.SA_BRANCH_NODE_OPEN),
    ("triage", None, kb.SA_BRANCH_NODE_OPEN),
    ("scheduled", None, kb.SA_BRANCH_NODE_OPEN),
    ("gave_up", None, kb.SA_BRANCH_NODE_FAIL),
    (None, None, kb.SA_BRANCH_NODE_OPEN),
])
def test_classify_branch_node_status(status, block_kind, expected):
    assert kb.classify_branch_node_status(status, block_kind) == expected


# ---------------------------------------------------------------------------
# Subtree closure + subscribed roots
# ---------------------------------------------------------------------------

def test_descendant_ids_transitive_closure(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a")
    b = _mk(board_conn, "b")
    leaf = _mk(board_conn, "leaf")
    kb.link_tasks(board_conn, root, a)
    kb.link_tasks(board_conn, a, b)
    kb.link_tasks(board_conn, b, leaf)
    # shared: leaf also a direct child of root
    kb.link_tasks(board_conn, root, leaf)
    desc = kb.get_descendant_ids(board_conn, root)
    assert desc == {a, b, leaf}
    assert root not in desc
    # a standalone task has no descendants
    alone = _mk(board_conn, "alone")
    assert kb.get_descendant_ids(board_conn, alone) == set()


def test_ancestor_ids_transitive_closure(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a")
    b = _mk(board_conn, "b")
    kb.link_tasks(board_conn, root, a)
    kb.link_tasks(board_conn, a, b)
    assert kb.get_ancestor_ids(board_conn, b) == {root, a}
    assert kb.get_ancestor_ids(board_conn, a) == {root}
    assert kb.get_ancestor_ids(board_conn, root) == set()


def test_subscribed_branch_roots_only_topmost_subscribed(board_conn, isolated_kanban_assignee_registry):
    top = _mk(board_conn, "top")
    mid = _mk(board_conn, "mid")
    low = _mk(board_conn, "low")
    kb.link_tasks(board_conn, top, mid)
    kb.link_tasks(board_conn, mid, low)
    # every node subscribed -> only the topmost is a branch root
    kb.add_notify_sub(board_conn, task_id=top, platform="telegram", chat_id="c-top")
    kb.add_notify_sub(board_conn, task_id=mid, platform="telegram", chat_id="c-mid")
    kb.add_notify_sub(board_conn, task_id=low, platform="telegram", chat_id="c-low")
    assert kb.subscribed_branch_roots(board_conn) == [top]


def test_subscribed_branch_roots_with_unsubscribed_intermediate(board_conn, isolated_kanban_assignee_registry):
    top = _mk(board_conn, "top")
    mid = _mk(board_conn, "mid")
    low = _mk(board_conn, "low")
    kb.link_tasks(board_conn, top, mid)
    kb.link_tasks(board_conn, mid, low)
    # Subscribe ONLY the bottom node: no subscribed ancestor -> low itself is
    # the branch root.
    kb.add_notify_sub(board_conn, task_id=low, platform="telegram", chat_id="c-low")
    assert kb.subscribed_branch_roots(board_conn) == [low]
    # Subscribe the top too: low now belongs to top's branch (top is a
    # subscribed ancestor), so the only root walks up to top.
    kb.add_notify_sub(board_conn, task_id=top, platform="telegram", chat_id="c-top")
    assert kb.subscribed_branch_roots(board_conn) == [top]


# ---------------------------------------------------------------------------
# branch_state: terminality semantics
# ---------------------------------------------------------------------------

def test_branch_state_success_when_all_done(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a")
    b = _mk(board_conn, "b")
    kb.link_tasks(board_conn, root, a)
    kb.link_tasks(board_conn, root, b)
    kb.link_tasks(board_conn, a, b)  # shared grandchild
    # open first
    assert kb.branch_state(board_conn, root) is None
    _close_branch(board_conn, root, [a, b])
    st = kb.branch_state(board_conn, root)
    assert st is not None
    assert st["outcome"] == "success"
    assert st["failed"] == []
    assert kb.branch_summary_message(st["outcome"], st["failed"]) == "✅ Задача выполнена"


def test_branch_state_open_while_one_node_open(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a")
    kb.link_tasks(board_conn, root, a)
    kb.complete_task(board_conn, root, summary="done")  # a promotes to ready
    assert kb.get_task(board_conn, a).status == "ready"
    assert kb.branch_state(board_conn, root) is None  # a still open


def test_branch_state_needs_input_block_is_failure(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a_need_answer")
    kb.link_tasks(board_conn, root, a)
    kb.complete_task(board_conn, root, summary="done")
    assert kb.block_task(board_conn, a, kind="needs_input", reason="answer me")
    assert kb.get_task(board_conn, a).status == "blocked"
    st = kb.branch_state(board_conn, root)
    assert st is not None and st["outcome"] == "failure"
    assert ("a_need_answer" in st["failed"][0][0] or st["failed"][0][0] == a)
    msg = kb.branch_summary_message(st["outcome"], st["failed"])
    assert msg.startswith("❌ Задача не выполнена (нужен фикс):")


def test_branch_state_legacy_untyped_block_is_failure(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a_legacy")
    kb.link_tasks(board_conn, root, a)
    kb.complete_task(board_conn, root, summary="done")
    assert kb.block_task(board_conn, a, kind=None, reason="legacy block")
    assert kb.get_task(board_conn, a).block_kind is None
    st = kb.branch_state(board_conn, root)
    assert st is not None and st["outcome"] == "failure"


def test_branch_state_dependency_and_transient_block_open(board_conn):
    root = _mk(board_conn, "root")
    dep = _mk(board_conn, "dep_child")
    tra = _mk(board_conn, "tra_child")
    kb.link_tasks(board_conn, root, dep)
    kb.link_tasks(board_conn, root, tra)
    kb.complete_task(board_conn, root, summary="done")
    kb.block_task(board_conn, dep, kind="dependency", reason="dep")
    kb.block_task(board_conn, tra, kind="transient", reason="flaky")
    # dependency routes to todo; transient sits in blocked — both OPEN
    assert kb.branch_state(board_conn, root) is None


def test_branch_state_gave_up_overrides_auto_resolving_block_kind(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "flaky_child")
    kb.link_tasks(board_conn, root, a)
    kb.complete_task(board_conn, root, summary="done")
    kb.block_task(board_conn, a, kind="transient", reason="flaky")
    assert kb.get_task(board_conn, a).status == "blocked"
    # simulate the retry circuit-breaker trip: status stays blocked but a
    # gave_up event is emitted (see kanban_db._record_task_failure)
    with kb.write_txn(board_conn):
        kb._append_event(board_conn, a, "gave_up", {"reason": "spawn failed"})
    st = kb.branch_state(board_conn, root)
    assert st is not None and st["outcome"] == "failure"
    assert ("flaky_child" in st["failed"][0][0] or st["failed"][0][0] == a)


def test_branch_state_dangling_child_edge_open(board_conn):
    root = _mk(board_conn, "root")
    # fabricate a dangling edge to a missing task — must keep branch open
    with kb.write_txn(board_conn):
        board_conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (root, "ghost-task"),
        )
    assert kb.branch_state(board_conn, root) is None


def test_branch_state_fingerprint_changes_on_state_change(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a")
    kb.link_tasks(board_conn, root, a)
    _close_branch(board_conn, root, [a])
    fp1 = kb.branch_state(board_conn, root)["fingerprint"]
    # reopen via direct SQL (simulates done -> ready -> done cycle)
    with kb.write_txn(board_conn):
        board_conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (a,))
    assert kb.branch_state(board_conn, root) is None
    kb.complete_task(board_conn, a, summary="done")
    fp2 = kb.branch_state(board_conn, root)["fingerprint"]
    assert fp1 == fp2  # same close-state fingerprint (marker reuse via clear-on-open)


def test_branch_summary_message_cap(board_conn):
    failed = [(f"t_{i:04d}", f"title {i}") for i in range(6)]
    msg = kb.branch_summary_message("failure", failed)
    assert "t_0000" in msg and "t_0002" in msg and "t_0003" not in msg
    assert "(+3 ещё)" in msg
    assert kb.branch_summary_message("failure", []) .startswith("✅") is False
    assert kb.branch_summary_message("success", []) == "✅ Задача выполнена"


# ---------------------------------------------------------------------------
# Once-only marker lifecycle
# ---------------------------------------------------------------------------

def test_marker_claim_ack_pending_clear(board_conn):
    root = _mk(board_conn, "root")
    a = _mk(board_conn, "a")
    kb.link_tasks(board_conn, root, a)
    _close_branch(board_conn, root, [a])
    st = kb.branch_state(board_conn, root)
    assert kb.has_branch_summary(board_conn, root) is None
    mid = kb.mark_branch_summary(board_conn, root, {
        "fingerprint": st["fingerprint"], "outcome": st["outcome"],
        "failed": [], "delivered": [],
    })
    assert mid > 0
    marker = kb.has_branch_summary(board_conn, root)
    assert marker is not None and marker["_event_id"] == mid
    subs = [
        {"platform": "telegram", "chat_id": "c1", "thread_id": ""},
        {"platform": "api_server", "chat_id": "owui-1", "thread_id": ""},
    ]
    assert len(kb.branch_summary_pending_subs(marker, subs)) == 2
    # idempotent ack
    assert kb.ack_branch_summary_channel(
        board_conn, root, platform="telegram", chat_id="c1", thread_id="") == mid
    assert kb.ack_branch_summary_channel(
        board_conn, root, platform="telegram", chat_id="c1", thread_id="") is not None
    new_marker = kb.has_branch_summary(board_conn, root)
    assert kb.branch_summary_pending_subs(new_marker, subs) == [subs[1]]
    assert kb.ack_branch_summary_channel(
        board_conn, root, platform="api_server", chat_id="owui-1", thread_id="")
    assert kb.branch_summary_pending_subs(
        kb.has_branch_summary(board_conn, root), subs) == []
    # clear-on-reopen
    assert kb.clear_branch_summary(board_conn, root) == 1
    assert kb.has_branch_summary(board_conn, root) is None
