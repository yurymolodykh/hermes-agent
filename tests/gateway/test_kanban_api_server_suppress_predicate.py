"""api_server (OWUI) lane: which events are intermediate noise, which stay loud.

HISTORY — why this module no longer imports a predicate
------------------------------------------------------
This module used to import three symbols from ``gateway.kanban_watchers``:

  * ``should_suppress_api_server_event(event, card, parent, replace)``
  * ``_owui_suppress_intermediate_event(kind, ev, replace, platform)``  (shim)
  * ``should_wake_agent_for_event(event, card, parent, replace)``       (module 2)

Those were the pre-R1 per-lane policy helpers.  The R1 delivery-engine
consolidation (``d3cfc258d5``, 2026-10-21) deliberately deleted all three and
replaced them with ONE classification authority:
``gateway.delivery_engine.classify_event()``.  ``gateway/REFACTOR-CHANGELOG.md``
records the removal explicitly (line 14 lists the three names as removed
legacy; line 74 states the accepted contract: "wake только [final] +
[needs-decision]", "triage по kind И card.status").

The symbols are gone from every revision from ``d3cfc258d5`` onward and have
**zero live call sites** — they were test-facing only.  Restoring them would
resurrect a second, parallel policy that contradicts the shipped engine:

  * the old predicate kept ``crashed`` / ``timed_out`` loud, but those kinds
    auto-retry (``delivery_engine._ABORTED_KINDS`` docstring) and are therefore
    INTERMEDIATE — the P2 rule says an intermediate close is never a message;
  * the old predicate suppressed a completing ROOT outright, while the engine
    stages a root's own terminal completion when the branch really is terminal.

So the module was RE-POINTED at the canonical classifier and its assertions
restated in engine terms.  Suppression is now expressed as
``classify_event(...).event_type is None`` (the event is silenced; the caller
still advances the cursor).  Everything the old tests actually cared about —
routine intermediate traffic stays quiet, machine-actionable and human-action
traffic stays loud, malformed input never silently drops a real event — is
preserved, now asserted against the single source of truth.

ONE ASSERTION WAS DROPPED, NOT SILENTLY RELAXED — the retired predicate
-----------------------------------------------------------------------
The old module asserted that ``gave_up`` / ``crashed`` / ``timed_out`` stay
loud on the api_server lane.  The engine still delivers them, but as
``[aborted]`` rather than as a needs-decision ping; the assertions here now
encode that.  ``gave_up`` (the breaker-tripped terminal failure,
``BRANCH_TERMINAL_FAILURE_KIND`` in ``hermes_cli/kanban_db``) additionally
still reaches the user through the branch итог, so nothing is lost — only the
per-event loudness became the aborted-delivery contract.  ``crashed`` /
``timed_out`` auto-retry, so treating them as intermediate is the correct
reading of the P2 rule, not a regression.
"""

from types import SimpleNamespace

import pytest

from gateway.delivery_engine import (
    ABORTED,
    FINAL,
    LIVE_STATUS,
    NEEDS_DECISION,
    classify_event,
)


def _ev(kind, payload=None):
    return SimpleNamespace(kind=kind, payload=payload or {}, id=11)


def _task(status="running", block_kind=None):
    return SimpleNamespace(
        status=status, title="card", result="", block_kind=block_kind,
    )


def _ctx(*, branch_terminal=False, card_status="running",
         block_kind=None, is_root=False):
    return {
        "branch_terminal": branch_terminal,
        "is_root": is_root,
        "card_status": card_status,
        "block_kind": block_kind,
    }


#: A descendant card of a mission tree — the "intermediate" case.
CHILD = _ctx()
#: The branch root whose OWN close really is the terminal итог.
ROOT_TERMINAL = _ctx(branch_terminal=True, is_root=True, card_status="done")


def _silenced(kind, *, payload=None, ctx=None, task=None, **kwargs):
    """True when the engine silences this event (nothing is delivered)."""
    dec = classify_event(
        task or _task(), _ev(kind, payload), dict(ctx if ctx is not None else CHILD),
        **kwargs,
    )
    return dec.event_type is None


# ---------------------------------------------------------------------------
# Terminal branch итог: the single channel message — never silenced.
# ---------------------------------------------------------------------------


def test_terminal_root_completion_is_final():
    dec = classify_event(_task("done"), _ev("completed"), dict(ROOT_TERMINAL))
    assert dec.event_type == FINAL


def test_root_completion_with_open_subscribed_descendant_is_intermediate():
    # P2 (t_ecab4990): an api_server root that still owns subscribed children
    # is NOT the branch итог — the branch-summary watcher owns that message.
    ctx = _ctx(branch_terminal=False, is_root=True, card_status="done")
    assert classify_event(_task("done"), _ev("completed"), dict(ctx)).event_type is None


# ---------------------------------------------------------------------------
# Routine intermediate traffic: silenced (the branch итог carries it).
# ---------------------------------------------------------------------------


def test_child_routine_kinds_silenced():
    for kind in ("completed", "status", "archived", "unblocked"):
        assert _silenced(kind) is True


def test_child_split_close_silenced():
    # [split N/M] one-off child close while the ancestor mission is running.
    assert _silenced("completed") is True


def test_self_resolving_block_silenced():
    # dependency / transient resolve on their own — never a message.
    assert _silenced("blocked", payload={"kind": "dependency"}) is True
    assert _silenced("blocked", payload={"kind": "transient"}) is True


def test_root_self_resolving_block_silenced():
    ctx = _ctx(is_root=True)
    assert _silenced("blocked", payload={"kind": "dependency"}, ctx=ctx) is True


def test_unclassified_kind_silenced():
    assert _silenced("weird_kind") is True


# ---------------------------------------------------------------------------
# Human-action events: never silenced.
# ---------------------------------------------------------------------------


def test_human_attention_kinds_loud():
    for kind in ("block_loop_detected", "review_requested", "triage"):
        dec = classify_event(_task(), _ev(kind), dict(CHILD))
        assert dec.event_type == NEEDS_DECISION


def test_blocked_needs_human_fix_loud():
    for block_kind in ("needs_input", "capability"):
        dec = classify_event(_task(), _ev("blocked", {"kind": block_kind}), dict(CHILD))
        assert dec.event_type == NEEDS_DECISION
    # Legacy un-typed block == human decision (branch_state semantics).
    for payload in ({}, None):
        dec = classify_event(_task(), _ev("blocked", payload), dict(CHILD))
        assert dec.event_type == NEEDS_DECISION


def test_blocked_provider_route_loud():
    dec = classify_event(
        _task(), _ev("blocked", {"reason": "API down", "provider_route": "x/y"}),
        dict(CHILD),
    )
    assert dec.event_type == NEEDS_DECISION


def test_card_status_triage_beats_routine_kind():
    # A card routed to triage needs a human decision no matter which event
    # carries the notification.
    for kind in ("completed", "status", "archived"):
        ctx = _ctx(card_status="triage")
        assert classify_event(_task(), _ev(kind), dict(ctx)).event_type == NEEDS_DECISION


def test_card_status_triage_beats_self_resolving_block():
    ctx = _ctx(card_status="triage")
    dec = classify_event(_task(), _ev("blocked", {"kind": "dependency"}), dict(ctx))
    assert dec.event_type == NEEDS_DECISION


# ---------------------------------------------------------------------------
# Aborted: terminal failure / interruption is a delivery, not a silence.
# ---------------------------------------------------------------------------


def test_aborted_kinds_delivered():
    for kind in ("gave_up", "crashed", "timed_out"):
        dec = classify_event(_task(), _ev(kind), dict(CHILD))
        assert dec.event_type == ABORTED


# ---------------------------------------------------------------------------
# Live status: an in-flight card edits its message, never a fresh ping.
# ---------------------------------------------------------------------------


def test_live_status_kinds():
    for kind in ("spawned", "claimed"):
        assert classify_event(_task(), _ev(kind), dict(CHILD)).event_type == LIVE_STATUS
    for payload in ({"status": "running"}, {"to": "running"}):
        assert classify_event(
            _task(), _ev("status", payload), dict(CHILD),
        ).event_type == LIVE_STATUS


def test_non_running_status_is_silenced():
    assert _silenced("status", payload={"status": "ready"}) is True


# ---------------------------------------------------------------------------
# Synthetic readback cards are never a delivery subject.
# ---------------------------------------------------------------------------


def test_do_not_deliver_silences_everything():
    dec = classify_event(
        _task("done"), _ev("completed"), dict(ROOT_TERMINAL), do_not_deliver=True,
    )
    assert dec.event_type is None
    dec = classify_event(
        _task(), _ev("block_loop_detected"), dict(CHILD), do_not_deliver=True,
    )
    assert dec.event_type is None


# ---------------------------------------------------------------------------
# Defensive: malformed input is silenced, never crashes the notifier.
# ---------------------------------------------------------------------------


def test_none_event_silenced():
    assert classify_event(_task(), None, dict(CHILD)).event_type is None


def test_event_without_kind_silenced():
    assert classify_event(_task(), object(), dict(CHILD)).event_type is None


def test_non_dict_payload_tolerated():
    # A non-dict payload must not crash classification.
    for payload in (None, "not-a-dict", 42):
        dec = classify_event(_task(), _ev("blocked", payload), dict(CHILD))
        assert dec.event_type == NEEDS_DECISION


def test_missing_tree_ctx_tolerated():
    dec = classify_event(_task(), _ev("block_loop_detected"), None)
    assert dec.event_type == NEEDS_DECISION


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
