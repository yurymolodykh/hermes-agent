"""Unit tests for the wake-gate predicate (t_0a121f42).

Covers the pure function ``should_wake_agent_for_event`` — the wake-side
complement of ``should_suppress_api_server_event``:

  * replace mode OFF — every historical wake kind still wakes (additive
    contract unchanged: completed/gave_up/crashed/timed_out/blocked);
  * terminal ``branch_summary`` — MUST wake (the only mission wake);
  * terminal-failure / human-attention kinds (gave_up, crashed, timed_out,
    block_loop_detected, review_requested) — wake;
  * ``triage`` (event kind or card status) — wake;
  * ``blocked`` with ``provider_route`` — wake; needs-human-fix kinds
    (needs_input / capability / legacy un-typed) — wake;
  * self-resolving ``blocked`` (dependency / transient) — NO wake;
  * routine ``completed`` / ``status`` / ``archived`` / ``unblocked`` —
    NO wake (intermediate, the branch_summary итог owns the single wake);
  * child-card / split-close routine events — NO wake;
  * defensive: None event / no kind / non-dict payload — NO wake.
"""

import pytest

from gateway.kanban_watchers import should_wake_agent_for_event


class _Ev:
    """Minimal event stand-in matching hermes_cli.kanban_db.Event fields."""

    def __init__(self, kind, payload=None):
        self.kind = kind
        self.payload = payload or {}


class _Card:
    """Minimal card stand-in exposing the fields the predicate reads."""

    def __init__(self, status="running"):
        self.status = status
        self.id = "t_card"
        self.title = "card"


def _wake(kind, *, payload=None, card=None, parent=None, replace=True):
    ev = _Ev(kind, payload)
    return should_wake_agent_for_event(
        ev, card=card, parent=parent, branch_summary_replace=replace,
    )


# ---------------------------------------------------------------------------
# Replace-mode OFF: additive behavior — everything historical still wakes.
# ---------------------------------------------------------------------------


def test_replace_off_always_wakes_terminal_kinds():
    for kind in ("completed", "blocked", "gave_up", "crashed", "timed_out"):
        assert _wake(kind, replace=False) is True
        assert _wake(kind, card=_Card(), parent=_Card(), replace=False) is True


def test_replace_off_wakes_routine_events():
    # Without branch_summary_replace the notifier keeps the historical
    # per-event wake contract — status / archived etc. are not in _WAKE_KINDS
    # so they never woke; completed-family stays wake-worthy.
    assert _wake("status", replace=False) is True
    assert _wake("archived", replace=False) is True
    assert _wake("unblocked", replace=False) is True


# ---------------------------------------------------------------------------
# Terminal branch итог: MUST wake.
# ---------------------------------------------------------------------------


def test_terminal_branch_summary_wakes():
    assert _wake("branch_summary") is True
    assert _wake("branch_summary", card=_Card(), parent=_Card()) is True


# ---------------------------------------------------------------------------
# Human-attention kinds: wake.
# ---------------------------------------------------------------------------


def test_block_loop_detected_wakes():
    assert _wake("block_loop_detected") is True
    assert _wake("block_loop_detected", card=_Card("running"),
                 parent=_Card("running")) is True


def test_triage_event_kind_wakes():
    assert _wake("triage") is True
    assert _wake("triage", card=_Card("triage")) is True


def test_triage_card_status_wakes():
    # Routine completed on a card whose status is triage — still wake.
    assert _wake("completed", card=_Card("triage")) is True
    assert _wake("status", card=_Card("triage")) is True


def test_terminal_failure_kinds_wake():
    for kind in ("gave_up", "crashed", "timed_out", "review_requested"):
        assert _wake(kind) is True
        assert _wake(kind, card=_Card(), parent=_Card()) is True


# ---------------------------------------------------------------------------
# blocked + provider_route (provider outage): wake.
# ---------------------------------------------------------------------------


def test_blocked_provider_route_wakes():
    ev = _Ev("blocked", {"reason": "API down",
                          "kind": "needs_input", "provider_route": "x/y"})
    assert should_wake_agent_for_event(
        ev, card=_Card("blocked"), parent=None, branch_summary_replace=True,
    ) is True
    # Even on a child card a provider outage still wakes.
    assert _wake("blocked", payload={"provider_route": "x/y"},
                 card=_Card("blocked"), parent=_Card()) is True


# ---------------------------------------------------------------------------
# blocked needs-human-fix kinds: wake.
# ---------------------------------------------------------------------------


def test_blocked_needs_input_wakes():
    assert _wake("blocked", payload={"kind": "needs_input"}) is True
    assert _wake("blocked", payload={"kind": "capability"}) is True
    # Legacy un-typed block → treated as a human blocker.
    assert _wake("blocked", payload={}) is True
    assert _wake("blocked", payload=None) is True


def test_blocked_self_resolving_no_wake():
    assert _wake("blocked", payload={"kind": "dependency"}) is False
    assert _wake("blocked", payload={"kind": "transient"}) is False


# ---------------------------------------------------------------------------
# Routine intermediate events: NO wake.
# ---------------------------------------------------------------------------


def test_routine_completed_no_wake():
    assert _wake("completed") is False
    assert _wake("status") is False
    assert _wake("archived") is False
    assert _wake("unblocked") is False


def test_child_completed_no_wake():
    assert _wake("completed", card=_Card(), parent=_Card()) is False
    assert _wake("status", card=_Card(), parent=_Card()) is False
    assert _wake("archived", card=_Card(), parent=_Card()) is False
    assert _wake("unblocked", card=_Card(), parent=_Card()) is False


def test_split_close_no_wake():
    # [split N/M] one-off child close while the ancestor mission is still
    # running — must NOT wake the creator.
    assert _wake("completed", card=_Card(), parent=_Card()) is False


def test_root_routine_kinds_no_wake():
    # Under replace mode the root's own completion ping is not the итог —
    # the branch_summary stage owns the single wake.
    assert _wake("completed", card=_Card(), parent=None) is False
    assert _wake("status", card=_Card(), parent=None) is False


def test_root_blocked_self_resolving_no_wake():
    assert _wake("blocked", payload={"kind": "dependency"},
                 card=_Card(), parent=None) is False


# ---------------------------------------------------------------------------
# Defensive: None / malformed inputs.
# ---------------------------------------------------------------------------


def test_none_event_no_wake():
    assert should_wake_agent_for_event(
        None, card=_Card(), parent=None, branch_summary_replace=True,
    ) is False


def test_event_without_kind_no_wake():
    class _Bare:
        pass
    assert should_wake_agent_for_event(
        _Bare(), card=_Card(), parent=None, branch_summary_replace=True,
    ) is False


def test_non_dict_payload_handled():
    class _Weird:
        kind = "blocked"

        @property
        def payload(self):
            raise TypeError("boom")
    ev = _Weird()
    assert should_wake_agent_for_event(
        ev, card=_Card("blocked"), parent=None, branch_summary_replace=True,
    ) is True


def test_non_dict_payload_routine_no_wake():
    class _Weird:
        kind = "completed"

        @property
        def payload(self):
            raise TypeError("boom")
    ev = _Weird()
    assert should_wake_agent_for_event(
        ev, card=_Card(), parent=None, branch_summary_replace=True,
    ) is False


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
