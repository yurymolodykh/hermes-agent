"""Temporary probe (card t_77e5de22): is the owner's answer the same before the fix?

Not a permanent test — deleted after the RED/GREEN receipt is captured.
"""
from __future__ import annotations

from tests.hermes_cli.test_session_default_profile_scope import (  # noqa: F401 — fixtures
    OWNER_LOGIN, _client, _ids, _isolated_home, homes, multi_user, registry)


def test_probe_owner_baseline(homes, multi_user, registry, monkeypatch):
    """With the pre-fix default (profile passed through), what does the owner see?"""
    from hermes_cli.web_routers import sessions as sessions_router

    monkeypatch.setattr(sessions_router, "session_profile", lambda request, profile: profile)
    with _client(as_login=OWNER_LOGIN) as cli:
        ids, body = _ids(cli, "/api/sessions?limit=50")
    print("PROBE owner ids (pre-fix default):", sorted(ids), "profiles:", {r["profile"] for r in body["sessions"]})
    assert ids == {"root-owner-1", "root-alice-1"}


def test_probe_alice_baseline(homes, multi_user, registry, monkeypatch):
    """Pre-fix default for a person: the ROOT store, row scope applied."""
    from hermes_cli.web_routers import sessions as sessions_router

    monkeypatch.setattr(sessions_router, "session_profile", lambda request, profile: profile)
    with _client(as_login="alice") as cli:
        ids, body = _ids(cli, "/api/sessions?limit=50")
    print("PROBE alice ids (pre-fix default):", sorted(ids), "profiles:", {r["profile"] for r in body["sessions"]})
