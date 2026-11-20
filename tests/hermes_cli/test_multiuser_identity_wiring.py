"""Step 3 end to end: a login names its own person, and a revocation reaches the device.

The card's acceptance criteria live here, at the seams a real sign-in actually crosses:

* ``plugins/dashboard_auth/basic`` — many accounts, one password each, and a minted ticket that
  carries the person's generation;
* ``gateway.owner_identity.principal_subject`` — the ONE place a signed-in surface becomes a
  subject, so two accounts must not collapse into the owner (П4);
* ``hermes_cli.dashboard_auth.ws_tickets`` — the 30-second socket credential, which must die with
  the generation too;
* ``hermes_cli.dashboard_auth.middleware`` — the gate's refusal, which must say "revoked" and not
  "expired";
* ``hermes auth users revoke|remove`` — the operator's two verbs.

No sleeps, no wall clock: lifetimes are asserted on the numbers the tokens carry.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

import hermes_acl_identity as hi
from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli import dashboard_users as du
from hermes_cli.dashboard_auth import InvalidCredentialsError, RefreshExpiredError
from plugins.dashboard_auth import basic as basic_plugin

SECRET = b"m" * 32


def _write_users(path: Path, records: list[du.UserRecord]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.to_line() + "\n" for r in records), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _record(login: str, password: str, **kwargs) -> du.UserRecord:
    kwargs.setdefault("display_name", login)
    return du.UserRecord(login=login, password_hash=du.hash_password(password), **kwargs)


@pytest.fixture
def users_path(tmp_path: Path) -> Path:
    return tmp_path / "users" / "users.jsonl"


@pytest.fixture
def fleet(users_path: Path) -> Path:
    """25 accounts: the owner plus 24 users, each with its own password."""
    records = [_record("yury", "ownerpw", role="owner")]
    records += [_record(f"user{i:02d}", f"pw{i:02d}") for i in range(1, 25)]
    return _write_users(users_path, records)


@pytest.fixture
def provider(fleet: Path, monkeypatch):
    """A provider over the test fleet, with the identity module reading the SAME file."""
    store = du.UserStore(fleet)
    monkeypatch.setattr(hi, "users_store", lambda: store)
    monkeypatch.setattr(hi, "multi_user_enabled", lambda: True)
    return basic_plugin.BasicAuthProvider(
        username="", password_hash="", secret=SECRET, users_file=str(fleet))


@pytest.fixture(autouse=True)
def _clean_tickets():
    ws_tickets._reset_for_tests()
    yield
    ws_tickets._reset_for_tests()


def _login(provider, login: str, password: str):
    return provider.complete_password_login(username=login, password=password)


# ---------------------------------------------------------------------------
# criterion 1/2: 25 accounts, each with its own password, each its OWN subject
# ---------------------------------------------------------------------------

class TestManyLoginsOnePersonUntilNow:
    def test_each_of_the_25_logins_authenticates_with_its_own_password(self, provider):
        assert _login(provider, "yury", "ownerpw").user_id == "yury"
        for i in range(1, 25):
            session = _login(provider, f"user{i:02d}", f"pw{i:02d}")
            assert session.user_id == f"user{i:02d}"

    def test_one_accounts_password_does_not_open_another(self, provider):
        with pytest.raises(InvalidCredentialsError):
            _login(provider, "user01", "pw02")

    def test_the_owner_password_does_not_open_a_user_account(self, provider):
        with pytest.raises(InvalidCredentialsError):
            _login(provider, "user01", "ownerpw")

    def test_two_accounts_yield_two_different_subjects(self, provider):
        from gateway.owner_identity import principal_subject

        first = _login(provider, "user01", "pw01")
        second = _login(provider, "user02", "pw02")
        subject_a = principal_subject({"user_id": first.user_id, "provider": first.provider})
        subject_b = principal_subject({"user_id": second.user_id, "provider": second.provider})
        assert subject_a == "person/user01"
        assert subject_b == "person/user02"
        assert subject_a != subject_b

    def test_no_user_login_becomes_the_owner(self, provider):
        from gateway.owner_identity import host_owner_subject, principal_subject

        subjects = set()
        for i in range(1, 25):
            session = _login(provider, f"user{i:02d}", f"pw{i:02d}")
            subjects.add(principal_subject({"user_id": session.user_id,
                                            "provider": session.provider}))
        assert len(subjects) == 24
        assert host_owner_subject() not in subjects

    def test_the_owner_keeps_the_host_owner_subject(self, provider):
        from gateway.owner_identity import host_owner_subject, principal_subject

        session = _login(provider, "yury", "ownerpw")
        assert principal_subject({"user_id": session.user_id,
                                  "provider": session.provider}) == host_owner_subject()

    def test_an_alias_signs_in_as_the_canonical_person(self, users_path, monkeypatch):
        _write_users(users_path, [_record("anna", "annapw", aliases=("a.old",))])
        store = du.UserStore(users_path)
        monkeypatch.setattr(hi, "users_store", lambda: store)
        monkeypatch.setattr(hi, "multi_user_enabled", lambda: True)
        provider = basic_plugin.BasicAuthProvider(
            username="", password_hash="", secret=SECRET, users_file=str(users_path))
        from gateway.owner_identity import principal_subject

        session = _login(provider, "a.old", "annapw")
        assert session.user_id == "anna"
        assert principal_subject({"user_id": session.user_id,
                                  "provider": session.provider}) == "person/anna"


# ---------------------------------------------------------------------------
# criterion 2/3: the ticket carries the generation, and a bump throws the device out
# ---------------------------------------------------------------------------

class TestTicketCarriesTheGeneration:
    def test_a_fresh_session_reports_the_records_generation(self, provider, fleet):
        store = du.UserStore(fleet)
        session = _login(provider, "user01", "pw01")
        assert session.credential_generation == store.get("user01").gen

    def test_the_owner_session_carries_the_owners_generation(self, provider, fleet):
        store = du.UserStore(fleet)
        assert _login(provider, "yury", "ownerpw").credential_generation == store.get("yury").gen

    def test_the_generation_is_in_the_signed_token_not_just_the_object(self, provider):
        from hermes_cli.dashboard_auth.ws_tickets import consume_ticket, mint_ticket

        session = _login(provider, "user01", "pw01")
        info = consume_ticket(mint_ticket(user_id=session.user_id, provider=session.provider))
        assert info["gen"] == session.credential_generation
        assert info["src"] is True

    def test_an_unknown_login_mints_no_generation(self, provider):
        info = ws_tickets.consume_ticket(
            ws_tickets.mint_ticket(user_id="admin", provider="basic"))
        assert info.get("gen") is None
        assert info["src"] is False


class TestPasswordChangeThrowsTheDeviceOut:
    def test_the_old_access_token_stops_verifying(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        assert provider.verify_session(access_token=session.access_token) is not None
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(
            replace(store.get("user01"),
                                     password_hash=du.hash_password("newpw"))))
        assert provider.verify_session(access_token=session.access_token) is None

    def test_the_old_refresh_token_stops_rotating(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(store.get("user01")))
        with pytest.raises(RefreshExpiredError):
            provider.refresh_session(refresh_token=session.refresh_token)

    def test_the_new_password_works_and_mints_the_new_generation(self, provider, fleet):
        store = du.UserStore(fleet)
        record = store.get("user01")
        store.upsert_record(store.next_gen(
            replace(record, password_hash=du.hash_password("newpw"))))
        session = _login(provider, "user01", "newpw")
        assert session.credential_generation == store.get("user01").gen
        assert provider.verify_session(access_token=session.access_token) is not None

    def test_the_old_ws_ticket_is_refused_after_the_bump(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        ticket = ws_tickets.mint_ticket(user_id=session.user_id, provider=session.provider)
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(store.get("user01")))
        with pytest.raises(ws_tickets.TicketInvalid):
            ws_tickets.consume_ticket(ticket)

    def test_the_owners_own_sessions_survive_a_users_password_change(self, provider, fleet):
        owner = _login(provider, "yury", "ownerpw")
        victim = _login(provider, "user01", "pw01")
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(store.get("user01")))
        assert provider.verify_session(access_token=owner.access_token) is not None
        assert provider.verify_session(access_token=victim.access_token) is None

    def test_the_owners_sessions_survive_their_own_password_change(self, provider, fleet):
        """A person changing their password is re-signed-in, not locked out of the new session."""
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(store.get("yury")))
        fresh = _login(provider, "yury", "ownerpw")
        assert provider.verify_session(access_token=fresh.access_token) is not None


class TestDisableAndRevokeThrowTheDeviceOut:
    def test_disable_refuses_the_live_access_token(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(replace(
            store.get("user01"), disabled=True)))
        assert provider.verify_session(access_token=session.access_token) is None

    def test_disable_refuses_the_refresh_token(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(replace(
            store.get("user01"), disabled=True)))
        with pytest.raises(RefreshExpiredError):
            provider.refresh_session(refresh_token=session.refresh_token)

    def test_a_disabled_account_cannot_sign_in_again(self, provider, fleet):
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(replace(
            store.get("user01"), disabled=True)))
        with pytest.raises(InvalidCredentialsError):
            _login(provider, "user01", "pw01")

    def test_revoke_bumps_the_generation_without_touching_the_password(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        store = du.UserStore(fleet)
        before = store.get("user01")
        store.upsert_record(store.next_gen(before))
        after = store.get("user01")
        assert after.password_hash == before.password_hash
        assert after.gen > before.gen
        assert provider.verify_session(access_token=session.access_token) is None
        # …and the same password signs them back in.
        assert _login(provider, "user01", "pw01").credential_generation == after.gen

    def test_removing_the_record_kills_the_session(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        store = du.UserStore(fleet)
        assert store.remove_record("user01") is True
        assert provider.verify_session(access_token=session.access_token) is None
        with pytest.raises(InvalidCredentialsError):
            _login(provider, "user01", "pw01")

    def test_removing_one_person_does_not_touch_the_others(self, provider, fleet):
        keep = _login(provider, "user02", "pw02")
        store = du.UserStore(fleet)
        store.remove_record("user01")
        assert provider.verify_session(access_token=keep.access_token) is not None


# ---------------------------------------------------------------------------
# criterion 3: the gate says REVOKED, not "expired"
# ---------------------------------------------------------------------------

class TestGateNamesTheRevocation:
    def test_credential_status_reports_revoked_after_a_bump(self, provider, fleet):
        session = _login(provider, "user01", "pw01")
        assert provider.credential_status(session.access_token) == ("ok", "")
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(store.get("user01")))
        status, detail = provider.credential_status(session.access_token)
        assert status == "revoked"
        assert detail.startswith("refused:")
        assert detail.startswith("refused:generation_superseded")

    def test_credential_status_is_unknown_for_a_foreign_token(self, provider):
        assert provider.credential_status("not-a-token") == ("unknown", "")

    def test_the_middleware_detail_function_sees_the_revocation(self, provider, fleet,
                                                                monkeypatch):
        from hermes_cli.dashboard_auth import middleware

        session = _login(provider, "user01", "pw01")
        store = du.UserStore(fleet)
        store.upsert_record(store.next_gen(store.get("user01")))
        monkeypatch.setattr(middleware, "list_session_providers", lambda: [provider])
        assert middleware._revoked_detail(None, access_token=session.access_token,
                                          provider_hint="basic").startswith("refused:")

    def test_the_middleware_detail_function_is_silent_for_a_healthy_token(self, provider,
                                                                          monkeypatch):
        from hermes_cli.dashboard_auth import middleware

        session = _login(provider, "user01", "pw01")
        monkeypatch.setattr(middleware, "list_session_providers", lambda: [provider])
        assert middleware._revoked_detail(None, access_token=session.access_token,
                                          provider_hint="basic") == ""

    def test_the_middleware_detail_function_is_silent_without_a_token(self, monkeypatch):
        from hermes_cli.dashboard_auth import middleware

        assert middleware._revoked_detail(None, access_token=None, provider_hint=None) == ""
        assert middleware._revoked_detail(None, access_token="", provider_hint=None) == ""


# ---------------------------------------------------------------------------
# lifetimes, end to end
# ---------------------------------------------------------------------------

class TestSessionLifetime:
    def test_multi_user_sessions_live_seven_days(self, provider):
        session = _login(provider, "user01", "pw01")
        remaining = session.expires_at - int(time.time())
        assert 7 * 24 * 3600 - 60 <= remaining <= 7 * 24 * 3600

    def test_an_explicit_ttl_still_wins(self, fleet, monkeypatch):
        monkeypatch.setattr(hi, "users_store", lambda: du.UserStore(fleet))
        monkeypatch.setattr(hi, "multi_user_enabled", lambda: True)
        provider = basic_plugin.BasicAuthProvider(
            username="", password_hash="", secret=SECRET, ttl_seconds=3600,
            users_file=str(fleet))
        remaining = _login(provider, "user01", "pw01").expires_at - int(time.time())
        assert 3600 - 60 <= remaining <= 3600

    def test_flag_off_keeps_the_twelve_hour_default(self, fleet, monkeypatch):
        monkeypatch.setattr(hi, "users_store", lambda: du.UserStore(fleet))
        monkeypatch.setattr(hi, "multi_user_enabled", lambda: False)
        provider = basic_plugin.BasicAuthProvider(
            username="", password_hash="", secret=SECRET, users_file=str(fleet))
        remaining = _login(provider, "user01", "pw01").expires_at - int(time.time())
        assert 12 * 3600 - 60 <= remaining <= 12 * 3600

    def test_an_old_token_without_a_generation_is_refused_for_a_file_user(self, provider,
                                                                          fleet):
        """A ticket minted before this step carries no generation: it must not ride into the new
        mode for a person whose record exists."""
        store = du.UserStore(fleet)
        legacy = basic_plugin._sign(
            {"sub": "user01", "kind": "access", "exp": int(time.time()) + 600, "jti": "old"},
            SECRET)
        assert provider.verify_session(access_token=legacy) is None
        assert store.get("user01") is not None


# ---------------------------------------------------------------------------
# the operator's verbs
# ---------------------------------------------------------------------------

class TestOperatorVerbs:
    def test_revoke_verb_is_registered(self):
        from hermes_cli.subcommands import auth_users as au
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="action")
        au.build_auth_users_parser(sub)
        args = parser.parse_args(["users", "revoke", "user01"])
        assert getattr(args, "func", None) is au.cmd_users_revoke

    def test_remove_verb_is_registered(self):
        from hermes_cli.subcommands import auth_users as au
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="action")
        au.build_auth_users_parser(sub)
        args = parser.parse_args(["users", "remove", "user01", "--yes"])
        assert getattr(args, "func", None) is au.cmd_users_remove
        assert args.yes is True

    def test_revoke_bumps_the_generation_in_the_file(self, fleet, monkeypatch, capsys):
        from hermes_cli.subcommands import auth_users as au

        monkeypatch.setattr(au, "_configured_users_file", lambda: fleet)
        store = du.UserStore(fleet)
        before = store.get("user01").gen
        args = _Args(login="user01")
        assert au.cmd_users_revoke(args) == 0
        out = capsys.readouterr().out
        assert "signed user01 out everywhere" in out
        assert f"gen {before} -> {before + 1}" in out
        assert "scrypt$" not in out

    def test_revoke_of_an_unknown_login_fails(self, fleet, monkeypatch, capsys):
        from hermes_cli.subcommands import auth_users as au

        monkeypatch.setattr(au, "_configured_users_file", lambda: fleet)
        assert au.cmd_users_revoke(_Args(login="nobody")) == 1
        assert "no user with login" in capsys.readouterr().err

    def test_remove_deletes_only_that_record(self, fleet, monkeypatch, capsys):
        from hermes_cli.subcommands import auth_users as au

        monkeypatch.setattr(au, "_configured_users_file", lambda: fleet)
        assert au.cmd_users_remove(_Args(login="user01", yes=True)) == 0
        out = capsys.readouterr().out
        assert "deleted user01" in out
        assert "scrypt$" not in out
        remaining = {r.login for r in du.UserStore(fleet).load(force=True).users}
        assert "user01" not in remaining
        assert "user02" in remaining and "yury" in remaining

    def test_remove_without_confirmation_deletes_nothing(self, fleet, monkeypatch, capsys):
        from hermes_cli.subcommands import auth_users as au

        monkeypatch.setattr(au, "_configured_users_file", lambda: fleet)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("n\n"))
        assert au.cmd_users_remove(_Args(login="user01", yes=False)) == 1
        assert "nothing deleted" in capsys.readouterr().out
        assert du.UserStore(fleet).get("user01") is not None

    def test_remove_with_confirmation_deletes(self, fleet, monkeypatch, capsys):
        from hermes_cli.subcommands import auth_users as au

        monkeypatch.setattr(au, "_configured_users_file", lambda: fleet)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("y\n"))
        assert au.cmd_users_remove(_Args(login="user01", yes=False)) == 0
        assert du.UserStore(fleet).get("user01") is None

    def test_store_remove_record_reports_a_missing_login(self, fleet):
        assert du.UserStore(fleet).remove_record("nobody") is False

    def test_store_remove_record_keeps_the_other_lines_byte_identical(self, fleet):
        before = fleet.read_text(encoding="utf-8").splitlines()
        du.UserStore(fleet).remove_record("user01")
        after = fleet.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before) - 1
        assert [l for l in before if l in after] == after


class _Args:
    """The minimum an ``auth users`` handler reads off its argparse namespace."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
