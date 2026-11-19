"""Tests for the dashboard users file and multi-user password login.

Covers ``hermes_cli/dashboard_users.py`` (format, permissions, fail-closed parsing, hashing,
merge/import) and the users-file half of ``plugins/dashboard_auth/basic`` (login by login or
alias, the single-pair fallback, the bcrypt/argon2 read-only migration, and the constant-cost
timing contract across known / unknown / disabled / foreign-format logins).

The timing test measures the COUNT of scrypt invocations, not wall-clock: a wall-clock assertion
on a shared runner is a flake generator, and the invariant is "exactly one scrypt work unit on
every path", which counting proves exactly.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from hermes_cli import dashboard_users as du
from hermes_cli.dashboard_auth import InvalidCredentialsError
from plugins.dashboard_auth import basic as basic_plugin
from plugins.dashboard_auth._shared import SkipRegistration


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SECRET = b"k" * 32


def _write_users(path: Path, records: list[du.UserRecord], mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.to_line() + "\n" for r in records), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _provider(users_file: Path | str = "", *, username: str = "admin",
              password: str = "adminpw") -> basic_plugin.BasicAuthProvider:
    return basic_plugin.BasicAuthProvider(
        username=username,
        password_hash=du.hash_password(password) if username else "",
        secret=SECRET,
        users_file=str(users_file),
    )


def _login_ok(provider, login: str, password: str) -> bool:
    try:
        provider.complete_password_login(username=login, password=password)
        return True
    except InvalidCredentialsError:
        return False


@pytest.fixture
def users_path(tmp_path: Path) -> Path:
    return tmp_path / "users" / "users.json"


@pytest.fixture
def two_users(users_path: Path) -> Path:
    return _write_users(users_path, [
        du.UserRecord(login="yury", password_hash=du.hash_password("ownerpw"), role="owner",
                      display_name="Юрий", telegram_id="900"),
        du.UserRecord(login="anna", password_hash=du.hash_password("annapw"), aliases=("a.old",)),
    ])


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------

class TestHashing:
    def test_round_trip_and_shape(self):
        encoded = du.hash_password("hunter2")
        assert encoded.startswith("scrypt$")
        assert du.hash_kind(encoded) == du.HASH_SCRYPT
        assert du.verify_password("hunter2", encoded)
        assert not du.verify_password("hunter3", encoded)

    def test_salt_differs_per_hash(self):
        assert du.hash_password("same") != du.hash_password("same")

    def test_malformed_scrypt_is_a_mismatch_not_a_crash(self):
        for bad in ("scrypt$", "scrypt$a$b$c$d$e", "scrypt$16384$8$1$@@@$@@@", ""):
            assert du.verify_password("x", bad) is False

    def test_hash_kind_detection(self):
        assert du.hash_kind("$2b$12$abc") == du.HASH_BCRYPT
        assert du.hash_kind("$2a$12$abc") == du.HASH_BCRYPT
        assert du.hash_kind("$2y$12$abc") == du.HASH_BCRYPT
        assert du.hash_kind("$argon2id$v=19$m=1,t=1,p=1$abc$def") == du.HASH_ARGON2
        assert du.hash_kind("pbkdf2$sha256$1$abc$def") == du.HASH_UNKNOWN
        assert du.hash_kind("") == du.HASH_UNKNOWN

    def test_needs_rehash_only_for_foreign_formats(self):
        assert du.needs_rehash(du.hash_password("x")) is False
        assert du.needs_rehash("$2b$12$abc") is True
        assert du.needs_rehash("$argon2id$v=19$m=1$abc$def") is True

    def test_bcrypt_verifies(self):
        pytest.importorskip("bcrypt")
        import bcrypt

        encoded = bcrypt.hashpw(b"hunter2", bcrypt.gensalt(rounds=4)).decode()
        assert du.verify_password("hunter2", encoded) is True
        assert du.verify_password("nope", encoded) is False

    def test_argon2_verifies(self):
        pytest.importorskip("argon2")
        from argon2 import PasswordHasher

        encoded = PasswordHasher().hash("hunter2")
        assert du.verify_password("hunter2", encoded) is True
        assert du.verify_password("nope", encoded) is False

    def test_missing_backend_raises_instead_of_denying(self, monkeypatch):
        """An absent bcrypt module must say "cannot check", never "wrong password"."""
        monkeypatch.setitem(sys.modules, "bcrypt", None)
        with pytest.raises(du.UnsupportedHashFormat):
            du.verify_password("x", "$2b$12$abcdefghijklmnopqrstuv")


# ---------------------------------------------------------------------------
# parsing / validation — fail-closed
# ---------------------------------------------------------------------------

class TestParsing:
    def test_line_delimited_json(self):
        text = "\n".join([
            du.UserRecord(login="a", password_hash="scrypt$1$1$1$AA$BB").to_line(),
            "# a comment line is allowed",
            "",
            du.UserRecord(login="b", password_hash="scrypt$1$1$1$AA$BB").to_line(),
        ])
        records, problems, _warnings = du.parse_users_text(text)
        assert problems == []
        assert [r.login for r in records] == ["a", "b"]

    def test_yaml_list_is_accepted(self):
        text = "- login: a\n  password_hash: scrypt$1$1$1$AA$BB\n- login: b\n  password_hash: scrypt$1$1$1$AA$BB\n"
        records, problems, _ = du.parse_users_text(text)
        assert problems == []
        assert [r.login for r in records] == ["a", "b"]

    def test_missing_password_hash_voids_the_whole_file(self):
        text = (du.UserRecord(login="a", password_hash="scrypt$1$1$1$AA$BB").to_line() + "\n"
                + json.dumps({"login": "b"}) + "\n")
        records, problems, _ = du.parse_users_text(text)
        assert records == [], "a partly-valid file must yield NO users (fail closed)"
        assert len(problems) == 1
        assert problems[0].line == 2
        assert problems[0].field == "password_hash"

    def test_missing_login_names_the_field(self):
        _records, problems, _ = du.parse_users_text(json.dumps({"password_hash": "scrypt$x"}) + "\n")
        assert {p.field for p in problems} == {"login"}

    def test_bad_role_names_the_field(self):
        _records, problems, _ = du.parse_users_text(
            json.dumps({"login": "a", "password_hash": "scrypt$x", "role": "admin"}) + "\n")
        assert [p.field for p in problems] == ["role"]

    def test_non_bool_disabled_names_the_field(self):
        _records, problems, _ = du.parse_users_text(
            json.dumps({"login": "a", "password_hash": "scrypt$x", "disabled": "yes"}) + "\n")
        assert [p.field for p in problems] == ["disabled"]

    def test_non_integer_gen_names_the_field(self):
        _records, problems, _ = du.parse_users_text(
            json.dumps({"login": "a", "password_hash": "scrypt$x", "gen": "one"}) + "\n")
        assert [p.field for p in problems] == ["gen"]

    def test_aliases_must_be_a_list(self):
        _records, problems, _ = du.parse_users_text(
            json.dumps({"login": "a", "password_hash": "scrypt$x", "aliases": 3}) + "\n")
        assert [p.field for p in problems] == ["aliases"]

    def test_duplicate_login_across_records_is_refused(self):
        text = (json.dumps({"login": "a", "password_hash": "scrypt$x"}) + "\n"
                + json.dumps({"login": "A", "password_hash": "scrypt$y"}) + "\n")
        records, problems, _ = du.parse_users_text(text)
        assert records == []
        assert any("claimed by both" in p.message for p in problems)

    def test_duplicate_alias_collision_is_refused(self):
        text = (json.dumps({"login": "a", "password_hash": "scrypt$x"}) + "\n"
                + json.dumps({"login": "b", "password_hash": "scrypt$y", "aliases": ["a"]}) + "\n")
        records, problems, _ = du.parse_users_text(text)
        assert records == []
        assert problems

    def test_unparseable_text_is_one_file_level_problem(self):
        records, problems, _ = du.parse_users_text('{"login": "a", "password_has\n')
        assert records == []
        assert problems[0].line == 0
        assert problems[0].field == "<file>"

    def test_unknown_hash_format_is_a_load_warning_not_a_login_surprise(self):
        text = json.dumps({"login": "a", "password_hash": "pbkdf2$sha256$1$x$y"}) + "\n"
        records, problems, warnings = du.parse_users_text(text)
        assert problems == []
        assert len(records) == 1
        assert any("unrecognised password_hash format" in w for w in warnings)
        assert not any("pbkdf2$sha256$1$x$y" in w for w in warnings), "never echo the hash"

    def test_foreign_format_is_a_migration_warning(self):
        _records, _problems, warnings = du.parse_users_text(
            json.dumps({"login": "a", "password_hash": "$2b$12$abcdefghijklmnopqrstuv"}) + "\n")
        assert any("bcrypt" in w and "re-hashed" in w for w in warnings)

    def test_login_with_whitespace_or_separator_is_refused(self):
        for bad in ("a b", "a|b", "a/b", "a\\b"):
            _records, problems, _ = du.parse_users_text(
                json.dumps({"login": bad, "password_hash": "scrypt$x"}) + "\n")
            assert [p.field for p in problems] == ["login"], bad


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------

class TestPermissions:
    def test_0600_is_accepted(self, users_path):
        p = _write_users(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("x"))])
        assert du.load_users_file(p).ok

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o777])
    def test_group_or_world_readable_is_refused(self, users_path, mode):
        p = _write_users(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("x"))], mode=mode)
        with pytest.raises(du.UsersFilePermissionError) as info:
            du.load_users_file(p)
        assert "chmod 600" in str(info.value)

    def test_missing_file_is_empty_not_an_error(self, users_path):
        result = du.load_users_file(users_path)
        assert result.ok
        assert result.users == []
        assert result.exists is False

    def test_provider_registration_refuses_a_loose_file(self, users_path, monkeypatch):
        """Registration is what refuses; the constructor alone must not silently accept it."""
        p = _write_users(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("x"))], mode=0o644)
        monkeypatch.setattr(basic_plugin, "_load_config_basic_auth_section",
                            lambda: {"username": "admin", "password_hash": du.hash_password("pw"),
                                     "users_file": str(p)})
        with pytest.raises(SkipRegistration) as info:
            basic_plugin._settings()
        assert info.value.level == "error"
        assert "chmod 600" in str(info.value)

    def test_register_does_not_register_with_a_loose_file(self, users_path, monkeypatch):
        from unittest.mock import MagicMock

        p = _write_users(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("x"))], mode=0o644)
        monkeypatch.setattr(basic_plugin, "_load_config_basic_auth_section",
                            lambda: {"username": "admin", "password_hash": du.hash_password("pw"),
                                     "users_file": str(p)})
        ctx = MagicMock()
        basic_plugin.register(ctx)
        ctx.register_dashboard_auth_provider.assert_not_called()
        assert "chmod 600" in basic_plugin.LAST_SKIP_REASON


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

class TestMultiUserLogin:
    def test_each_user_signs_in(self, two_users):
        provider = _provider(two_users)
        for login, pw in (("yury", "ownerpw"), ("anna", "annapw")):
            session = provider.complete_password_login(username=login, password=pw)
            assert session.user_id == login

    def test_alias_resolves_to_the_record(self, two_users):
        session = _provider(two_users).complete_password_login(username="a.old", password="annapw")
        assert session.user_id == "anna"

    def test_login_is_case_insensitive_but_only_exact(self, two_users):
        provider = _provider(two_users)
        assert _login_ok(provider, "ANNA", "annapw")
        assert not _login_ok(provider, "ann", "annapw")

    def test_wrong_password_is_refused(self, two_users):
        assert not _login_ok(_provider(two_users), "yury", "annapw")

    def test_disabled_user_cannot_sign_in(self, users_path):
        p = _write_users(users_path, [
            du.UserRecord(login="boris", password_hash=du.hash_password("borispw"), disabled=True)])
        assert not _login_ok(_provider(p), "boris", "borispw")

    def test_single_pair_still_works_beside_the_file(self, two_users):
        provider = _provider(two_users)
        assert _login_ok(provider, "admin", "adminpw")
        assert not _login_ok(provider, "admin", "wrong")

    def test_single_pair_works_with_no_users_file(self, users_path):
        provider = _provider("", username="admin", password="adminpw")
        assert _login_ok(provider, "admin", "adminpw")

    def test_users_file_only_provider_has_no_single_pair(self, users_path):
        _write_users(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("apw"))])
        provider = _provider(users_path, username="")
        assert _login_ok(provider, "a", "apw")
        assert not _login_ok(provider, "admin", "adminpw")

    def test_unknown_login_never_succeeds(self, two_users):
        assert not _login_ok(_provider(two_users), "ghost", "anything")

    def test_unusable_file_fails_closed_for_its_users_only(self, users_path):
        p = users_path.parent / "users.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(du.UserRecord(login="yury", password_hash=du.hash_password("ownerpw")).to_line()
                     + "\n" + '{"login": "broken"}\n')
        os.chmod(p, 0o600)
        provider = _provider(p)
        assert not _login_ok(provider, "yury", "ownerpw"), "a broken file must not authenticate anyone"
        assert _login_ok(provider, "admin", "adminpw"), "the single pair is unaffected"

    def test_store_picks_up_an_out_of_process_edit(self, two_users):
        provider = _provider(two_users)
        assert not _login_ok(provider, "newbie", "newpw")
        with two_users.open("a", encoding="utf-8") as handle:
            handle.write(du.UserRecord(login="newbie", password_hash=du.hash_password("newpw")).to_line() + "\n")
        os.utime(two_users, None)  # ensure a new mtime even on a coarse clock
        assert _login_ok(provider, "newbie", "newpw")


# ---------------------------------------------------------------------------
# foreign-format migration on login
# ---------------------------------------------------------------------------

class TestForeignFormatMigration:
    def _bcrypt_record(self, login: str = "clara", password: str = "clarapw") -> du.UserRecord:
        bcrypt = pytest.importorskip("bcrypt")
        return du.UserRecord(login=login, password_hash=bcrypt.hashpw(
            password.encode(), bcrypt.gensalt(rounds=4)).decode())

    def test_bcrypt_login_succeeds_and_is_rewritten_as_scrypt(self, users_path):
        record = self._bcrypt_record()
        _write_users(users_path, [record])
        provider = _provider(users_path)

        assert provider.users_snapshot().by_login("clara").password_hash.startswith("$2b$")
        assert _login_ok(provider, "clara", "clarapw")

        after = provider.users_snapshot()
        written = after.by_login("clara")
        assert written.password_hash.startswith("scrypt$")
        assert "$2b$" not in users_path.read_text(encoding="utf-8")
        assert du.verify_password("clarapw", written.password_hash)

    def test_argon2_login_succeeds_and_is_rewritten(self, users_path):
        pytest.importorskip("argon2")
        from argon2 import PasswordHasher

        _write_users(users_path, [du.UserRecord(login="dora", password_hash=PasswordHasher().hash("dorapw"))])
        provider = _provider(users_path)
        assert _login_ok(provider, "dora", "dorapw")
        assert provider.users_snapshot().by_login("dora").password_hash.startswith("scrypt$")

    def test_rewrite_preserves_permissions_and_other_lines(self, users_path):
        other = du.UserRecord(login="yury", password_hash=du.hash_password("ownerpw"))
        record = self._bcrypt_record()
        _write_users(users_path, [other, record])
        original_first_line = users_path.read_text(encoding="utf-8").splitlines()[0]

        assert _login_ok(_provider(users_path), "clara", "clarapw")

        assert stat.S_IMODE(users_path.stat().st_mode) == 0o600
        assert users_path.read_text(encoding="utf-8").splitlines()[0] == original_first_line

    def test_wrong_password_on_a_foreign_hash_is_not_rewritten(self, users_path):
        record = self._bcrypt_record()
        _write_users(users_path, [record])
        provider = _provider(users_path)
        assert not _login_ok(provider, "clara", "wrong")
        assert provider.users_snapshot().by_login("clara").password_hash.startswith("$2b$")

    def test_unknown_format_denies_and_warns_once(self, users_path, caplog):
        _write_users(users_path, [du.UserRecord(login="petr", password_hash="pbkdf2$sha256$1$x$y")])
        provider = _provider(users_path)
        with caplog.at_level("WARNING", logger=basic_plugin.logger.name):
            assert not _login_ok(provider, "petr", "whatever")
            assert not _login_ok(provider, "petr", "whatever")
        warnings = [r for r in caplog.records if "unknown password hash format" in r.getMessage()]
        assert len(warnings) == 1, "one line per login, not one per attempt"
        page = "\n".join(r.getMessage() for r in caplog.records)
        assert "pbkdf2$sha256$1$x$y" not in page, "the hash itself is never logged"


# ---------------------------------------------------------------------------
# the timing contract — counted, not wall-clocked
# ---------------------------------------------------------------------------

class TestConstantCost:
    """Every path spends exactly ONE scrypt work unit (verify or re-hash alike).

    Counted at ``hashlib.scrypt`` itself — the primitive both ``verify_scrypt`` and
    ``hash_password`` funnel through — so the count cannot be fooled by which wrapper a path uses.
    """

    @pytest.fixture
    def counted(self, monkeypatch):
        calls = {"scrypt": 0}
        real_scrypt = du.hashlib.scrypt

        def counting_scrypt(*args, **kwargs):
            calls["scrypt"] += 1
            return real_scrypt(*args, **kwargs)

        monkeypatch.setattr(du.hashlib, "scrypt", counting_scrypt)
        return calls

    def test_known_login_costs_one(self, two_users, counted):
        checkout = _provider(two_users)
        counted["scrypt"] = 0
        assert _login_ok(checkout, "yury", "ownerpw")
        assert counted["scrypt"] == 1

    def test_unknown_looking_known_login_costs_one(self, two_users, counted):
        checkout = _provider(two_users)
        counted["scrypt"] = 0
        assert not _login_ok(checkout, "yury", "wrong")
        assert counted["scrypt"] == 1

    def test_unknown_login_costs_one(self, two_users, counted):
        checkout = _provider(two_users)
        counted["scrypt"] = 0
        assert not _login_ok(checkout, "ghost", "whatever")
        assert counted["scrypt"] == 1

    def test_disabled_login_costs_one(self, users_path, counted):
        p = _write_users(users_path, [du.UserRecord(
            login="boris", password_hash=du.hash_password("borispw"), disabled=True)])
        checkout = _provider(p)
        counted["scrypt"] = 0
        assert not _login_ok(checkout, "boris", "borispw")
        assert counted["scrypt"] == 1

    def test_single_pair_unknown_user_costs_one(self, users_path, counted):
        checkout = _provider("")
        counted["scrypt"] = 0
        assert not _login_ok(checkout, "nobody", "x")
        assert counted["scrypt"] == 1

    def test_users_file_only_unknown_login_costs_one(self, users_path, counted):
        _write_users(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("apw"))])
        checkout = _provider(users_path, username="")
        counted["scrypt"] = 0
        assert not _login_ok(checkout, "nobody", "x")
        assert counted["scrypt"] == 1

    def test_bcrypt_success_costs_one(self, users_path, counted):
        bcrypt = pytest.importorskip("bcrypt")
        p = _write_users(users_path, [du.UserRecord(
            login="clara", password_hash=bcrypt.hashpw(b"clarapw", bcrypt.gensalt(rounds=4)).decode())])
        checkout = _provider(p)
        counted["scrypt"] = 0
        assert _login_ok(checkout, "clara", "clarapw")
        assert counted["scrypt"] == 1, "the rewrite IS the one scrypt unit"

    def test_bcrypt_failure_costs_one(self, users_path, counted):
        bcrypt = pytest.importorskip("bcrypt")
        p = _write_users(users_path, [du.UserRecord(
            login="clara", password_hash=bcrypt.hashpw(b"clarapw", bcrypt.gensalt(rounds=4)).decode())])
        checkout = _provider(p)
        counted["scrypt"] = 0
        assert not _login_ok(checkout, "clara", "wrong")
        assert counted["scrypt"] == 1

    def test_unknown_format_costs_one(self, users_path, counted):
        p = _write_users(users_path, [du.UserRecord(login="petr", password_hash="pbkdf2$x")])
        checkout = _provider(p)
        counted["scrypt"] = 0
        assert not _login_ok(checkout, "petr", "whatever")
        assert counted["scrypt"] == 1


# ---------------------------------------------------------------------------
# merge / import
# ---------------------------------------------------------------------------

HEADER = "login|display_name|password_hash|telegram_id|role|created_at|email"


class TestImport:
    def test_parse_export_skips_a_header(self):
        rows, problems = du.parse_export_text(HEADER + "\na|A|scrypt$x|1|user|2026-01-01|\n")
        assert problems == []
        assert rows[0]["login"] == "a"

    def test_parse_export_without_header_keeps_a_user_named_login(self):
        rows, problems = du.parse_export_text("login|Login Person|scrypt$x|1|user|2026-01-01|\n")
        assert problems == []
        assert rows[0]["login"] == "login"

    def test_short_line_is_a_problem(self):
        _rows, problems = du.parse_export_text("a|A\n")
        assert problems and problems[0].field == "line"

    def test_five_rows_one_duplicate_gives_four_added_one_merged(self):
        """The card's acceptance case: 5 export rows, one pair is the same person."""
        rows, _ = du.parse_export_text("\n".join([
            HEADER,
            "yury.molodykh|Юрий|scrypt$x|900|owner|2026-01-05|yury@example.org",
            "yury|Юрий (old)|scrypt$y|900|owner|2026-01-06|yury@example.org",
            "anna.k|Анна|scrypt$z|901|user|2026-02-01|anna@example.org",
            "petr|Пётр|pbkdf2$bad|902|user|2026-02-02|",
            "olga|Ольга|scrypt$w||user|2026-03-01|olga@example.org",
        ]) + "\n")
        records, report = du.merge_import([], rows)
        assert (len(report.added), len(report.merged), len(report.conflicts)) == (4, 1, 0)
        assert len(records) == 4
        assert report.unknown_hash == ["petr"]
        merged = next(r for r in records if r.login == "yury.molodykh")
        assert "yury" in merged.aliases, "the old login survives as an alias"

    def test_merge_by_telegram_id_not_login_similarity(self):
        existing = [du.UserRecord(login="yury", password_hash=du.hash_password("y"), telegram_id="900")]
        rows = [{"login": "y.molodykh", "password_hash": "$2b$x", "telegram_id": "900",
                 "role": "owner", "display_name": "Юрий"}]
        records, report = du.merge_import(existing, rows)
        assert report.merged and not report.added
        assert len(records) == 1
        assert records[0].login == "yury"
        assert "y.molodykh" in records[0].aliases

    def test_similar_logins_are_not_merged(self):
        existing = [du.UserRecord(login="yury", password_hash=du.hash_password("y"), telegram_id="900")]
        rows = [{"login": "yuri", "password_hash": "scrypt$x", "telegram_id": "901", "role": "user"}]
        records, report = du.merge_import(existing, rows)
        assert report.added == ["yuri"] and not report.merged
        assert len(records) == 2

    def test_merge_by_email(self):
        existing = [du.UserRecord(login="anna", password_hash=du.hash_password("a"),
                                  email="anna@example.org")]
        rows = [{"login": "a.kuznetsova", "password_hash": "scrypt$x", "email": "Anna@Example.org",
                 "role": "user"}]
        _records, report = du.merge_import(existing, rows)
        assert report.merged and not report.added

    def test_same_login_different_identity_is_a_conflict(self):
        existing = [du.UserRecord(login="yury", password_hash=du.hash_password("y"), telegram_id="900")]
        rows = [{"login": "yury", "password_hash": "scrypt$x", "telegram_id": "999", "role": "owner"}]
        records, report = du.merge_import(existing, rows)
        assert report.conflicts and report.conflicts[0][0] == "yury"
        assert records[0].telegram_id == "900", "a conflict leaves the existing record untouched"

    def test_existing_hash_is_kept_without_overwrite(self):
        existing = [du.UserRecord(login="yury", password_hash=du.hash_password("y"), telegram_id="900")]
        rows = [{"login": "yury", "password_hash": "scrypt$DIFFERENT", "telegram_id": "900"}]
        records, report = du.merge_import(existing, rows)
        assert report.skipped == ["yury"]
        assert du.verify_password("y", records[0].password_hash)

    def test_overwrite_replaces_the_hash(self):
        existing = [du.UserRecord(login="yury", password_hash=du.hash_password("y"), telegram_id="900")]
        rows = [{"login": "yury", "password_hash": du.hash_password("new"), "telegram_id": "900"}]
        records, _report = du.merge_import(existing, rows, overwrite=True)
        assert du.verify_password("new", records[0].password_hash)

    def test_report_names_logins_but_never_hashes(self):
        rows, _ = du.parse_export_text("\n".join([
            HEADER, "petr|Пётр|pbkdf2$secretish|902|user|2026-02-02|"]) + "\n")
        _records, report = du.merge_import([], rows)
        rendered = report.render()
        assert "petr" in rendered
        assert "pbkdf2$secretish" not in rendered


# ---------------------------------------------------------------------------
# store writes
# ---------------------------------------------------------------------------

class TestStoreWrites:
    def test_write_is_atomic_0600_and_round_trips(self, users_path):
        store = du.UserStore(users_path)
        store.write_records([du.UserRecord(login="a", password_hash=du.hash_password("x"))])
        assert stat.S_IMODE(users_path.stat().st_mode) == 0o600
        assert [r.login for r in store.load().users] == ["a"]

    def test_replace_record_keeps_sibling_lines_byte_identical(self, users_path):
        first = du.UserRecord(login="a", password_hash=du.hash_password("x"))
        second = du.UserRecord(login="b", password_hash=du.hash_password("y"))
        _write_users(users_path, [first, second])
        store = du.UserStore(users_path)
        store.replace_record(du.UserRecord(login="b", password_hash=du.hash_password("z")))
        assert users_path.read_text(encoding="utf-8").splitlines()[0] == first.to_line()

    def test_next_gen_increments(self):
        store = du.UserStore(Path("/nonexistent"))
        record = du.UserRecord(login="a", password_hash="scrypt$x", gen=4)
        assert store.next_gen(record).gen == 5

    def test_upsert_appends_a_new_record(self, users_path):
        _write_users(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("x"))])
        store = du.UserStore(users_path)
        store.upsert_record(du.UserRecord(login="b", password_hash=du.hash_password("y")))
        assert sorted(r.login for r in store.load().users) == ["a", "b"]

    def test_render_list_shows_no_hash(self, two_users):
        text = du.render_list(du.load_users_file(two_users).users)
        assert "scrypt$" not in text
        assert "yury" in text and "owner" in text and "a.old" in text

    def test_empty_record_list_renders_plainly(self):
        assert du.render_list([]) == "no users"
