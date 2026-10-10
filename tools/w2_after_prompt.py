#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emit the byte-exact "after" system prompt for the W1a polygon (W2/W3).

P1 gate-emulation (mission token-optimization-rollout, W2): the A/B harness
consumes the system prompt via ``--system <file>`` and ``--no-anticache``.
This script serializes the three prompt tiers + the joined full prompt once so
W3 can (a) feed the identical file to BOTH before/after arms (M1/M2/M3/M10
differ only in which config flags are ON when this script runs) and (b) diff
stable/context/volatile sizes across arms.

Usage (run from the W2 worktree with the after-arms' flags ON in config):
    python tools/w2_after_prompt.py > /tmp/after_prompt.json
    # then: harness.py --mode after --system <(jq -r .full /tmp/after_prompt.json) ...
"""

from __future__ import annotations

import json
import sys

from hermes_cli.prompt_size import _build_inspection_agent
from agent.system_prompt import build_system_prompt, build_system_prompt_parts  # noqa: F401


def main() -> int:
    agent = _build_inspection_agent("cli")
    parts = build_system_prompt_parts(agent)
    full = build_system_prompt(agent)
    out = {
        "stable": parts.get("stable", ""),
        "context": parts.get("context", ""),
        "volatile": parts.get("volatile", ""),
        "full": full,
        "tools": getattr(agent, "tools", None) or [],
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
