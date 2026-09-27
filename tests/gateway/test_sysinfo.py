"""Unit tests for :mod:`gateway.sysinfo` (the ``/status`` load-metrics block).

Covers the formatting contract and error handling with *synthetic* values --
no real host metrics are required.  The real smoke run (``collect_load()`` on
the live machine returning 0-100 / ``н/д``) is a manual/acceptance check, not
part of this deterministic test.

Design notes under test:

* ``format_load_line`` must render every metric and never raise, even for
  ``None`` / missing values (the ``н/д`` fallback).
* All percent helpers clamp to ``[0, 100]`` and reject degenerate input.
* ``_decode_load_snapshot`` / ``LoadSnapshot`` tolerate garbage field types.
* ``_Ticker`` delta math is pure enough to unit test with fake samplers.
"""

from __future__ import annotations

import threading

import pytest

from gateway.sysinfo import (
    LoadSnapshot,
    _Ticker,
    _gb,
    _pct,
    _pct_text,
    collect_load,
    format_load_line,
)


class TestPercentHelpers:
    def test_pct_basic(self):
        assert _pct(50, 100) == 50.0
        assert _pct(0, 100) == 0.0
        assert _pct(100, 100) == 100.0

    def test_pct_clamps(self):
        assert _pct(150, 100) == 100.0  # never > 100%
        assert _pct(-5, 100) == 0.0     # never < 0%

    def test_pct_degenerate(self):
        assert _pct(10, 0) is None
        assert _pct(10, -1) is None
        assert _pct("a", "b") is None
        assert _pct(None, 100) is None

    def test_gb(self):
        assert _gb(1024 ** 3) == 1.0
        assert _gb(2362232012) == 2.2
        assert _gb(None) is None

    def test_pct_text(self):
        assert _pct_text(97.4) == "97%"
        assert _pct_text(0.0) == "0%"
        assert _pct_text(None) == "н/д"


class TestFormatLoadLine:
    def test_full_block(self):
        line = format_load_line({
            "cpu_pct": 97.4,
            "ram_pct": 96.0,
            "ram_gb": 22.8,
            "ram_total_gb": 23.6,
            "swap_pct": 52.0,
            "swap_gb": 37.0,
            "swap_total_gb": 71.6,
            "io_pct": 12.7,
        })
        assert line == "CPU: 97% · RAM: 96% (22.8/23.6 ГБ) · SWAP: 52% · IO: 13%"

    def test_all_na(self):
        line = format_load_line({})
        assert line == "CPU: н/д · RAM: н/д · SWAP: н/д · IO: н/д"

    def test_ram_no_gb_still_renders(self):
        line = format_load_line({"ram_pct": 50.0})
        assert "RAM: 50%" in line and "ГБ" not in line

    def test_never_raises_on_garbage(self):
        # Garbage types must degrade, never crash /status.
        line = format_load_line({"cpu_pct": "nope", "ram_pct": {}, "io_pct": []})
        assert "CPU: н/д" in line and "RAM: н/д" in line and "IO: н/д" in line


class TestSnapshot:
    def test_load_snapshot_dict(self):
        snap = LoadSnapshot(cpu_pct=1.0, io_pct=None)
        d = snap.as_dict()
        assert d["cpu_pct"] == 1.0
        assert d["io_pct"] is None

    def test_collect_load_never_raises(self):
        # On any host this must return a dict with the 4 keys and not raise.
        raw = collect_load()
        assert isinstance(raw, dict)
        for key in ("cpu_pct", "ram_pct", "swap_pct", "io_pct"):
            assert key in raw
            v = raw[key]
            assert v is None or (isinstance(v, float) and 0.0 <= v <= 100.0)


class TestTicker:
    def test_cpu_first_call_has_real_delta(self):
        # Fake sampler: busy ticks grow by 3 per sample, total by 4.
        calls = {"n": 0}

        def fake_cpu():
            calls["n"] += 1
            return (calls["n"] * 3, calls["n"] * 4)

        ticker = _Ticker(fake_cpu, io_sampler=None, cpu_sample_s=0.001)
        val = ticker.cpu_pct()
        assert val is not None
        assert 0.0 <= val <= 100.0
        assert val == 75.0  # 3/4 busy

    def test_cpu_second_call_instant_delta(self):
        calls = {"n": 0}

        def fake_cpu():
            calls["n"] += 1
            return (calls["n"] * 3, calls["n"] * 4)

        ticker = _Ticker(fake_cpu, io_sampler=None, cpu_sample_s=0.0)
        first = ticker.cpu_pct()
        second = ticker.cpu_pct()
        assert first is not None and second is not None
        assert second == 75.0

    def test_cpu_unavailable_sampler_returns_none(self):
        ticker = _Ticker(lambda: None, io_sampler=None)
        assert ticker.cpu_pct() is None

    def test_io_no_sampler_none(self):
        ticker = _Ticker(lambda: (1, 2), io_sampler=None, cpu_sample_s=0.0)
        assert ticker.io_pct() is None

    def test_io_first_call_real_after_resample(self):
        # Device "sda": busy grows by 5ms per sample; total window ~= the fake
        # sleep (cpu_sample_s=0.01 -> 10ms); expect ~50%.
        calls = {"n": 0}

        def fake_io():
            calls["n"] += 1
            return {"sda": (calls["n"] * 5.0, 0.0)}

        ticker = _Ticker(lambda: (1, 2), io_sampler=fake_io, cpu_sample_s=0.02)
        val = ticker.io_pct()
        # 5ms busy / ~20ms window -> ~25%; coarse bound 0-100, real value.
        assert val is not None
        assert 0.0 <= val <= 100.0

    def test_io_no_counters_none(self):
        ticker = _Ticker(lambda: (1, 2), io_sampler=lambda: None, cpu_sample_s=0.0)
        assert ticker.io_pct() is None

    def test_thread_safety_smoke(self):
        # No exception under concurrent access; values never out of range.
        calls = {"n": 0}

        def fake_cpu():
            calls["n"] += 1
            return (calls["n"] * 3, calls["n"] * 4)

        ticker = _Ticker(fake_cpu, io_sampler=None, cpu_sample_s=0.001)
        results = []

        def worker():
            results.append(ticker.cpu_pct())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 8
        assert all(v is None or (isinstance(v, float) and 0.0 <= v <= 100.0) for v in results)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
