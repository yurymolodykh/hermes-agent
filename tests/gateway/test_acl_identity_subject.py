"""Step 3 at the gateway's ONE subject seam: a password login is a person, not the owner.

``gateway.owner_identity.principal_subject`` is where a signed-in surface becomes the subject every
owner comparison and every history scope uses (П4 in the mission's gap analysis). Its single-user
rule — "any login is the owner" — is what made 25 accounts one human. These tests pin both sides:
the rule is preserved verbatim while ``acl.multi_user`` is off, and a password login that the users
file knows becomes ``person/<login>`` while it is on, WITHOUT re-labelling the credentials whose
rows are already stamped (federated logins, the loopback session token, native grants).
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import hermes_acl_identity as hi
import hermes_acl_scope
from gateway import owner_identity, session_identity
from hermes_cli import dashboard_users as du

OWNER = "uid:1001"


@pytest.fixture
def users_path(tmp_path: Path) -> Path:
    return tmp_path / "users.jsonl"


@pytest.fixture
def fleet(users_path: Path, monkeypatch):
    records = [du.UserRecord(login="yury", password_hash=du.hash_password("pw"), role="owner")]
    records += [du.UserRecord(login=f"user{i:02d}", password_hash=du.hash_password("pw"))
                for i in range(1, 4)]
    users_path.write_text("".join(r.to_line() + "\n" for r in records), encoding="utf-8")
    os.chmod(users_path, 0o600)
    store = du.UserStore(users_path)
    monkeypatch.setattr(hi, "users_store", lambda: store)
    return store


@pytest.fixture
def multi_user(fleet, monkeypatch):
    monkeypatch.setattr(hi, "multi_user_enabled", lambda: True)


@pytest.fixture
def legacy(fleet, monkeypatch):
    monkeypatch.setattr(hi, "multi_user_enabled", lambda: False)


def _login(login: str, provider: str = "basic") -> dict:
    return {"user_id": login, "provider": provider, "issuer": ""}


class TestLegacyRuleIsPreserved:
    def test_flag_off_any_password_login_is_still_the_owner(self, legacy):
        assert owner_identity.principal_subject(_login("user01")) == \
            owner_identity.host_owner_subject()

    def test_flag_off_matches_the_pre_step3_answer_for_every_credential(self, legacy):
        for identity in (_login("user01"), _login("user01", "nous"),
                         {"user_id": "deployment-token", "provider": "session-token"},
                         {"user_id": "local", "provider": "local"}):
            assert owner_identity.principal_subject(identity) == \
                owner_identity._legacy_principal_subject(
                    identity, session_identity.authenticated_subject(identity))

    def test_an_identity_without_a_user_id_stays_unbound(self, legacy):
        """The pre-step-3 answer, verbatim: ``unbound`` is not a person and not the owner."""
        assert owner_identity.principal_subject({"provider": "basic"}) == \
            session_identity.authenticated_subject({"provider": "basic"})
        assert "unbound" in owner_identity.principal_subject({"provider": "basic"})


class TestMultiUserSplitsLogins:
    def test_a_user_login_is_its_own_person(self, multi_user):
        assert owner_identity.principal_subject(_login("user01")) == "person/user01"

    def test_the_owner_login_is_the_host_owner(self, multi_user):
        assert owner_identity.principal_subject(_login("yury")) == \
            owner_identity.host_owner_subject()

    def test_an_unknown_login_keeps_the_legacy_answer(self, multi_user):
        assert owner_identity.principal_subject(_login("stranger")) == \
            owner_identity.host_owner_subject()

    def test_a_federated_login_keeps_its_legacy_answer(self, multi_user):
        identity = {"user_id": "user01", "provider": "nous", "issuer": "portal"}
        assert owner_identity.principal_subject(identity) == \
            owner_identity._legacy_principal_subject(
                identity, session_identity.authenticated_subject(identity))

    def test_the_loopback_session_token_keeps_its_legacy_answer(self, multi_user):
        identity = {"user_id": "deployment-token", "provider": "session-token"}
        assert owner_identity.principal_subject(identity) == \
            owner_identity._legacy_principal_subject(
                identity, session_identity.authenticated_subject(identity))

    def test_native_grants_keep_their_subject(self, multi_user):
        identity = {"user_id": "local", "provider": "local"}
        assert owner_identity.principal_subject(identity) == \
            session_identity.authenticated_subject(identity)


class TestOwnerScopeFollowsTheLogin:
    def test_a_user_login_scopes_to_its_person_stamp_only(self, multi_user, monkeypatch):
        monkeypatch.setattr(hermes_acl_scope, "_binding_stamps", lambda identity: [])
        stamps = hermes_acl_scope.owner_stamps("user01")
        assert "person/user01" in stamps
        assert OWNER not in stamps
        assert "cron-owner" not in stamps

    def test_flag_off_adds_no_person_stamp(self, legacy, monkeypatch):
        monkeypatch.setattr(hermes_acl_scope, "_binding_stamps", lambda identity: [])
        stamps = hermes_acl_scope.owner_stamps("user01")
        assert stamps == ["user01"]

    def test_the_owner_login_still_reaches_the_owner_stamps(self, multi_user, monkeypatch):
        monkeypatch.setattr(hermes_acl_scope, "_binding_stamps", lambda identity: [])
        stamps = hermes_acl_scope.owner_stamps("yury")
        assert OWNER in stamps

    def test_an_auth_subject_keeps_its_derived_stamps(self, multi_user, monkeypatch):
        monkeypatch.setattr(hermes_acl_scope, "_binding_stamps", lambda identity: [])
        stamps = hermes_acl_scope.owner_stamps('auth:v1:["basic","","user01"]')
        assert "basic:user01" in stamps
        assert "user01" in stamps

    def test_bindings_are_consulted_for_each_derived_stamp(self, multi_user, monkeypatch):
        """A person bound under their login must also be found through its auth subject."""
        seen: list[str] = []

        def fake(identity: str) -> list[str]:
            seen.append(identity)
            return [OWNER] if identity == "person/user01" else []

        monkeypatch.setattr(hermes_acl_scope, "_binding_stamps", fake)
        stamps = hermes_acl_scope.owner_stamps("user01")
        assert OWNER in stamps
        assert "person/user01" in seen

    def test_no_identity_yields_no_stamps(self, multi_user):
        assert hermes_acl_scope.owner_stamps("") == []
        assert hermes_acl_scope.owner_stamps(None) == []


class TestGenerationOnTheIdentityPath:
    def test_a_minted_marker_uses_the_record_generation(self, multi_user, fleet):
        from_file, generation = hi.credential_marker("user01")
        assert from_file is True
        assert generation == fleet.get("user01").gen

    def test_a_bump_invalidates_the_person_but_not_the_owner(self, multi_user, fleet):
        fleet.upsert_record(fleet.next_gen(fleet.get("user01")))
        assert hi.credential_valid("user01", 1, from_file=True) is False
        assert hi.credential_valid("yury", fleet.get("yury").gen, from_file=True) is True
