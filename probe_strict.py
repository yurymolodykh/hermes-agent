"""Probe 5: no widening — unknown/foreign principals gain nothing; REST path fixed too."""
import os, sys
from types import SimpleNamespace

os.environ.setdefault("HERMES_HOME", "/home/hermes/.hermes")
sys.path.insert(0, "/home/hermes/.hermes/kanban/workspaces/t_65b29e55")

import hermes_acl_profiles as P

NAMES = ["01-maria-safronova", "01-ksenia-kozlovskaya", "16-personal", "03-software-engineer",
         "01-orchestrator"]

CASES = [
    ("unknown tg stranger", {"user_id": "999999999", "provider": "telegram"}),
    ("unknown login", {"user_id": "nobody.at.all", "provider": "basic"}),
    ("ksenia (own record)", {"user_id": "person/ksenia.kozlovskaya", "provider": "local"}),
    ("ksenia(tg)", {"user_id": "320318064", "provider": "telegram"}),
    ("prefixed person/yury2 spoof", {"user_id": "person/yury2", "provider": "local"}),
    ("empty user_id", {"user_id": "", "provider": "local"}),
    ("probe: raw auth subject of maria", {"user_id": 'auth:v1:["local","","person/maria.safronova"]',
                                          "provider": "local"}),
]
for label, ident in CASES:
    req = SimpleNamespace(state=SimpleNamespace(
        session=SimpleNamespace(user_id=ident["user_id"], provider=ident["provider"], issuer=""),
        native_http_principal=None))
    try:
        subj = P.request_subject(req)
    except Exception as exc:
        subj = f"<{type(exc).__name__}: {exc}>"
    print(f"{label:32s} subject={subj!r:52s} slug={P.registry_slug(subj)!r}")
    print(f"{'':32s} allows={ {n: P.request_allows(req, n) for n in NAMES} }")

print("\n-- REST/mint gate (_require_profile_for_principal) --")
from hermes_cli.web_routers.webapp import _require_profile_for_principal
from fastapi import HTTPException
for label, ident, name in [
    ("maria(local,slug)->own", {"user_id": "person/maria.safronova", "provider": "local"}, "01-maria-safronova"),
    ("maria(local,slug)->shared", {"user_id": "person/maria.safronova", "provider": "local"}, "03-software-engineer"),
    ("maria(local,slug)->ksenia", {"user_id": "person/maria.safronova", "provider": "local"}, "01-ksenia-kozlovskaya"),
    ("maria(local,slug)->orchestrator", {"user_id": "person/maria.safronova", "provider": "local"}, "01-orchestrator"),
    ("owner->16-personal", {"user_id": "uid:1001", "provider": "local"}, "16-personal"),
]:
    req = SimpleNamespace(state=SimpleNamespace(session=None, native_http_principal=None))
    try:
        _require_profile_for_principal(req, ident, name)
        print(f"  {label:34s} ALLOWED")
    except HTTPException as exc:
        print(f"  {label:34s} REFUSED {exc.status_code}")
