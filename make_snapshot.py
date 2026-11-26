"""Build a shape-identical, pseudonymized snapshot of the LIVE bindings registry for tests.

The live registry holds real Telegram ids (PII) and must not enter git, while the automated test
may not read the real home either (``tests/home_io_guard.py``). This snapshot keeps the DOCUMENT
SHAPE that produced the defect — slugs ``person/<login>``, ``uid:`` / ``cron-owner`` owner stamps,
one shared record carrying the ``0N-`` fleet — and replaces every ``tg:`` id with a distinct
placeholder, so no test ever asserts on a real person's messenger id.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
SRC = HOME / "acl" / "profile-bindings.json"
DST = Path(__file__).resolve().parent / "tests" / "hermes_cli" / "data_profile_bindings_snapshot.json"


def main() -> int:
    raw = SRC.read_bytes()
    doc = json.loads(raw)
    persons, n = [], 0
    for person in doc.get("persons") or []:
        subjects = []
        for stamp in person.get("subjects") or []:
            text = str(stamp)
            if text.startswith("tg:") or text.isdigit():  # messenger ids are PII in any spelling
                n += 1
                subjects.append(f"tg:{900000000 + n}" if text.startswith("tg:")
                                else str(900000000 + n))
            else:  # uid:/cron-owner: owner semantics, not PII — kept verbatim
                subjects.append(stamp)
        persons.append({
            "slug": person["slug"],
            "display_name": person.get("display_name") or person["slug"],
            "shared": bool(person.get("shared")),
            "profiles": list(person.get("profiles") or []),
            "subjects": subjects,
            "owner": person.get("owner") or person["slug"],
            "source": person.get("source") or "admin",
        })
    out = {"schema": doc["schema"], "written_at": "2026-01-01T00:00:00+00:00",
           "written_by": "admin", "persons": persons}
    DST.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("live sha256:", hashlib.sha256(raw).hexdigest())
    print("persons:", len(persons), "tg pseudonymized:", n)
    for p in persons:
        if p["slug"] in ("person/maria.safronova", "person/ksenia.kozlovskaya", "person/yury",
                         "shared/service"):
            print(" ", p["slug"], p["profiles"], p["subjects"], "shared=", p["shared"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
