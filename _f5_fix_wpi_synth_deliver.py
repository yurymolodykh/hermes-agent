"""F5 fix: re-bind wpi-synthesize delivery to the origin-token form.

Preflight (enabled ~2026-10-09) rejects `deliver=api_server:owui-...` for
agent jobs because api_server is not in _KNOWN_DELIVERY_PLATFORMS.  The
origin-token form `deliver="origin,telegram:125805788"` + origin block
bound to the OWUI chat is preflight-exempt and resolves at runtime to the
same api_server chat + telegram lane (native T2 dual-lane, same as the
proven group-ai-centaur-report-watcher precedent).
"""
import json
import os
import sys

os.environ["HERMES_HOME"] = r"C:/Users/hermes/AppData/Local/hermes/profiles/01-orchestrator"
sys.path.insert(0, r"C:/Users/hermes/AppData/Local/hermes/hermes-agent")

from cron.jobs import load_jobs, update_job

CHAT = "owui-f76faefd-be76-4781-93f7-b3f047cda4bb"
OWNER = "9ae3eca0-d6e9-45d5-b4af-414afe4616b9"

before = [j for j in load_jobs() if j.get("id") == "9c1b7bd1dd7d"][0]
print("BEFORE deliver:", before.get("deliver"), "| origin:", before.get("origin"))

updates = {
    "deliver": "origin,telegram:125805788",
    "origin": {
        "platform": "api_server",
        "chat_id": CHAT,
        "session_id": CHAT,
        "user_id": OWNER,
        "owner_id": OWNER,
        "thread_id": None,
    },
}
updated = update_job("9c1b7bd1dd7d", updates)
print("UPDATE returned:", "ok" if updated else "NONE")
after = [j for j in load_jobs() if j.get("id") == "9c1b7bd1dd7d"][0]
print("AFTER deliver:", after.get("deliver"))
print("AFTER origin:", json.dumps(after.get("origin"), ensure_ascii=False))
