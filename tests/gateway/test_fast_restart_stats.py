"""Tests for the FAST_RESTART_STATS observer (path A3).

Covers the two new pieces delivered alongside the e2e harness:

1. ``gateway/fast_restart_stats.py`` — the durable one-line stats observer:
   a fast stop stamps ``.fast_restart_shutdown.json``; the next boot counts
   cron recoveries + reclaimed kanban cards (in-process accumulation) and
   emits a single ``FAST_RESTART_STATS`` JSON line, then clears the stamp so
   a later (non-fast) boot stays silent.
2. ``gateway/status.*fast_restart_request*`` — the in-band restart-request
   marker file (the missing Windows trigger from restart.py's contract):
   write → consume-for-self (PID-less fast request), stale-drop, PID-mismatch
   leave-in-place.

Acceptance criteria (A3 subset):
  1. ``emit_stats_if_fast_restart`` returns stats (and logs FAST_RESTART_STATS)
     only when a stamp exists.
  2. ``total_downtime`` is measured between the stamped shutdown ts and emit.
  3. ``resumed_cron`` / ``reclaimed_cards`` accumulate before emit and reset
     after.
  4. ``record_shutdown`` twice → second overwrites (idempotent at stop).
  5. A boot without a stamp emits nothing and leaves no file behind.
  6. Marker: string-roundtrip write/consume; stale marker dropped; marker
     naming another PID left in place.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import gateway.fast_restart_stats as frs
from gateway import status as gw_status


@pytest.fixture
def isolated_frs(monkeypatch, tmp_path):
    """Point fast_restart_stats at a temp HERMES_HOME."""
    home = tmp_path / "hermes_home"
    home.mkdir(parents=True)
    monkeypatch.setattr(frs, "_stamp_path", lambda: str(home / frs._SHUTDOWN_STAMP_FILENAME))
    frs.clear_stamp()
    # Reset in-process counters for isolation.
    monkeypatch.setattr(frs, "_cron_recovered", 0)
    monkeypatch.setattr(frs, "_cards_reclaimed", 0)
    return home


def test_emit_silent_without_stamp(isolated_frs, caplog):
    """A boot that did NOT follow a fast restart stays silent."""
    with caplog.at_level(logging.INFO):
        result = frs.emit_stats_if_fast_restart()
    assert result is None
    assert "FAST_RESTART_STATS" not in caplog.text


def test_emit_after_fast_stop_returns_measured_stats(isolated_frs, caplog):
    """After a fast stop stamp, emit returns downtime + counters and logs the line."""
    shutdown_dt = datetime.now(timezone.utc) - timedelta(seconds=9)
    frs.record_shutdown(ts_utc_iso=shutdown_dt.isoformat(), active_agents=1, active_cron=1)
    frs.record_cron_recovered(1)
    frs.record_cards_reclaimed(2)

    with caplog.at_level(logging.INFO):
        result = frs.emit_stats_if_fast_restart()

    assert result is not None
    assert result["resumed_cron"] == 1
    assert result["reclaimed_cards"] == 2
    assert 5.0 <= result["total_downtime"] <= 30.0
    # The stamp file must be gone after emit.
    assert frs._read_stamp() is None
    assert "FAST_RESTART_STATS" in caplog.text
    assert '"total_downtime"' in caplog.text


def test_no_second_emit(isolated_frs):
    """After emit clears the stamp, a second emit is silent."""
    frs.record_shutdown(ts_utc_iso=datetime.now(timezone.utc).isoformat())
    frs.record_cron_recovered(0)
    assert frs.emit_stats_if_fast_restart() is not None
    assert frs.emit_stats_if_fast_restart() is None


def test_record_shutdown_twice_overwrites(isolated_frs):
    """Two fast stops in a row overwrite the stamp (idempotent at stop)."""
    older = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    newer = datetime.now(timezone.utc).isoformat()
    frs.record_shutdown(ts_utc_iso=older)
    frs.record_shutdown(ts_utc_iso=newer)
    stamp = frs._read_stamp()
    assert stamp is not None
    assert stamp["ts"] == newer


def test_counters_reset_after_emit(isolated_frs):
    """Counters must not leak into a second emit window."""
    frs.record_shutdown(ts_utc_iso=datetime.now(timezone.utc).isoformat())
    frs.record_cron_recovered(3)
    assert frs.emit_stats_if_fast_restart()["resumed_cron"] == 3
    frs.record_shutdown(ts_utc_iso=datetime.now(timezone.utc).isoformat())
    assert frs.emit_stats_if_fast_restart()["resumed_cron"] == 0


# --- Restart-request marker (status.py) ------------------------------------


@pytest.fixture
def isolated_marker(monkeypatch, tmp_path):
    """Point the marker path helpers at a temp HERMES_HOME."""
    home = tmp_path / "marker_home"
    home.mkdir(parents=True)
    monkeypatch.setattr(
        gw_status,
        "_get_fast_restart_request_path",
        lambda: home / gw_status._FAST_RESTART_REQUEST_FILENAME,
    )
    return home


def test_marker_roundtrip(isolated_marker):
    assert gw_status.write_fast_restart_request(fast=True, reason="e2e", target_pid=None)
    payload = gw_status.consume_fast_restart_request_for_self()
    assert payload is not None
    assert payload["fast"] is True
    assert payload["reason"] == "e2e"
    # Consumed → gone.
    assert gw_status.consume_fast_restart_request_for_self() is None


def test_marker_targeted_other_pid_left_in_place(isolated_marker):
    other_pid = gw_status.os.getpid() + 123456
    assert gw_status.write_fast_restart_request(fast=True, reason="other", target_pid=other_pid)
    # Not ours → not consumed, not deleted.
    assert gw_status.consume_fast_restart_request_for_self() is None
    assert (isolated_marker / gw_status._FAST_RESTART_REQUEST_FILENAME).exists()


def test_stale_marker_dropped(isolated_marker, monkeypatch):
    stale_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(
        gw_status,
        "_utc_now_iso",
        lambda: datetime.now(timezone.utc).isoformat(),
    )
    assert gw_status.write_fast_restart_request(fast=True, reason="stale")
    (isolated_marker / gw_status._FAST_RESTART_REQUEST_FILENAME).write_text(
        json.dumps({"fast": True, "reason": "stale", "written_at": stale_iso}),
        encoding="utf-8",
    )
    assert gw_status.consume_fast_restart_request_for_self() is None
    # Stale marker cleaned up so it cannot wedge a fresh gateway.
    assert not (isolated_marker / gw_status._FAST_RESTART_REQUEST_FILENAME).exists()


def test_clear_fast_restart_request(isolated_marker):
    assert gw_status.write_fast_restart_request(fast=True, reason="x")
    gw_status.clear_fast_restart_request()
    assert not (isolated_marker / gw_status._FAST_RESTART_REQUEST_FILENAME).exists()
