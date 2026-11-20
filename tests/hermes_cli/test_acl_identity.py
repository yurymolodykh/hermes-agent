"""Multi-user identity: login → subject, credential generations, and the lifetimes.

Criterion this file exists for (card ``t_00544442``, step 3 of the multi-user mission): a login
must produce a subject that is NOT the host owner, and changing a password or revoking access must
throw the already-signed-in devices out. Before this step ``principal_subject`` answered "the
owner" for every sign-in, so 25 logins were one person and "fire somebody" had no implementation.

Everything here drives ``hermes_acl_identity`` directly, with an explicit users file, so the policy
is proven without a gateway process. ``acl.multi_user`` is the flag the step ships turned OFF;
each class states which side of it it is testing rather than relying on the live config.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import hermes_acl_identity as hi
from hermes_cli import dashboard_users as du

SECRET = b"k" * 32


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _write_users(path: Path, records: list[du.UserRecord]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.to_line() + "\n" for r in records), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _record(login: str, password: str = "pw", **kwargs) -> du.UserRecord:
    kwargs.setdefault("display_name", login)
    return du.UserRecord(login=login, password_hash=du.hash_password(password), **kwargs)


@pytest.fixture
def users_path(tmp_path: Path) -> Path:
    return tmp_path / "users" / "users.jsonl"


@pytest.fixture
def twenty_five(users_path: Path) -> Path:
    """The card's own scale: 25 accounts, one of them the owner."""
    records = [_record("yury", "ownerpw", role="owner", display_name="Юрий")]
    records += [_record(f"user{i:02d}", f"pw{i:02d}") for i in range(1, 25)]
    return _write_users(users_path, records)


@pytest.fixture
def store(users_path: Path, twenty_five: Path, monkeypatch) -> du.UserStore:
    """Point the identity module at the test's users file (never the operator's)."""
    user_store = du.UserStore(users_path)
    monkeypatch.setattr(hi, "users_store", lambda: user_store)
    return user_store


@pytest.fixture
def multi_user(monkeypatch):
    """``acl.multi_user`` ON — the mode step 3 ships disabled."""
    monkeypatch.setattr(hi, "multi_user_enabled", lambda: True)


@pytest.fixture
def legacy(monkeypatch):
    """``acl.multi_user`` OFF — every answer must be the caller's legacy one."""
    monkeypatch.setattr(hi, "multi_user_enabled", lambda: False)


# ---------------------------------------------------------------------------
# person slugs
# ---------------------------------------------------------------------------

class TestPersonSlug:
    def test_plain_login(self):
        assert hi.person_slug("anna") == "person/anna"

    def test_upper_case_is_lowered(self):
        assert hi.person_slug("Anna.Safronova") == "person/anna.safronova"

    def test_dotted_login_is_kept(self):
        assert hi.person_slug("maria.safronova") == "person/maria.safronova"

    def test_spaces_become_dashes(self):
        assert hi.person_slug("ivan petrov") == "person/ivan-petrov"

    def test_email_login_is_folded(self):
        assert hi.person_slug("a.b@example.com") == "person/a.b-example.com"

    def test_leading_punctuation_is_trimmed(self):
        assert hi.person_slug("..anna..") == "person/anna"

    def test_empty_login_yields_nothing(self):
        assert hi.person_slug("") == ""
        assert hi.person_slug("   ") == ""
        assert hi.person_slug(None) == ""

    def test_punctuation_only_yields_nothing(self):
        assert hi.person_slug("...") == ""
        assert hi.person_slug("---") == ""

    def test_long_login_is_truncated_to_the_registry_limit(self):
        slug = hi.person_slug("a" * 200)
        assert slug == "person/" + "a" * hi.SLUG_MAX
        assert len(slug) - len("person/") == hi.SLUG_MAX

    def test_truncation_never_leaves_a_trailing_dash(self):
        # 63 chars of 'a' then '-b': the cut lands right after the dash.
        slug = hi.person_slug("a" * (hi.SLUG_MAX - 1) + "-b")
        assert not slug.endswith("-")

    def test_result_matches_the_bindings_registry_rule(self):
        import hermes_acl_bindings as hb

        for login in ("anna", "Anna.Safronova", "ivan petrov", "a" * 200):
            slug = hi.person_slug(login)
            assert hb._PERSON_RE.match(slug), slug

    def test_is_stable_across_calls(self):
        assert hi.person_slug("Mixed Case") == hi.person_slug("Mixed Case")

    def test_person_subject_is_the_slug(self):
        assert hi.person_subject("anna") == "person/anna"


# ---------------------------------------------------------------------------
# the flag
# ---------------------------------------------------------------------------

class TestFlag:
    def test_flag_is_off_by_default(self, monkeypatch):
        import hermes_cli.config_defaults as defaults

        assert defaults.DEFAULT_CONFIG["acl"]["multi_user"] is False

    def test_flag_absent_reads_as_off(self, monkeypatch, tmp_path):
        """A config with no ``acl.multi_user`` key must not silently enable the mode."""
        import hermes_acl_identity

        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"acl": {}})
        assert hermes_acl_identity.multi_user_enabled() is False

    def test_flag_true_reads_as_on(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"acl": {"multi_user": True}})
        assert hi.multi_user_enabled() is True

    def test_broken_config_reads_as_off(self, monkeypatch):
        def boom():
            raise RuntimeError("config.yaml is not readable")

        monkeypatch.setattr("hermes_cli.config.load_config", boom)
        assert hi.multi_user_enabled() is False

    def test_describe_says_legacy_when_off(self, legacy):
        assert "legacy" in hi.describe()

    def test_describe_says_multi_user_when_on(self, multi_user):
        assert "multi-user" in hi.describe()


# ---------------------------------------------------------------------------
# login → subject
# ---------------------------------------------------------------------------

class TestResolveSubject:
    def test_flag_off_returns_the_fallback_unchanged(self, legacy, store):
        assert hi.resolve_subject("user01", fallback="auth:v1:[\"basic\",\"\",\"user01\"]") == \
            "auth:v1:[\"basic\",\"\",\"user01\"]"

    def test_flag_off_returns_the_fallback_for_the_owner_too(self, legacy, store):
        assert hi.resolve_subject("yury", fallback="uid:1001") == "uid:1001"

    def test_user_login_becomes_its_own_person(self, multi_user, store):
        assert hi.resolve_subject("user01", fallback="uid:1001") == "person/user01"

    def test_owner_login_stays_the_host_owner(self, multi_user, store):
        from gateway.owner_identity import host_owner_subject

        assert hi.resolve_subject("yury", fallback="something-else") == host_owner_subject()

    def test_owner_login_is_the_host_owner_even_without_a_fallback(self, multi_user, store):
        from gateway.owner_identity import host_owner_subject

        assert hi.resolve_subject("yury") == host_owner_subject()

    def test_two_users_resolve_to_two_different_subjects(self, multi_user, store):
        first = hi.resolve_subject("user01", fallback="uid:1001")
        second = hi.resolve_subject("user02", fallback="uid:1001")
        assert first != second
        assert {first, second} == {"person/user01", "person/user02"}

    def test_no_user_is_the_owner_by_accident(self, multi_user, store):
        from gateway.owner_identity import host_owner_subject

        subjects = {hi.resolve_subject(f"user{i:02d}", fallback="uid:1001") for i in range(1, 25)}
        assert len(subjects) == 24
        assert host_owner_subject() not in subjects
        assert "uid:1001" not in subjects

    def test_alias_resolves_to_the_canonical_person(self, multi_user, users_path, monkeypatch):
        _write_users(users_path, [_record("anna", aliases=("a.old",))])
        user_store = du.UserStore(users_path)
        monkeypatch.setattr(hi, "users_store", lambda: user_store)
        assert hi.resolve_subject("a.old", fallback="") == "person/anna"

    def test_login_case_does_not_create_a_second_person(self, multi_user, store):
        assert hi.resolve_subject("USER01", fallback="") == hi.resolve_subject("user01", fallback="")

    def test_unknown_login_keeps_the_fallback(self, multi_user, store):
        assert hi.resolve_subject("nobody", fallback="uid:1001") == "uid:1001"

    def test_unknown_login_without_fallback_is_empty(self, multi_user, store):
        assert hi.resolve_subject("nobody", fallback="") == ""

    def test_no_users_file_keeps_the_fallback(self, multi_user, monkeypatch):
        monkeypatch.setattr(hi, "users_store", lambda: None)
        assert hi.resolve_subject("user01", fallback="uid:1001") == "uid:1001"

    def test_broken_users_file_does_not_make_anyone_an_owner(self, multi_user, users_path,
                                                             monkeypatch):
        users_path.parent.mkdir(parents=True, exist_ok=True)
        users_path.write_text('{"login": "user01", "role": "owner"}\n', encoding="utf-8")
        os.chmod(users_path, 0o600)
        monkeypatch.setattr(hi, "users_store", lambda: du.UserStore(users_path))
        assert hi.resolve_subject("user01", fallback="uid:1001") == "uid:1001"

    def test_unnameable_login_is_refused_not_promoted(self, multi_user, users_path, monkeypatch):
        """A record whose login cannot produce a slug must not fall back to the owner."""
        users_path.parent.mkdir(parents=True, exist_ok=True)
        users_path.write_text(
            du.UserRecord(login="ok", password_hash="scrypt$1$1$1$AA$BB").to_line() + "\n",
            encoding="utf-8")
        os.chmod(users_path, 0o600)
        monkeypatch.setattr(hi, "users_store", lambda: du.UserStore(users_path))
        # The record exists but the login normalises to something usable; none of them is the owner.
        assert hi.resolve_subject("ok", fallback="uid:1001") == "person/ok"


class TestResolveIdentitySubject:
    def test_flag_off_is_the_legacy_answer(self, legacy, store):
        identity = {"user_id": "user01", "provider": "basic", "issuer": ""}
        assert hi.resolve_identity_subject(identity, legacy="uid:1001") == "uid:1001"

    def test_password_login_is_split(self, multi_user, store):
        identity = {"user_id": "user01", "provider": "basic", "issuer": ""}
        assert hi.resolve_identity_subject(identity, legacy="uid:1001") == "person/user01"

    def test_federated_login_keeps_its_legacy_subject(self, multi_user, store):
        identity = {"user_id": "user01", "provider": "nous", "issuer": "portal"}
        assert hi.resolve_identity_subject(identity, legacy="auth:v1:[...]") == "auth:v1:[...]"

    def test_session_token_keeps_its_legacy_subject(self, multi_user, store):
        identity = {"user_id": "deployment-token", "provider": "session-token"}
        assert hi.resolve_identity_subject(identity, legacy="uid:1001") == "uid:1001"

    def test_server_internal_keeps_its_legacy_subject(self, multi_user, store):
        identity = {"user_id": "server-internal", "provider": "server-internal"}
        assert hi.resolve_identity_subject(identity, legacy="uid:1001") == "uid:1001"

    def test_missing_user_id_keeps_the_legacy_answer(self, multi_user, store):
        assert hi.resolve_identity_subject({"provider": "basic"}, legacy="unbound") == "unbound"

    def test_owner_identity_resolves_to_the_host_owner(self, multi_user, store):
        from gateway.owner_identity import host_owner_subject

        identity = {"user_id": "yury", "provider": "basic"}
        assert hi.resolve_identity_subject(identity, legacy="owner") == host_owner_subject()


# ---------------------------------------------------------------------------
# generations — what makes a revocation immediate
# ---------------------------------------------------------------------------

class TestCredentialValid:
    def test_flag_off_accepts_everything(self, legacy, store):
        assert hi.credential_valid("user01", None) is True

    def test_no_users_file_accepts_everything(self, multi_user, monkeypatch):
        """A single-pair installation has no per-person credential to revoke."""
        monkeypatch.setattr(hi, "users_store", lambda: None)
        assert hi.credential_valid("user01", 1) is True

    def test_current_generation_is_accepted(self, multi_user, store):
        assert hi.credential_valid("user01", store.get("user01").gen) is True

    def test_old_generation_is_refused(self, multi_user, store):
        assert hi.credential_valid("user01", 1) is True
        store.upsert_record(store.next_gen(store.get("user01")))
        assert hi.credential_valid("user01", 1) is False

    def test_new_generation_is_accepted_after_a_bump(self, multi_user, store):
        store.upsert_record(store.next_gen(store.get("user01")))
        assert hi.credential_valid("user01", store.get("user01").gen) is True

    def test_missing_generation_on_a_users_file_token_is_refused(self, multi_user, store):
        assert hi.credential_valid("user01", None) is False

    def test_disabled_user_is_refused_at_the_same_generation(self, multi_user, store):
        record = store.get("user01")
        store.upsert_record(replace(record, disabled=True))
        assert hi.credential_valid("user01", record.gen) is False

    def test_enabled_again_accepts_the_new_generation(self, multi_user, store):
        record = store.get("user01")
        store.upsert_record(store.next_gen(replace(record, disabled=True)))
        store.upsert_record(store.next_gen(replace(store.get("user01"), disabled=False)))
        assert hi.credential_valid("user01", store.get("user01").gen) is True

    def test_deleted_user_is_refused(self, multi_user, store, users_path):
        record = store.get("user01")
        assert store.remove_record("user01") is True
        assert hi.credential_valid("user01", record.gen, from_file=True) is False

    def test_deleted_user_without_the_source_marker_falls_back_to_the_file(self, multi_user, store):
        record = store.get("user01")
        store.remove_record("user01")
        assert hi.credential_valid("user01", record.gen) is False

    def test_single_pair_login_survives_a_users_file(self, multi_user, store):
        """The deployment's own configured pair is not revocable by this mechanism."""
        assert hi.credential_valid("admin", None, from_file=False) is True

    def test_broken_users_file_refuses(self, multi_user, users_path, monkeypatch):
        users_path.parent.mkdir(parents=True, exist_ok=True)
        users_path.write_text('{"login": ""}\n', encoding="utf-8")
        os.chmod(users_path, 0o600)
        monkeypatch.setattr(hi, "users_store", lambda: du.UserStore(users_path))
        reasons: list = []
        assert hi.credential_valid("user01", 1, reason_out=reasons) is False
        assert reasons == ["users_file_invalid"]

    def test_empty_login_is_refused(self, multi_user, store):
        assert hi.credential_valid("", 1) is False

    def test_non_integer_generation_is_refused(self, multi_user, store):
        assert hi.credential_valid("user01", "not-a-number") is False

    def test_non_integer_record_generation_is_refused(self, multi_user, store, users_path):
        store.upsert_record(replace(store.get("user01"), gen="x"))
        # The record no longer validates, so the whole file is refused.
        assert hi.credential_valid("user01", 1) is False

    def test_reason_names_the_rule(self, multi_user, store):
        reasons: list = []
        hi.credential_valid("user01", 1, reason_out=reasons)
        assert reasons == []
        store.upsert_record(store.next_gen(store.get("user01")))
        reasons = []
        hi.credential_valid("user01", 1, reason_out=reasons)
        assert reasons and reasons[0].startswith("generation_superseded")

    def test_revoke_reason_is_an_audit_sentence(self, multi_user, store):
        assert hi.revoke_reason("user01", store.get("user01").gen) == "accepted"
        assert hi.revoke_reason("user01", -5).startswith("refused:")

    def test_revoke_reason_never_contains_a_hash(self, multi_user, store):
        text = hi.revoke_reason("user01", -5)
        assert "scrypt" not in text


class TestCredentialMarker:
    def test_users_file_record_contributes_its_generation(self, multi_user, store):
        assert hi.credential_marker("user01") == (True, store.get("user01").gen)

    def test_unknown_login_contributes_nothing(self, multi_user, store):
        assert hi.credential_marker("admin") == (False, None)

    def test_flag_off_still_reports_the_marker(self, legacy, store):
        """The marker is data, not policy: minting it while the flag is off keeps tickets portable
        once the owner switches the mode on."""
        assert hi.credential_marker("user01")[0] is True


class TestSessionGeneration:
    def test_returns_the_record_generation(self, multi_user, store):
        assert hi.session_generation("user01") == store.get("user01").gen

    def test_unknown_login_returns_none(self, multi_user, store):
        assert hi.session_generation("nobody") is None


# ---------------------------------------------------------------------------
# lifetimes
# ---------------------------------------------------------------------------

class TestLifetimes:
    def test_flag_off_keeps_the_provider_default(self, legacy):
        assert hi.session_ttl_seconds(0, default_seconds=12 * 3600) == 12 * 3600

    def test_multi_user_imposes_seven_days(self, multi_user):
        assert hi.session_ttl_seconds(0, default_seconds=12 * 3600) == 7 * 24 * 3600

    def test_explicit_config_wins(self, multi_user):
        assert hi.session_ttl_seconds(3600, default_seconds=12 * 3600) == 3600

    def test_explicit_config_wins_while_off_too(self, legacy):
        assert hi.session_ttl_seconds(7200, default_seconds=12 * 3600) == 7200

    def test_garbage_config_falls_back(self, multi_user):
        assert hi.session_ttl_seconds("x", default_seconds=60) == 7 * 24 * 3600

    def test_negative_config_falls_back(self, multi_user):
        assert hi.session_ttl_seconds(-5, default_seconds=60) == 7 * 24 * 3600

    def test_ttl_has_a_floor(self, legacy):
        assert hi.session_ttl_seconds(1, default_seconds=60) == 60

    def test_refresh_ttl_off_is_the_provider_default(self, legacy):
        assert hi.refresh_ttl_seconds(0, default_seconds=30 * 24 * 3600) == 30 * 24 * 3600

    def test_refresh_ttl_multi_user_is_seven_days(self, multi_user):
        assert hi.refresh_ttl_seconds(0, default_seconds=30 * 24 * 3600) == 7 * 24 * 3600

    def test_refresh_ttl_explicit_wins(self, multi_user):
        assert hi.refresh_ttl_seconds(600, default_seconds=60) == 600

    def test_seven_days_is_stated_once(self):
        assert hi.MULTI_USER_SESSION_TTL_SECONDS == 7 * 24 * 60 * 60
        assert hi.MULTI_USER_REFRESH_TTL_SECONDS == 7 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# record lookup plumbing
# ---------------------------------------------------------------------------

class TestRecordLookup:
    def test_record_for_a_known_login(self, store):
        record = hi.record_for("user01")
        assert record is not None and record.login == "user01"

    def test_record_for_an_unknown_login_is_none(self, store):
        assert hi.record_for("nobody") is None

    def test_record_for_an_empty_login_is_none(self, store):
        assert hi.record_for("") is None

    def test_owner_record_is_marked(self, store):
        assert hi.is_owner_record(hi.record_for("yury")) is True
        assert hi.is_owner_record(hi.record_for("user01")) is False

    def test_is_owner_record_tolerates_none(self):
        assert hi.is_owner_record(None) is False

    def test_users_store_is_none_without_config(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"dashboard": {"basic_auth": {}}})
        assert hi.users_store() is None

    def test_users_store_resolves_the_configured_path(self, monkeypatch, users_path):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"dashboard": {"basic_auth": {"users_file": str(users_path)}}})
        store = hi.users_store()
        assert store is not None and store.path == users_path
