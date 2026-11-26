"""Build a shape-identical, FULLY pseudonymized snapshot of the LIVE bindings registry for tests.

The live registry is personal data: real logins (``person/<login>``), real display names and real
Telegram ids. None of it may enter git, while the automated test may not read the real home either
(``tests/home_io_guard.py``). This snapshot therefore keeps only the DOCUMENT SHAPE that produced
the defect and replaces every identity-bearing value:

* ``slug``           ``person/<login>``        -> ``person/user01``…``person/user22`` (live ordinal)
* ``display_name``   ``Алексей Туманов``       -> ``Сотрудник 01``…``Сотрудник 22``
* ``subjects``       ``tg:<real id>`` / bare digits -> ``tg:900000001``… / ``900000020``
* personal profiles  ``01-<login>``            -> ``01-user01``…``01-user22``

DELIBERATELY KEPT, because they carry the geometry the test must keep proving:

* the record count (23), the single shared record and its 13 fleet profiles (``02-``…``15-``),
  a personal profile for every person, and the owner record's extra ``16-personal`` profile;
* the owner's non-PII stamps ``uid:<n>`` and ``cron-owner`` — they are the OWNER semantics the gate
  compares against (``gateway.owner_identity``), not a person's name;
* the ``shared`` / ``source`` / ``owner`` field relationships.

Run from the repo root:

    HERMES_HOME=/home/hermes/.hermes python tests/hermes_cli/tools/make_snapshot.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
SRC = HOME / "acl" / "profile-bindings.json"
DST = Path(__file__).resolve().parents[1] / "data_profile_bindings_snapshot.json"

#: ``01-<login>`` — a person's own profile root, named after the person on disk.
_PERSONAL_PROFILE_RE = re.compile(r"^01-(?!user\d+$).+")


def main() -> int:
    raw = SRC.read_bytes()
    doc = json.loads(raw)
    persons, n_person, n_tg = [], 0, 0
    for person in doc.get("persons") or []:
        subjects = []
        for stamp in person.get("subjects") or []:
            text = str(stamp)
            if text.startswith("tg:") or text.isdigit():
                # Messenger ids are PII in either spelling (``tg:123`` / bare ``123``).
                n_tg += 1
                subjects.append(f"tg:{900000000 + n_tg}" if text.startswith("tg:")
                                else str(900000000 + n_tg))
            else:
                # ``uid:<n>`` / ``cron-owner`` are owner SEMANTICS, not a name: the gate compares
                # them verbatim, so renaming them would change what the snapshot proves.
                subjects.append(stamp)
        if person.get("shared"):
            slug = person["slug"]
            display = person.get("display_name") or ""
            owner = person.get("owner") or slug
            profiles = [str(p) for p in person.get("profiles") or []]
        else:
            n_person += 1
            label = f"user{n_person:02d}"
            slug = f"person/{label}"
            display = f"Сотрудник {n_person:02d}"
            owner = f"person/{label}"
            profiles = [f"01-{label}" if _PERSONAL_PROFILE_RE.match(str(p)) else str(p)
                        for p in person.get("profiles") or []]
        persons.append({
            "slug": slug,
            "display_name": display,
            "shared": bool(person.get("shared")),
            "profiles": profiles,
            "subjects": subjects,
            "owner": owner,
            "source": person.get("source") or "admin",
        })
    out = {"schema": doc["schema"], "written_at": "2026-01-01T00:00:00+00:00",
           "written_by": "admin", "persons": persons}
    DST.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("live sha256:", hashlib.sha256(raw).hexdigest())
    print("persons:", len(persons), "personal records:", n_person, "tg pseudonymized:", n_tg,
          "shared profiles:", sum(len(p["profiles"]) for p in persons if p["shared"]))
    print("snapshot:", DST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
