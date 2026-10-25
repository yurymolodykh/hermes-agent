"""PWA-path baseline test (task t_e9dbbf90, W2).

Reads the live probe results produced by ``tools/pwa_probe.py`` (a JSON file
with per-request: latency, fast_lane decision, routed_profile) and asserts the
current-code expectations:

  E1. Triggers «подумай» and «гигантская задача» reach gateway/run.py over the
      PWA (yadisk_bridge) path and are recognized -> fast_lane=SLOW.
  E2. A markerless fast PWA request is routed to the fast executor profile
      (11-fast-answers) when ``fast_lane.enabled`` and
      ``fast_lane.route_to_profile`` are both true (parity with api_server).

E2 is currently RED on the live code — that is the reproducible regression this
test suite exists to expose. When the fix lands (task t_b030b28a), E2 turns
green without touching this test.

Run:
  python tools/pwa_probe.py                  # produce results JSON
  pytest tests/integration/test_pwa_path_baseline.py

Env:
  W2_PROBE_OUT  results JSON path (same default as the probe).
"""
from __future__ import annotations

import json
import os
import unittest

import pytest

OUT_PATH = os.environ.get(
    "W2_PROBE_OUT",
    os.path.join(os.path.expanduser("~"), "w2_probe_results.json"),
)

#: Expected slow decision per request tag (mirror of tools/pwa_probe.py).
EXPECT = {
    "ordinary": False,
    "podumay": True,   # marker 2 (TG «подумай») -> SLOW
    "giant": True,     # marker 3 (always-slow phrase) -> SLOW
}


def _load() -> dict:
    if not os.path.exists(OUT_PATH):
        raise AssertionError(f"probe results not found at {OUT_PATH!r}; run tools/pwa_probe.py first")
    with open(OUT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.integration
class TestPwaPathBaseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = _load()
        cls.r = cls.results.get("results", {})
        cls.fl_config = (cls.results.get("config") or {}).get("fast_lane") or {}

    def test_probe_succeeded_all_requests(self):
        for tag in EXPECT:
            self.assertTrue(self.r.get(tag, {}).get("ok"), f"{tag}: request failed — see result")

    def test_e1_triggers_reach_gateway_and_are_recognized(self):
        for tag, expect_slow in EXPECT.items():
            item = self.r[tag]
            fl = item.get("fast_lane")
            self.assertEqual(
                fl == "SLOW",
                expect_slow,
                f"{tag}: expected slow={expect_slow} on PWA source, "
                f"got fast_lane={fl!r} (source={item.get('fast_lane_source')}, "
                f"reason={item.get('fast_lane_reason')}). "
                "E1 violated: PWA trigger not recognized at gateway/run.py.",
            )

    def test_e2_fast_pwa_requests_route_to_11_fast_answers(self):
        if not (self.fl_config.get("enabled") and self.fl_config.get("route_to_profile")):
            self.skipTest("fast_lane route_to_profile not enabled")
        item = self.r.get("ordinary", {})
        target = item.get("routed_profile")
        self.assertEqual(
            target,
            "11-fast-answers",
            "E2 violated: PWA fast request was NOT routed to 11-fast-answers "
            f"(routed_profile={target!r}). Fast-lane profile routing (Track M) "
            "exists only for api_server; the PWA path executes fast requests "
            "under 01-orchestrator (full lane). Fix belongs in t_b030b28a.",
        )


if __name__ == "__main__":
    unittest.main()
