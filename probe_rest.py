"""Probe 2: does the SAME broken subject key close the REST/mint path too?"""
import os, sys
from types import SimpleNamespace

os.environ.setdefault("HERMES_HOME", "/home/hermes/.hermes")
sys.path.insert(0, "/home/hermes/.hermes/kanban/workspaces/t_65b29e55")

import hermes_acl_profiles as prof_acl
from hermes_cli.web_routers.webapp import _require_profile_for_principal
from fastapi import HTTPException

MARIA_IDENT = {"user_id": "person/maria.safronova", "provider": "local"}
LOGIN_IDENT = {"user_id": "maria.safronova", "provider": "basic"}


def req(identity):
    return SimpleNamespace(state=SimpleNamespace(session=None, native_http_principal=None))


# The mint route: issue_socket_ticket(profile=...) -> _require_profile_for_principal
for label, ident, name in [
    ("maria(local,slug) -> own", MARIA_IDENT, "01-maria-safronova"),
    ("maria(local,slug) -> shared", MARIA_IDENT, "03-software-engineer"),
    ("maria(login) -> own", LOGIN_IDENT, "01-maria-safronova"),
    ("maria(local,slug) -> foreign", MARIA_IDENT, "01-orchestrator"),
]:
    try:
        _require_profile_for_principal(req(ident), ident, name)
        print(f"{label:35s} ALLOWED")
    except HTTPException as exc:
        print(f"{label:35s} REFUSED {exc.status_code} {exc.detail}")

# The REST route shape: request.state.session is the real login (the auth middleware sets it)
print("\n-- via request.state.session (REST ?profile= path) --")
for label, uid, prov in [("maria as person/ slug + local", "person/maria.safronova", "local"),
                         ("maria as login + basic", "maria.safronova", "basic")]:
    request = SimpleNamespace(state=SimpleNamespace(
        session=SimpleNamespace(user_id=uid, provider=prov, issuer=""),
        native_http_principal=None))
    print(f"{label:35s} request_allows(own)={prof_acl.request_allows(request, '01-maria-safronova')}"
          f" shared={prof_acl.request_allows(request, '03-software-engineer')}"
          f" foreign={prof_acl.request_allows(request, '01-orchestrator')}")
