"""Live-registry matrix for the terminal gate — the acceptance numbers, reproducibly.

Run OUTSIDE pytest (the test guard forbids test-time reads of the real home; this is an operator
probe):

    HERMES_HOME=/home/hermes/.hermes bash py.sh verify_live_matrix.py

It drives the SAME entry points the socket gate uses (``_ticket_principal_may_open`` /
``_host_terminal_profile_matches``) plus the REST/mint gate (``_require_profile_for_principal``) and
prints the resolved subject beside every verdict, so the key is never hidden.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermes_acl_bindings as hb  # noqa: E402
import hermes_acl_profiles as prof_acl  # noqa: E402

NAMES = ["01-maria-safronova", "01-orchestrator", "01-ksenia-kozlovskaya", "16-personal",
         "03-software-engineer"]
CASES = [
    ("maria(local,slug)", {"user_id": "person/maria.safronova", "provider": "local"}),
    ("maria(basic,login)", {"user_id": "maria.safronova", "provider": "basic"}),
    ("maria(telegram)", {"user_id": "543447681", "provider": "telegram"}),
    ("owner(local,uid)", {"user_id": "uid:1001", "provider": "local"}),
    ("owner(session-token)", {"user_id": "legacy-token-owner", "provider": "session-token"}),
    ("anonymous", {}),
]


def _verdicts(identity: dict) -> dict[str, bool]:
    """The verdict of the REAL terminal gate for each profile name."""
    from hermes_cli.web_routers import webapp as wa

    out = {}
    for name in NAMES:
        ws = SimpleNamespace(query_params={"profile": name}, _hermes_auth_identity=identity) \
            if identity else SimpleNamespace(query_params={"profile": name})
        out[name] = wa._ticket_principal_may_open(ws, name)
    return out


def main() -> int:
    reg = hb.load_cached()
    print(f"registry: {reg.path if hasattr(reg, 'path') else '(live)'}")
    print(f"persons : {len(reg.persons) if hasattr(reg, 'persons') else '?'}\n")

    ok = True
    expect = {
        "maria(local,slug)": {"01-maria-safronova": True, "03-software-engineer": True,
                              "01-orchestrator": False, "01-ksenia-kozlovskaya": False,
                              "16-personal": False},
        "maria(basic,login)": {"01-maria-safronova": True, "03-software-engineer": True,
                               "01-orchestrator": False, "01-ksenia-kozlovskaya": False,
                               "16-personal": False},
        "maria(telegram)": {"01-maria-safronova": True, "03-software-engineer": True,
                            "01-orchestrator": False, "01-ksenia-kozlovskaya": False,
                            "16-personal": False},
        "owner(local,uid)": dict.fromkeys(NAMES, True),
        "owner(session-token)": dict.fromkeys(NAMES, True),
        "anonymous": dict.fromkeys(NAMES, False),
    }
    for label, identity in CASES:
        from hermes_acl_scope import request_identity_subject

        req = SimpleNamespace(state=SimpleNamespace(
            session=SimpleNamespace(user_id=identity.get("user_id", ""),
                                    provider=identity.get("provider", ""), issuer="")
            if identity else None, native_http_principal=None))
        subject = request_identity_subject(req) if identity else None
        got = _verdicts(identity)
        want = expect[label]
        bad = {k: v for k, v in got.items() if v != want[k]}
        ok = ok and not bad
        print(f"== {label}  subject={subject!r}  registry_person={prof_acl.registry_slug(subject)!r}")
        print(f"   gate: {got}")
        if bad:
            print(f"   MISMATCH vs acceptance: {bad}")
        print()

    print("-- REST/mint gate (_require_profile_for_principal) --")
    from hermes_cli.web_routers.webapp import _require_profile_for_principal
    from fastapi import HTTPException

    for label, identity, name, want_ok in [
        ("maria -> own", {"user_id": "person/maria.safronova", "provider": "local"},
         "01-maria-safronova", True),
        ("maria -> shared", {"user_id": "person/maria.safronova", "provider": "local"},
         "03-software-engineer", True),
        ("maria -> ksenia", {"user_id": "person/maria.safronova", "provider": "local"},
         "01-ksenia-kozlovskaya", False),
        ("maria -> orchestrator", {"user_id": "person/maria.safronova", "provider": "local"},
         "01-orchestrator", False),
        ("owner -> 16-personal", {"user_id": "uid:1001", "provider": "local"},
         "16-personal", True),
    ]:
        req = SimpleNamespace(state=SimpleNamespace(session=None, native_http_principal=None))
        try:
            _require_profile_for_principal(req, identity, name)
            allowed = True
        except HTTPException:
            allowed = False
        ok = ok and allowed == want_ok
        print(f"   {label:24s} {'ALLOWED' if allowed else 'REFUSED'}"
              f"{'' if allowed == want_ok else '   <-- MISMATCH'}")

    print("\nRESULT:", "all acceptance rows match" if ok else "MISMATCHES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
