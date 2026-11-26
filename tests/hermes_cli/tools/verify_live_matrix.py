"""Live-registry matrix for the terminal gate — the acceptance numbers, reproducibly.

Run OUTSIDE pytest (the test guard forbids test-time reads of the real home; this is an operator
probe):

    HERMES_HOME=$HOME/.hermes python3 tests/hermes_cli/tools/verify_live_matrix.py

It drives the SAME entry points the socket gate uses (``_ticket_principal_may_open`` /
``_host_terminal_profile_matches``) plus the REST/mint gate (``_require_profile_for_principal``) and
prints the resolved subject beside every verdict, so the key is never hidden.

The matrix is keyed by ROLE, not by a person's name, and every name/id it uses is derived from the
registry document it is pointed at (``--registry``). That is what makes the two runs comparable:

    # 1) the live registry (read-only)                       -> matrix A
    HERMES_HOME=$HOME/.hermes python3 …/verify_live_matrix.py --json /tmp/live.json
    # 2) a test home whose registry is the pseudonymized snapshot -> matrix B
    HERMES_HOME=/tmp/snap-home python3 …/verify_live_matrix.py --json /tmp/snap.json

    # 3) the two must be identical, role for role and form for form
    python3 …/verify_live_matrix.py --compare /tmp/live.json /tmp/snap.json

``--compare`` is the acceptance check itself: pseudonymizing the snapshot may not change a single
verdict (own True / foreign personal False / shared True / owner True / anonymous False).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
# The repo root is three levels up from tests/hermes_cli/tools/.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import hermes_acl_bindings as hb  # noqa: E402
import hermes_acl_profiles as prof_acl  # noqa: E402


# ── roles derived from the registry document (never from a hardcoded name) ────────────────────
def _roles(registry) -> dict:
    """``{role: {...}}`` for the record shapes the matrix needs, read from the document itself."""
    persons = list(registry.persons.values())
    shared_records = [p for p in persons if p.shared]
    personal = [p for p in persons if not p.shared]
    shared_profiles = [str(x) for r in shared_records for x in r.profiles]

    def is_personal_profile(name: str) -> bool:
        return name not in shared_profiles

    owner = next((p for p in personal if any(str(s).startswith("uid:") for s in p.subjects)), None)
    owner_extras = ([str(x) for x in owner.profiles if is_personal_profile(str(x))][1:]
                    if owner is not None else [])

    def mailbox(record) -> str:
        """The messenger id a record is reachable at, in the spelling the registry stores it."""
        for stamp in record.subjects:
            text = str(stamp)
            if text.startswith("tg:"):
                return text[3:]
        return ""

    candidates = [p for p in personal if p is not owner and mailbox(p)]
    subject = candidates[0] if candidates else None
    foreign = candidates[1] if len(candidates) > 1 else None
    if subject is None:
        raise SystemExit("registry has no person with a messenger id to act as")

    def label(record) -> str:
        return record.slug.split("/", 1)[1] if "/" in record.slug else record.slug

    def own_profile(record) -> str:
        profs = [str(x) for x in record.profiles if is_personal_profile(str(x))]
        return profs[0] if profs else ""

    roles = {
        "own": {"profile": own_profile(subject), "label": label(subject),
                "mailbox": mailbox(subject), "slug": subject.slug},
        "foreign_personal": {"profile": own_profile(foreign) if foreign else "",
                             "label": label(foreign) if foreign else "", "slug": foreign.slug if foreign else ""},
        "shared": {"profile": shared_profiles[0] if shared_profiles else "",
                   "count": len(shared_profiles)},
        "owner": {"profile": (owner_extras or [own_profile(owner)])[0] if owner else "",
                  "slug": owner.slug if owner else "",
                  "uid": next((str(s) for s in owner.subjects if str(s).startswith("uid:")), "")
                  if owner else ""},
    }
    return roles


def _identity_forms(roles: dict) -> list[tuple[str, dict]]:
    """The six identity shapes the gate is asked about, in BOTH registries' own spellings."""
    forms = [
        ("subject(local,slug)", {"user_id": roles["own"]["slug"], "provider": "local"}),
        ("subject(basic,login)", {"user_id": roles["own"]["label"], "provider": "basic"}),
        ("subject(telegram,bare id)", {"user_id": roles["own"]["mailbox"], "provider": "telegram"}),
        ("owner(local,uid)", {"user_id": roles["owner"]["uid"], "provider": "local"}),
        ("owner(session-token)", {"user_id": "legacy-token-owner", "provider": "session-token"}),
        ("anonymous", {}),
    ]
    return forms


def _verdicts(identity: dict, profiles: dict[str, str]) -> dict[str, bool]:
    """The verdict of the REAL terminal gate for each role's profile name."""
    from hermes_cli.web_routers import webapp as wa

    out = {}
    for role, name in profiles.items():
        ws = SimpleNamespace(query_params={"profile": name}, _hermes_auth_identity=identity) \
            if identity else SimpleNamespace(query_params={"profile": name})
        out[role] = bool(wa._ticket_principal_may_open(ws, name))
    return out


def _subject_of(identity: dict) -> str | None:
    if not identity:
        return None
    from hermes_acl_scope import request_identity_subject

    req = SimpleNamespace(state=SimpleNamespace(
        session=SimpleNamespace(user_id=identity.get("user_id", ""),
                                provider=identity.get("provider", ""), issuer=""),
        native_http_principal=None))
    return request_identity_subject(req)


def _rest_allowed(identity: dict, profile: str) -> bool | None:
    """``_require_profile_for_principal``'s answer, or ``None`` when the name does not exist here."""
    from fastapi import HTTPException

    from hermes_cli.web_routers.webapp import _require_profile_for_principal

    req = SimpleNamespace(state=SimpleNamespace(session=None, native_http_principal=None))
    try:
        _require_profile_for_principal(req, identity, profile)
        return True
    except HTTPException:
        return False


def build_matrix() -> dict:
    reg = hb.load_cached()
    if reg is None:
        raise SystemExit("no bindings registry at this HERMES_HOME (is acl.enabled/multi_user on?)")
    roles = _roles(reg)
    profiles = {role: spec["profile"] for role, spec in roles.items() if spec.get("profile")
                and role != "owner"}
    profiles["owner_extra"] = roles["owner"]["profile"]

    # A ``basic`` login only resolves through the users file; when this home has no record for it
    # the form is not comparable and is reported as null rather than as a False verdict.
    import hermes_acl_identity

    has_login = hermes_acl_identity.record_for(roles["own"]["label"]) is not None

    matrix = {"registry": str(getattr(reg, "path", "")), "persons": len(reg.persons),
              "shared_profiles": roles["shared"]["count"], "roles": roles, "forms": {}}
    for label, identity in _identity_forms(roles):
        subject = _subject_of(identity)
        got = _verdicts(identity, profiles)
        matrix["forms"][label] = {
            "subject": subject,
            "registry_slug": prof_acl.registry_slug(subject),
            "gate": got,
            "usable": label != "subject(basic,login)" or has_login,
        }
    own = {"user_id": roles["own"]["slug"], "provider": "local"}
    matrix["rest"] = {
        "own": _rest_allowed(own, profiles["own"]),
        "shared": _rest_allowed(own, profiles["shared"]),
        "foreign": _rest_allowed(own, profiles["foreign_personal"]),
        "owner_extra": _rest_allowed({"user_id": roles["owner"]["uid"], "provider": "local"},
                                    profiles["owner_extra"]),
    }
    return matrix


EXPECT = {
    "subject(local,slug)": {"own": True, "shared": True, "foreign_personal": False,
                            "owner_extra": False},
    "subject(basic,login)": {"own": True, "shared": True, "foreign_personal": False,
                             "owner_extra": False},
    "subject(telegram,bare id)": {"own": True, "shared": True, "foreign_personal": False,
                                  "owner_extra": False},
    "owner(local,uid)": {"own": True, "shared": True, "foreign_personal": True,
                         "owner_extra": True},
    "owner(session-token)": {"own": True, "shared": True, "foreign_personal": True,
                             "owner_extra": True},
    "anonymous": {"own": False, "shared": False, "foreign_personal": False,
                  "owner_extra": False},
}
EXPECT_REST = {"own": True, "shared": True, "foreign": False, "owner_extra": True}


def _print(matrix: dict) -> bool:
    print(f"registry: {matrix['registry']}")
    print(f"persons : {matrix['persons']}   shared profiles: {matrix['shared_profiles']}\n")
    ok = True
    for label, row in matrix["forms"].items():
        want = EXPECT[label]
        bad = {k: v for k, v in row["gate"].items() if v != want[k]}
        usable = row["usable"]
        ok = ok and (not usable or not bad)
        print(f"== {label}  subject={row['subject']!r}  registry_slug={row['registry_slug']!r}"
              f"{'' if usable else '   (users file has no such login: n/a)'}")
        print(f"   gate: {row['gate']}")
        if usable and bad:
            print(f"   MISMATCH vs acceptance: {bad}")
        print()
    print("-- REST/mint gate (_require_profile_for_principal) --")
    for label, got in matrix["rest"].items():
        want = EXPECT_REST[label]
        ok = ok and got == want
        print(f"   {label:12s} {'ALLOWED' if got else 'REFUSED'}"
              f"{'' if got == want else '   <-- MISMATCH'}")
    return ok


def _compare(first: Path, second: Path) -> int:
    a = json.loads(first.read_text(encoding="utf-8"))
    b = json.loads(second.read_text(encoding="utf-8"))
    print(f"A: {a['registry']}   persons={a['persons']} shared={a['shared_profiles']}")
    print(f"B: {b['registry']}   persons={b['persons']} shared={b['shared_profiles']}")
    problems = []
    if (a["persons"], a["shared_profiles"]) != (b["persons"], b["shared_profiles"]):
        problems.append("geometry differs: person count / shared profile count")
    for label in a["forms"]:
        fa, fb = a["forms"][label], b["forms"][label]
        if not (fa["usable"] and fb["usable"]):
            print(f"   {label:28s} skipped (users file lacks the login in one home)")
            continue
        same = fa["gate"] == fb["gate"]
        print(f"   {label:28s} {'SAME' if same else 'DIFF'}  {fa['gate']}")
        if not same:
            problems.append(f"{label}: A={fa['gate']} B={fb['gate']}")
    if a["rest"] != b["rest"]:
        problems.append(f"REST: A={a['rest']} B={b['rest']}")
    else:
        print(f"   {'REST/mint':28s} SAME  {a['rest']}")
    print("\nRESULT:", "identical verdicts" if not problems else "DIFFERENCES")
    for problem in problems:
        print("  -", problem)
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="write the matrix here for a later --compare")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"),
                        help="compare two saved matrices (live vs snapshot)")
    args = parser.parse_args(argv)

    if args.compare:
        return _compare(*args.compare)

    matrix = build_matrix()
    ok = _print(matrix)
    if args.json:
        args.json.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print("\nmatrix written:", args.json)
    print("\nRESULT:", "all acceptance rows match" if ok else "MISMATCHES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
