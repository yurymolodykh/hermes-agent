#!/usr/bin/env python3
"""PWA-path probe / baseline measurement (task t_e9dbbf90, W2).

Reproduces the PWA request path:

    PWA -> bridge (8765) -> ingress (8766) -> gateway/run.py

by POSTing exactly what the yadisk-bridge dispatcher sends to the ingress
endpoint ``/v1/turn`` (see bridge/dispatch.py and yadisk_bridge/adapter.py
``do_POST``). Each request carries a unique probe id in the text and each
uses a fresh session, so the gateway fast_lane decision for that wire is
recoverable from gateway.log by message text.

Per request this probe records:
  * end-to-end latency (client wall-clock to the final response)
  * the chat/session id (for log correlation)
  * HTTP status / any error (HTTP errors or a slow full agent reply)

The results are written to a JSON file for the paired pytest
(``tests/integration/test_pwa_path_baseline.py``), which encodes the
current-code expectations:

  E1. markers «подумай» and «гигантская задача» MUST reach gateway/run.py and
      be recognized on the PWA (yadisk_bridge) source  ->  fast_lane=SLOW
      (source=tg-prefix / source=phrase).
  E2. with ``fast_lane.enabled=true`` AND ``fast_lane.route_to_profile=true``
      configured, a markerless fast request MUST be routed to the fast-executor
      profile (11-fast-answers), as the api_server path does
      (fast_lane=ROUTE ... target=11-fast-answers).

Defect measured on current code (2026-10-24) — E2 FAILS:
the yadisk_bridge path logs ``fast_lane=FAST ... active=True`` with no
``target=11-fast-answers``. Fast-lane profile routing (Track M, t_746614d5)
exists only in gateway/platforms/api_server.py, so PWA fast requests execute
under 01-orchestrator (full lane) instead of 11-fast-answers.

Usage:
  python tools/pwa_probe.py             # run live probes -> results JSON
  python tools/pwa_probe.py --check     # run probes then print E1/E2 verdict

Env:
  YADISK_BRIDGE_GATEWAY_URL   (default http://127.0.0.1:8766)
  YADISK_BRIDGE_INGRESS_TOKEN (required; Bearer token for POST /v1/turn)
  YADISK_BRIDGE_PROFILE       (default 01-orchestrator)
  W2_PROBE_MODEL              (default deepseek-v4-flash)
  W2_PROBE_TIMEOUT            (per-request timeout seconds, default 360)
  W2_PROBE_OUT                (results JSON path, default $HOME/w2_probe_results.json)
  W2_GATEWAY_LOG              (gateway.log path for the profile, default
                               <profile-dir>/logs/gateway.log)
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import uuid
import urllib.request

BASE = os.environ.get("YADISK_BRIDGE_GATEWAY_URL", "http://127.0.0.1:8766")
TOKEN = os.environ.get("YADISK_BRIDGE_INGRESS_TOKEN", "")
PROFILE = os.environ.get("YADISK_BRIDGE_PROFILE", "01-orchestrator")
MODEL = os.environ.get("W2_PROBE_MODEL", "deepseek-v4-flash")
TIMEOUT = float(os.environ.get("W2_PROBE_TIMEOUT", "360"))
OUT_PATH = os.environ.get(
    "W2_PROBE_OUT",
    os.path.join(os.path.expanduser("~"), "w2_probe_results.json"),
)
GATEWAY_LOG = os.environ.get(
    "W2_GATEWAY_LOG",
    os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "hermes", "profiles",
        PROFILE, "logs", "gateway.log",
    ),
)

# (tag, message template, expect_slow)
REQUESTS = [
    ("ordinary", "Скажи просто 'OK' - это тестовый запрос W2-PROBE {stamp} обычный.", False),
    ("podumay", "подумай: Скажи просто 'OK' - это тестовый запрос W2-PROBE {stamp} с подумай.", True),
    ("giant", "Составь гигантский план для W2-PROBE {stamp} гигантская задача.", True),
]

# Detect the literal text (excluding the tag marker) used to correlate the log
# line, since the gateway logs the raw inbound text verbatim.
_TAG_TO_TEXT = {
    "ordinary": "обычный",
    "podumay": "подумай:",
    "giant": "гигантская задача",
}


def send_turn(text: str) -> dict:
    st = time.monotonic()
    payload = {
        "profile": PROFILE,
        "hermes_session_id": str(uuid.uuid4()),
        "model": MODEL,
        "installation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "text": text,
        "attachments": [],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/v1/turn",
        data=body,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            return {
                "ok": True, "http": r.status,
                "latency": round(time.monotonic() - st, 2),
                "resp": json.loads(raw) if raw else None,
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False, "http": e.code,
            "latency": round(time.monotonic() - st, 2),
            "error": e.read().decode("utf-8", "replace")[:500],
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "latency": round(time.monotonic() - st, 2), "error": repr(e)[:500]}


def correlate(probe_stamp: str, results: dict) -> None:
    """Correlate each probe with its gateway fast_lane decision from the log.

    The gateway logs ``inbound message: ... msg='<text>'`` (which carries the
    probe text and stamp) and a ``fast_lane=... session=...:dm:<chat_id>`` line
    for the same turn. We recover the chat_id from the inbound line then read
    the fast_lane decision for that session key.
    """
    try:
        with open(GATEWAY_LOG, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return

    # chat_id -> (inbound_line, fast_lane_line, routed_target)
    chats: dict[str, dict] = {}
    for line in lines:
        m = re.search(r"inbound message:.*chat=([0-9a-f-]{36})", line)
        md = re.search(r"fast_lane=(FAST|SLOW|ROUTE).*?yadisk_bridge:dm:([0-9a-f-]{36})", line)
        if m and f"W2-PROBE {probe_stamp}" in line:
            chats.setdefault(m.group(1), {})["inbound"] = line.strip()
        if md:
            slot = chats.setdefault(md.group(2), {})
            slot["fast_lane"] = md.group(1)
            slot.setdefault("fast_lane_lines", []).append(line.strip())

    for tag, item in results.get("results", {}).items():
        text = item.get("text_sent") or ""
        marker_text = _TAG_TO_TEXT.get(tag)
        best = None
        for cid, slot in chats.items():
            inbound = slot.get("inbound", "")
            if f"W2-PROBE {probe_stamp}" in inbound:
                # Match the tag-specific marker in the INBOUND line itself so
                # each probe correlates to its own chat (not just the stamp,
                # which is shared by all three probes of one run).
                if marker_text and marker_text in inbound:
                    best = (cid, slot)
                    break
        if best is None:
            continue
        cid, slot = best
        item["chat_id"] = cid
        item["fast_lane"] = slot.get("fast_lane")
        lines = slot.get("fast_lane_lines") or []
        item["fast_lane_lines"] = lines
        target = None
        for ln in lines:
            tm = re.search(r"target=([\w-]+)", ln)
            if tm:
                target = tm.group(1)
            sm = re.search(r"source=(\S+)", ln)
            if sm and target is None:
                # keep the most detailed line
                pass
        item["routed_profile"] = target
        for ln in lines:
            sm = re.search(r"source=(\S+)", ln)
            if sm:
                item["fast_lane_source"] = sm.group(1)
                break
        for ln in lines:
            rs = re.search(r"reason=([^ ]+)", ln)
            if rs:
                item["fast_lane_reason"] = rs.group(1)
                break


def check(results: dict) -> int:
    """E1/E2 verdict against current-code expectations."""
    failures: list[str] = []
    passes: list[str] = []
    r = results.get("results", {})
    for tag, _tmpl, expect_slow in REQUESTS:
        item = r.get(tag) or {}
        if not item.get("ok"):
            failures.append(f"{tag}: request failed ({item.get('error')})")
            continue
        fl = item.get("fast_lane")
        slow = (fl == "SLOW")
        if slow != expect_slow:
            failures.append(
                f"{tag}: expected slow={expect_slow}, got fast_lane={fl!r} "
                f"(source={item.get('fast_lane_source')}, reason={item.get('fast_lane_reason')})"
            )
        else:
            passes.append(
                f"{tag}: fast_lane={fl} source={item.get('fast_lane_source')} "
                f"reason={item.get('fast_lane_reason')} latency={item.get('latency')}s"
            )
    # E2
    fl_cfg = (results.get("config") or {}).get("fast_lane") or {}
    if fl_cfg.get("enabled") and fl_cfg.get("route_to_profile"):
        target = (r.get("ordinary") or {}).get("routed_profile")
        if target != "11-fast-answers":
            failures.append(
                "E2: PWA fast request not routed to 11-fast-answers "
                f"(routed_profile={target!r}); fast-lane profile routing "
                "(Track M) is implemented only in api_server.py, so PWA fast "
                "requests execute under 01-orchestrator (full lane)."
            )
        else:
            passes.append("E2: PWA fast request routed to 11-fast-answers")
    else:
        passes.append("E2: skipped (fast_lane route_to_profile not enabled)")

    print("=== PASS ===")
    for p in passes:
        print("  [ok]", p)
    print("=== FAIL ===")
    for f_ in failures:
        print("  [FAIL]", f_)
    return 1 if failures else 0


def main() -> int:
    # --correlate-only: re-run log correlation on an existing results file, no
    # new requests. Fast path for debugging the log reader without touching the
    # live gateway again.
    if "--correlate-only" in sys.argv:
        with open(OUT_PATH, encoding="utf-8") as fh:
            existing = json.load(fh)
        correlate(existing.get("probe_ts", ""), existing)
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, ensure_ascii=False, indent=2)
        print("RE-CORRELATED", OUT_PATH, flush=True)
        return check(existing)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    results = {
        "probe_ts": stamp,
        "profile": PROFILE,
        "model": MODEL,
        "base": BASE,
        "gateway_log": GATEWAY_LOG,
        "config": {"fast_lane": {"enabled": True, "route_to_profile": True}},
        "results": {},
    }
    if not TOKEN:
        print("No YADISK_BRIDGE_INGRESS_TOKEN in env; cannot run live probe.", file=sys.stderr)
        return 2
    for tag, tmpl, _expect_slow in REQUESTS:
        text = tmpl.format(stamp=stamp)
        print(f"--- {tag} sending @ {datetime.datetime.now().isoformat()}", flush=True)
        r = send_turn(text)
        r["text_sent"] = text
        results["results"][tag] = r
        print(
            json.dumps(
                {k: r.get(k) for k in ("ok", "http", "latency", "error")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        time.sleep(2)
    correlate(stamp, results)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print("WROTE", OUT_PATH, flush=True)
    if "--check" in sys.argv:
        return check(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
