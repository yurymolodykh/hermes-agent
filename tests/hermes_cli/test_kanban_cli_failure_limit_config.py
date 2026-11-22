"""``kanban.failure_limit`` must actually reach the CLI dispatch path.

Found while closing the auto-continue card: ``hermes kanban dispatch`` (and the
deprecated ``kanban daemon``) passed a hardcoded ``DEFAULT_FAILURE_LIMIT`` to
``dispatch_once``, so an operator who wrote ``kanban.failure_limit: 3`` in
``config.yaml`` still ran the breaker at 2 — the journal proved it with
``gave_up`` events carrying ``effective_limit=2, limit_source=dispatcher``
while the root config said 3.

The precedence is ``--failure-limit`` > ``kanban.failure_limit`` >
``DEFAULT_FAILURE_LIMIT``; a value below 1 falls through, matching the
gateway's own sanity floor.
"""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops as kops


@pytest.fixture()
def captured_dispatch(monkeypatch):
    """Capture the kwargs ``_cmd_dispatch`` hands to ``dispatch_once``."""
    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kops.kbc, "connect_closing", _null_conn)
    return captured


class _null_conn:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _args(**overrides) -> argparse.Namespace:
    base = {"dry_run": True, "max": None, "failure_limit": None, "json": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_config_failure_limit_reaches_cli_dispatch(captured_dispatch, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"kanban": {"failure_limit": 3}},
    )

    kops._cmd_dispatch(_args())

    assert captured_dispatch.get("failure_limit") == 3, (
        "kanban.failure_limit: 3 in config.yaml must reach dispatch_once; "
        f"got {captured_dispatch.get('failure_limit')!r}"
    )


def test_cli_flag_overrides_config_failure_limit(captured_dispatch, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"kanban": {"failure_limit": 3}},
    )

    kops._cmd_dispatch(_args(failure_limit=1))

    assert captured_dispatch.get("failure_limit") == 1


def test_missing_config_falls_back_to_default(captured_dispatch, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})

    kops._cmd_dispatch(_args())

    assert captured_dispatch.get("failure_limit") == kb.DEFAULT_FAILURE_LIMIT


def test_unreadable_config_falls_back_to_default(captured_dispatch, monkeypatch):
    def boom():
        raise RuntimeError("config is unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config", boom)

    kops._cmd_dispatch(_args())

    assert captured_dispatch.get("failure_limit") == kb.DEFAULT_FAILURE_LIMIT


def test_below_one_values_fall_through_the_chain():
    """0 / negative are not thresholds: they fall through to the next source."""
    assert kops._resolve_failure_limit(0, 3) == 3
    assert kops._resolve_failure_limit(-1, 3) == 3
    assert kops._resolve_failure_limit(0, 0) == kb.DEFAULT_FAILURE_LIMIT
    assert kops._resolve_failure_limit("nonsense", 3) == 3
