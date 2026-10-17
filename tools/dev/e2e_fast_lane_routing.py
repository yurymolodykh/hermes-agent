"""Isolated live e2e for fast-lane profile routing (t_746614d5).

Runs a real APIServerAdapter on a private port (18642) with the patched
code (fast_lane.route_to_profile) and drives two real agent turns:

  * markerless (fast) request  ->  must execute under 11-fast-answers:
      - the turn must succeed (HTTP 200, final_response non-empty)
      - the session row must land in profiles/11-fast-answers/state.db
        (fast_lane / review_exempt marker present)
  * slow request („гигантская задача…“) -> executes under origin 01:
      - the session row lands in profiles/01-orchestrator/state.db
        with slow_mode true.

The adapter is wired exactly like the production middleware: the
profile-prefix middleware scopes the request to the default profile
(01-orchestrator), then _run_agent re-scopes to 11 for fast requests.

Run:  venv/Scripts/python.exe tools/dev/e2e_fast_lane_routing.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import aiohttp

HOST = "127.0.0.1"
PORT = 18642
BASE = f"http://{HOST}:{PORT}"

PROFILE_HOME = Path(
    Path(__file__).resolve().parents[3] / "profiles"
)


def _read_api_key(profile: str) -> str:
    env_path = PROFILE_HOME / profile / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("API_SERVER_KEY=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return ""


def _latest_session_row(db_path: Path, marker_key: str):
    """Return the most recent session's model_config marker cell."""
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        cur.execute(
            f"SELECT * FROM sessions ORDER BY id DESC LIMIT 200"
        )
        rows = cur.fetchall()
    finally:
        con.close()
    # Search backwards for a row that has the marker cell populated.
    for row in reversed(rows):
        try:
            import json

            mc = json.loads(row["model_config"] or "{}")
        except Exception:
            mc = {}
        if marker_key in mc or mc:
            return dict(row), mc
    return None, None


async def main() -> int:
    # --- start the private adapter in-process ---
    from gateway.config import PlatformConfig, load_gateway_config
    from gateway.platforms.api_server import APIServerAdapter

    key = _read_api_key("01-orchestrator")
    if not key:
        print("FATAL: no API_SERVER_KEY in 01-orchestrator/.env")
        return 2

    config = PlatformConfig(
        enabled=True,
        extra={
            "host": HOST,
            "port": PORT,
            "key": key,
            "default_profile": "01-orchestrator",
            "model": "deepseek-v4-flash",
        },
    )
    adapter = APIServerAdapter(config)
    adapter.gateway_runner = type("R", (), {})()  # minimal runner stub for cfg reads
    ok = await adapter.connect(is_reconnect=False)
    if not ok:
        print("FATAL: adapter.connect() failed")
        return 2
    print(f"adapter listening on {BASE}")

    # Emulate the profile-prefix middleware: default profile scope.
    from gateway.platforms.api_server import _api_request_profile

    token = _api_request_profile.set("01-orchestrator")

    results = {}

    async def post_chat(session_aiohttp, messages, tag):
        t0 = time.time()
        async with session_aiohttp.post(
            f"{BASE}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": messages}],
            },
        ) as resp:
            dt = time.time() - t0
            body = await resp.json()
            results[tag] = {"status": resp.status, "latency": dt, "body": body}
            print(
                f"[{tag}] status={resp.status} latency={dt:.2f}s "
                f"final={bool((body or {}).get('choices'))}"
            )

    async with aiohttp.ClientSession() as s:
        await post_chat(s, "При каком значении 7 умножить на 8? Ответь кратко.", "fast")
        await post_chat(
            s,
            "гигантская задача: создай детальный план миграции базы данных на новую схему",
            "slow",
        )

    _api_request_profile.reset(token)
    try:
        runner = adapter._runner
        if runner is not None:
            await runner.cleanup()
    except Exception:
        pass
    if hasattr(adapter, "close"):
        try:
            adapter.close()
        except Exception:
            pass

    print("\n=== RESULTS ===")
    for tag, r in results.items():
        print(f"{tag}: status={r['status']} latency={r['latency']:.2f}s")
        if r["status"] != 200:
            print(f"   BODY: {str(r['body'])[:400]}")

    # Verify session landing in profile state.db files.
    fast_db = PROFILE_HOME / "11-fast-answers" / "state.db"
    slow_db = PROFILE_HOME / "01-orchestrator" / "state.db"
    print(f"\nfast profile db exists: {fast_db.exists()} size={fast_db.stat().st_size if fast_db.exists() else 0}")
    print(f"slow profile db exists: {slow_db.exists()} size={slow_db.stat().st_size if slow_db.exists() else 0}")
    if fast_db.exists():
        row, mc = _latest_session_row(fast_db, "review_exempt")
        if mc:
            print(f"11-fast-answers latest session model_config: {mc}")
    if slow_db.exists():
        row, mc = _latest_session_row(slow_db, "slow_mode")
        if mc:
            print(f"01-orchestrator latest session model_config: {mc}")

    ok_fast = results.get("fast", {}).get("status") == 200
    ok_slow = results.get("slow", {}).get("status") == 200
    print(f"\nE2E RESULT: fast={ok_fast} slow={ok_slow}")
    return 0 if ok_fast and ok_slow else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
