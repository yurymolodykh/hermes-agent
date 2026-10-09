"""Tests for the per-provider LLM drop-rate ledger (``/status`` provider health)."""

from datetime import datetime
from pathlib import Path

import pytest

from hermes_cli.observability import provider_health


def _write(path: Path, line: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


@pytest.fixture
def ledger_home(tmp_path, monkeypatch):
    home = tmp_path / "metrics"
    monkeypatch.setattr(provider_health, "_ledger_dir", lambda home_=None: home)
    return home


def test_handles_hook_gates_on_policy(monkeypatch):
    monkeypatch.setattr(provider_health, "enabled", lambda: True)
    assert provider_health.handles_hook("post_api_request")
    assert provider_health.handles_hook("api_request_error")
    assert not provider_health.handles_hook("on_session_start")
    monkeypatch.setattr(provider_health, "enabled", lambda: False)
    assert not provider_health.handles_hook("post_api_request")


def test_enabled_defaults_on_with_broken_config(monkeypatch):
    def _boom():
        raise RuntimeError("no config")

    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "read_raw_config_readonly", _boom)
    assert provider_health.enabled() is True


def test_enabled_respects_telemetry_opt_out(monkeypatch):
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "read_raw_config_readonly",
        lambda: {"telemetry": {"provider_health": False}},
    )
    assert provider_health.enabled() is False
    monkeypatch.setattr(
        config_mod,
        "read_raw_config_readonly",
        lambda: {"telemetry": {"shared_metrics": True}},
    )
    assert provider_health.enabled() is True


def test_observe_records_ok_and_err(ledger_home):
    provider_health.observe_lifecycle(
        "post_api_request",
        provider="zai",
        model="glm-5.3-flash",
        api_request_id="t1:api:1",
    )
    provider_health.observe_lifecycle(
        "api_request_error",
        provider="zai",
        model="glm-5.3-flash",
        api_request_id="t1:api:1",
        status_code=503,
        retryable=True,
        error={"type": "APIStatusError", "message": "boom"},
    )
    files = list(ledger_home.glob("provider_health-*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 2
    first = provider_health.json.loads(content[0])
    assert first["p"] == "zai"
    assert first["r"] == "t1:api:1"
    assert first["o"] == "ok"


def test_retries_then_success_counts_once(ledger_home):
    # Two failed attempts, then success — same api_request_id => one ok request.
    provider_health.observe_lifecycle(
        "api_request_error", provider="zai", api_request_id="t1:api:1", status_code=429
    )
    provider_health.observe_lifecycle(
        "api_request_error", provider="zai", api_request_id="t1:api:1", status_code=503
    )
    provider_health.observe_lifecycle(
        "post_api_request", provider="zai", api_request_id="t1:api:1"
    )
    stats = provider_health.provider_drop_stats()
    assert stats["zai"] == {"total": 1, "dropped": 0, "drop_pct": 0.0}


def test_exhausted_retries_count_as_drop(ledger_home):
    provider_health.observe_lifecycle(
        "api_request_error", provider="zai", api_request_id="t1:api:2", status_code=500
    )
    provider_health.observe_lifecycle(
        "api_request_error", provider="zai", api_request_id="t1:api:2", status_code=500
    )
    provider_health.observe_lifecycle(
        "api_request_error", provider="zai", api_request_id="t1:api:2", status_code=500
    )
    stats = provider_health.provider_drop_stats()
    assert stats["zai"]["total"] == 1
    assert stats["zai"]["dropped"] == 1
    assert stats["zai"]["drop_pct"] == 100.0


def test_window_filters_old_events(ledger_home):
    now = datetime(2026, 9, 30, 12, 0, 0)
    old_ts = now.timestamp() - 25 * 3600
    in_ts = now.timestamp() - 1 * 3600
    _write(
        ledger_home / "provider_health-20260929.jsonl",
        {"ts": old_ts, "p": "oldprov", "r": "t0:api:1", "o": "err"},
    )
    _write(
        ledger_home / "provider_health-20260930.jsonl",
        {"ts": in_ts, "p": "newprov", "r": "t1:api:1", "o": "ok"},
    )
    stats = provider_health.provider_drop_stats(window_hours=24.0, now=now)
    assert set(stats) == {"newprov"}
    assert stats["newprov"]["total"] == 1


def test_per_provider_isolation(ledger_home):
    provider_health.observe_lifecycle(
        "post_api_request", provider="alpha", api_request_id="t1:api:1"
    )
    provider_health.observe_lifecycle(
        "api_request_error", provider="beta", api_request_id="t1:api:2"
    )
    stats = provider_health.provider_drop_stats()
    assert stats["alpha"]["drop_pct"] == 0.0
    assert stats["beta"]["drop_pct"] == 100.0


def test_format_lines_sorted_and_localized(ledger_home):
    now = datetime(2026, 9, 30, 12, 0, 0)
    ts = now.timestamp()
    for i in range(3):
        _write(
            ledger_home / "provider_health-20260930.jsonl",
            {"ts": ts, "p": "busy", "r": f"t:api:{i}", "o": "ok"},
        )
    _write(
        ledger_home / "provider_health-20260930.jsonl",
        {"ts": ts, "p": "flaky", "r": "t:api:9", "o": "err"},
    )
    stats = provider_health.provider_drop_stats(now=now)
    text = provider_health.format_provider_health_lines(stats)
    assert "busy" in text and "flaky" in text
    assert "100%" in text and "0%" in text


def test_format_lines_empty_state():
    text = provider_health.format_provider_health_lines({})
    assert "24" in text  # the "no data" hint mentions the window


def test_observe_never_raises_on_garbage(ledger_home):
    provider_health.observe_lifecycle("post_api_request")  # no provider/id at all
    provider_health.observe_lifecycle("api_request_error", provider=None, api_request_id=None)
    assert provider_health.provider_drop_stats() == {}
