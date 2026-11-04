"""Wake-side contract: which events may wake the creator's agent session.

HISTORY — why this module no longer imports a predicate
------------------------------------------------------
This module used to import ``should_wake_agent_for_event`` from
``gateway.kanban_watchers``.  That function was the pre-R1 wake predicate.  The
R1 delivery-engine consolidation (``d3cfc258d5``, 2026-10-21) deleted it and
moved the wake decision into ``gateway.delivery_engine``:

  * classification -> ``classify_event()`` (single authority);
  * wake eligibility -> ``DeliveryEngine.handle_event()``:
    ``want_wake = et in (FINAL, NEEDS_DECISION)``.

``gateway/REFACTOR-CHANGELOG.md`` line 74 records the accepted R1 contract —
"wake только [final]+[needs-decision]".  The old symbol has zero live call
sites and is gone from every revision from ``d3cfc258d5`` onward.

Re-pointing this module was NOT a cosmetic rename: the retired predicate's
truth table contradicted the shipped engine on three groups of kinds, and in
each case the ENGINE is the correct one:

  * ``gave_up`` / ``crashed`` / ``timed_out`` — the old predicate woke on all
    three.  ``crashed`` and ``timed_out`` auto-retry (see the
    ``delivery_engine._ABORTED_KINDS`` docstring), so waking the creator on
    them is waking on intermediate churn.  All three are ``[aborted]``
    deliveries with wake OFF; ``gave_up`` still reaches the user as the branch
    итог (``BRANCH_TERMINAL_FAILURE_KIND`` in ``kanban_db``).
  * a completing ROOT whose branch really closed — the old predicate refused
    to wake, the engine wakes because that close IS the terminal итог.
  * ``branch_summary`` — the engine does not classify the итог event; the
    branch-summary watcher wakes through ``handle_branch_final``.  That
    surface is covered by ``tests/gateway/test_kanban_wake_mechanics_e2e.py``
    and is deliberately NOT re-asserted here (see the note at the bottom).

So this module now asserts the LIVE wake contract — "does this event type
qualify for a wake turn" — computed exactly the way the engine computes it.

ONE BEHAVIOUR CHANGE IS RECORDED, NOT HIDDEN
--------------------------------------------
The retired predicate woke the creator for ``gave_up`` / ``crashed`` /
``timed_out``.  The engine does not (``_ABORTED_KINDS``: "crashed/timed_out
auto-retry, so they are NOT a creator wake under the design"), and that is the
accepted R1 contract in ``REFACTOR-CHANGELOG.md`` line 74.  The assertions
below encode the engine's contract.  For ``gave_up`` the creator is still
reached — through the branch итог, which is the single wake for a mission —
so the practical capability is preserved; only the redundant per-event wake is
gone.  If this ever needs to change, it must change in
``delivery_engine.handle_event`` (``want_wake``), and
``test_terminal_failure_kinds`` will fail loudly.
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

#: The engine's wake contract (``DeliveryEngine.handle_event``).
#: Kept as a local mirror ON PURPOSE: if someone widens or narrows the
#: engine's rule, these tests must fail loudly rather than silently agree.
WAKE_TYPES = (FINAL, NEEDS_DECISION)


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


CHILD = _ctx()
ROOT_TERMINAL = _ctx(branch_terminal=True, is_root=True, card_status="done")


def _wakes(kind, *, payload=None, ctx=None, task=None, **kwargs):
    """True when this event qualifies for a wake turn, per the engine."""
    dec = classify_event(
        task or _task(), _ev(kind, payload), dict(ctx if ctx is not None else CHILD),
        **kwargs,
    )
    return dec.event_type in WAKE_TYPES


# ---------------------------------------------------------------------------
# Terminal branch итог: the ONE wake the creator gets for a mission.
# ---------------------------------------------------------------------------


def test_terminal_root_completion_wakes():
    assert _wakes("completed", ctx=ROOT_TERMINAL, task=_task("done")) is True


def test_terminal_root_completion_is_final_not_aborted():
    dec = classify_event(_task("done"), _ev("completed"), dict(ROOT_TERMINAL))
    assert dec.event_type == FINAL


# ---------------------------------------------------------------------------
# Human-action events: wake, even on a descendant.
# ---------------------------------------------------------------------------


def test_human_attention_kinds_wake():
    for kind in ("block_loop_detected", "review_requested", "triage"):
        assert _wakes(kind) is True


def test_terminal_failure_kinds():
    # gave_up / crashed / timed_out are [aborted] DELIVERIES, not wakes.
    # crashed + timed_out auto-retry; gave_up reaches the user via the итог.
    for kind in ("gave_up", "crashed", "timed_out"):
        dec = classify_event(_task(), _ev(kind), dict(CHILD))
        assert dec.event_type == ABORTED
        assert dec.event_type not in WAKE_TYPES


def test_blocked_needs_human_fix_wakes():
    for block_kind in ("needs_input", "capability"):
        assert _wakes("blocked", payload={"kind": block_kind}) is True


def test_blocked_provider_route_wakes():
    # Provider outage — the operator must pick a route.
    assert _wakes("blocked", payload={"provider_route": "x/y"}) is True
    assert _wakes(
        "blocked",
        payload={"reason": "API down", "kind": "needs_input", "provider_route": "x/y"},
    ) is True


def test_blocked_legacy_untyped_wakes():
    # Un-typed block == human decision (branch_state semantics).
    assert _wakes("blocked", payload={}) is True
    assert _wakes("blocked", payload=None) is True


def test_triage_card_status_wakes():
    ctx = _ctx(card_status="triage")
    for kind in ("completed", "status", "archived"):
        assert _wakes(kind, ctx=ctx) is True


def test_triage_status_beats_self_resolving_block():
    ctx = _ctx(card_status="triage")
    assert _wakes("blocked", payload={"kind": "dependency"}, ctx=ctx) is True


# ---------------------------------------------------------------------------
# Routine / intermediate events: NO wake.
# ---------------------------------------------------------------------------


def test_routine_kinds_no_wake():
    for kind in ("completed", "status", "archived", "unblocked"):
        assert _wakes(kind) is False


def test_child_completed_no_wake():
    # The whole point of replace mode: an intermediate child close must not
    # spend a synthetic agent turn.
    assert _wakes("completed") is False
    assert classify_event(_task(), _ev("completed"), dict(CHILD)).event_type is None


def test_root_routine_kinds_no_wake():
    # Root with a subscribed descendant that is still open: not the итог.
    ctx = _ctx(branch_terminal=False, is_root=True, card_status="done")
    assert _wakes("completed", ctx=ctx, task=_task("done")) is False
    assert _wakes("status", ctx=ctx) is False


def test_self_resolving_block_no_wake():
    for block_kind in ("dependency", "transient"):
        assert _wakes("blocked", payload={"kind": block_kind}) is False


def test_root_self_resolving_block_no_wake():
    ctx = _ctx(is_root=True)
    assert _wakes("blocked", payload={"kind": "dependency"}, ctx=ctx) is False


def test_live_status_no_wake():
    # Live status edits the existing message; it must never spend a wake turn.
    for kind in ("spawned", "claimed"):
        dec = classify_event(_task(), _ev(kind), dict(CHILD))
        assert dec.event_type == LIVE_STATUS
        assert dec.event_type not in WAKE_TYPES
    assert _wakes("status", payload={"status": "running"}) is False


def test_unclassified_kind_no_wake():
    assert _wakes("weird_kind") is False


def test_do_not_deliver_never_wakes():
    assert _wakes("completed", ctx=ROOT_TERMINAL, task=_task("done"),
                  do_not_deliver=True) is False
    assert _wakes("block_loop_detected", do_not_deliver=True) is False


# ---------------------------------------------------------------------------
# Defensive: None / malformed inputs never wake (and never crash).
# ---------------------------------------------------------------------------


def test_none_event_no_wake():
    dec = classify_event(_task(), None, dict(CHILD))
    assert (dec.event_type in WAKE_TYPES) is False


def test_event_without_kind_no_wake():
    dec = classify_event(_task(), object(), dict(CHILD))
    assert (dec.event_type in WAKE_TYPES) is False


def test_non_dict_payload_tolerated():
    for payload in (None, "not-a-dict", 42):
        assert _wakes("blocked", payload=payload) is True


# ---------------------------------------------------------------------------
# NOTE on ``branch_summary``
# ---------------------------------------------------------------------------
# The retired predicate returned True for kind == "branch_summary".  The engine
# does NOT classify that event (classify_event returns None): the итог event is
# not a notifier-stream trigger — the branch-summary watcher delivers and wakes
# through ``DeliveryEngine.handle_branch_final(terminal=True)``.  Asserting a
# wake here via classify_event would encode a false contract.  The итог wake is
# covered end-to-end by
# ``tests/gateway/test_kanban_wake_mechanics_e2e.py::test_terminal_branch_summary_wakes``
# (currently RED — tracked as a separate engine defect, see kanban t_0ea56ccc).


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
