"""Probe: what subject does the live gate compute, and what does the registry know?"""
import json, os, sys
from types import SimpleNamespace

os.environ.setdefault("HERMES_HOME", "/home/hermes/.hermes")
sys.path.insert(0, "/home/hermes/.hermes/kanban/workspaces/t_65b29e55")

import hermes_acl_bindings as hb
import hermes_acl_identity as idn
from gateway.owner_identity import principal_subject
from hermes_acl_scope import owner_stamps, request_identity_subject
from hermes_acl_profiles import request_allows, allows

print("multi_user =", idn.multi_user_enabled())
reg = hb.load_cached()
print("registry persons =", len(reg.persons) if reg else None)

IDENTITIES = {
    "maria(local,slug)": {"user_id": "person/maria.safronova", "provider": "local"},
    "maria(basic,login)": {"user_id": "maria.safronova", "provider": "basic"},
    "owner(local,uid)": {"user_id": "uid:1001", "provider": "local"},
    "owner(session-token)": {"user_id": "legacy-token-owner", "provider": "session-token"},
    "anon": {},
    "maria(tg)": {"user_id": "543447681", "provider": "telegram"},
}
NAMES = ["01-maria-safronova", "01-orchestrator", "01-ksenia-kozlovskaya", "16-personal",
         "03-software-engineer"]

def current_gate(identity, profile):
    """Exactly what webapp._ticket_principal_may_open does today."""
    if not identity.get("user_id"):
        return False
    req = SimpleNamespace(state=SimpleNamespace(
        session=SimpleNamespace(user_id=identity.get("user_id", ""),
                                provider=identity.get("provider", ""),
                                issuer=identity.get("issuer", "") or ""),
        native_http_principal=None))
    return request_allows(req, profile)

for label, ident in IDENTITIES.items():
    subj = principal_subject(ident) if ident.get("user_id") else ""
    stamps = owner_stamps(subj) if subj else []
    pofs = {s: (reg.person_of(s) if reg else None) for s in stamps}
    print(f"\n== {label} {ident}")
    print("  principal_subject :", subj)
    print("  request_identity_subject:", request_identity_subject(SimpleNamespace(
        state=SimpleNamespace(session=SimpleNamespace(
            user_id=ident.get("user_id", ""), provider=ident.get("provider", ""), issuer=""),
            native_http_principal=None))) if ident.get("user_id") else None)
    print("  owner_stamps      :", stamps)
    print("  person_of(stamps) :", {k: v for k, v in pofs.items() if v})
    print("  gate now          :", {n: current_gate(ident, n) for n in NAMES})
    known = [pofs[s] for s in stamps if pofs[s]]
    if known:
        print("  allows(normalized):", {n: allows(known[0], n) for n in NAMES}, "subject=", known[0])
