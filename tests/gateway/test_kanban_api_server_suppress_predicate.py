"""Unit tests for the api_server-lane intermediate-event suppression predicate.

Covers the pure function ``should_suppress_api_server_event`` (t_ec550192):

  * child cards (parent present) — suppressed;
  * root / unsubscribed tasks, routine non-terminal kinds — suppressed
    (under replace mode the branch итог is the single channel message);
  * terminal ``branch_summary`` — NEVER suppressed;
  * ``block_loop_detected`` / ``triage`` (event kind or card status) — NEVER
    suppressed (human action needed);
  * ``blocked`` with ``provider_route`` (provider outage) — NOT suppressed;
  * ``blocked`` needs-human-fix kinds (needs_input / capability / legacy
    un-typed) — NOT suppressed;
  * self-resolving ``blocked`` (dependency / transient) — suppressed;
  * replace mode OFF — nothing suppressed (additive behavior unchanged).

The TG delivery-policy gate is deliberately out of scope: the predicate is a
pure api_server-lane decision and never inspects platform strings.
"""

import pytest

from gateway.kanban_watchers import (
    _owui_suppress_intermediate_event,
    should_suppress_api_server_event,
)


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


def _supp(kind, *, payload=None, card=None, parent=None, replace=True):
    ev = _Ev(kind, payload)
    return should_suppress_api_server_event(
        ev, card=card, parent=parent, branch_summary_replace=replace,
    )


# ---------------------------------------------------------------------------
# Replace-mode OFF: additive behavior — nothing suppressed.
# ---------------------------------------------------------------------------


def test_replace_off_never_suppresses():
    for kind in ("completed", "status", "blocked", "archived", "unblocked",
                 "branch_summary", "block_loop_detected"):
        assert _supp(kind, replace=False) is False
        assert _supp(kind, card=_Card(), parent=_Card(), replace=False) is False


# ---------------------------------------------------------------------------
# Terminal branch итог: NEVER suppressed.
# ---------------------------------------------------------------------------


def test_terminal_branch_summary_never_suppressed():
    assert _supp("branch_summary") is False
    assert _supp("branch_summary", card=_Card(), parent=_Card()) is False


# ---------------------------------------------------------------------------
# Human-attention kinds: NEVER suppressed.
# ---------------------------------------------------------------------------


def test_block_loop_detected_never_suppressed():
    assert _supp("block_loop_detected") is False
    # Even for a routed-to-triage card / any card.
    assert _supp("block_loop_detected", card=_Card("running"),
                 parent=_Card("running")) is False


def test_triage_event_kind_never_suppressed():
    assert _supp("triage") is False
    assert _supp("triage", card=_Card("triage")) is False


def test_triage_card_status_never_suppressed():
    # Routine completed on a card whose status is triage — still loud.
    assert _supp("completed", card=_Card("triage")) is False
    assert _supp("status", card=_Card("triage")) is False


def test_terminal_failure_kinds_never_suppressed():
    for kind in ("gave_up", "crashed", "timed_out", "review_requested"):
        assert _supp(kind) is False
        assert _supp(kind, card=_Card(), parent=_Card()) is False


# ---------------------------------------------------------------------------
# blocked + provider_route (provider outage): NOT suppressed.
# ---------------------------------------------------------------------------


def test_blocked_provider_route_not_suppressed():
    ev = _Ev("blocked", {"reason": "API down",
                          "kind": "needs_input", "provider_route": "x/y"})
    assert should_suppress_api_server_event(
        ev, card=_Card("blocked"), parent=None, branch_summary_replace=True,
    ) is False
    # Even on a child card (parent present) a provider outage stays loud.
    assert _supp("blocked", payload={"provider_route": "x/y"},
                 card=_Card("blocked"), parent=_Card()) is False


# ---------------------------------------------------------------------------
# blocked needs-human-fix kinds: NOT suppressed.
# ---------------------------------------------------------------------------


def test_blocked_needs_input_not_suppressed():
    assert _supp("blocked", payload={"kind": "needs_input"}) is False
    assert _supp("blocked", payload={"kind": "capability"}) is False
    # Legacy un-typed block → treated as a human blocker.
    assert _supp("blocked", payload={}) is False
    assert _supp("blocked", payload=None) is False


def test_blocked_self_resolving_suppressed():
    assert _supp("blocked", payload={"kind": "dependency"}) is True
    assert _supp("blocked", payload={"kind": "transient"}) is True


# ---------------------------------------------------------------------------
# Child cards (parent present): suppressed.
# ---------------------------------------------------------------------------


def test_child_completed_suppressed():
    assert _supp("completed", card=_Card(), parent=_Card()) is True
    assert _supp("status", card=_Card(), parent=_Card()) is True
    assert _supp("archived", card=_Card(), parent=_Card()) is True
    assert _supp("unblocked", card=_Card(), parent=_Card()) is True


def test_child_split_close_suppressed():
    # [split N/M] one-off child close while the ancestor mission is still
    # running — suppressed on the api_server lane.
    assert _supp("completed", card=_Card(), parent=_Card()) is True


# ---------------------------------------------------------------------------
# Root / unsubscribed tasks: routine non-terminal kinds suppressed, too.
# ---------------------------------------------------------------------------


def test_root_routine_kinds_suppressed():
    # Under replace mode the root's own completion ping is not the итог — the
    # branch_summary stage owns that single channel message.
    assert _supp("completed", card=_Card(), parent=None) is True
    assert _supp("status", card=_Card(), parent=None) is True
    assert _supp("archived", card=_Card(), parent=None) is True
    assert _supp("unblocked", card=_Card(), parent=None) is True


def test_root_blocked_self_resolving_suppressed():
    assert _supp("blocked", payload={"kind": "dependency"},
                 card=_Card(), parent=None) is True


# ---------------------------------------------------------------------------
# Defensive: None / malformed inputs.
# ---------------------------------------------------------------------------


def test_none_event_not_suppressed():
    assert should_suppress_api_server_event(
        None, card=_Card(), parent=None, branch_summary_replace=True,
    ) is False


def test_event_without_kind_not_suppressed():
    class _Bare:
        pass
    assert should_suppress_api_server_event(
        _Bare(), card=_Card(), parent=None, branch_summary_replace=True,
    ) is False


def test_non_dict_payload_handled():
    class _Weird:
        kind = "blocked"

        @property
        def payload(self):
            raise TypeError("boom")
    ev = _Weird()
    assert should_suppress_api_server_event(
        ev, card=_Card("blocked"), parent=None, branch_summary_replace=True,
    ) is False


# ---------------------------------------------------------------------------
# Legacy shim still routes through the canonical predicate.
# ---------------------------------------------------------------------------


def test_legacy_shim_forwards_behavior():
    # api_server lane + replace mode → routine kinds suppressed.
    assert _owui_suppress_intermediate_event("completed", _Ev("completed"),
                                             True, "api_server") is True
    assert _owui_suppress_intermediate_event("status", _Ev("status"),
                                             True, "api_server") is True
    # blocked needs-human-fix → loud.
    assert _owui_suppress_intermediate_event(
        "blocked", _Ev("blocked", {"kind": "needs_input"}), True,
        "api_server") is False
    # blocked provider_route → loud.
    assert _owui_suppress_intermediate_event(
        "blocked", _Ev("blocked", {"provider_route": "a/b"}), True,
        "api_server") is False
    # block_loop_detected → loud.
    assert _owui_suppress_intermediate_event(
        "block_loop_detected", _Ev("block_loop_detected"), True,
        "api_server") is False
    # branch_summary → loud.
    assert _owui_suppress_intermediate_event(
        "branch_summary", _Ev("branch_summary"), True, "api_server") is False
    # Non-api_server lane → never silenced by this gate.
    assert _owui_suppress_intermediate_event("completed", _Ev("completed"),
                                             True, "telegram") is False
    # Replace mode off → never silenced.
    assert _owui_suppress_intermediate_event("completed", _Ev("completed"),
                                             False, "api_server") is False


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
