"""Tests for ``hermes auth users`` — the CLI surface over the users file.

The store's own behaviour is covered by ``test_dashboard_users.py``; these tests pin the CLI
contract: no hash or password ever reaches stdout/stderr, ``add``/``passwd`` never accept a
password on argv, ``passwd``/``disable``/``enable`` raise ``gen``, and ``import`` reports
added/merged/skipped/conflicts without echoing hash material.
"""

from __future__ import annotations

import json
import os
import stat
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import dashboard_users as du
from hermes_cli.subcommands import auth_users


@pytest.fixture
def users_path(tmp_path: Path, monkeypatch) -> Path:
    """A configured users file, wired through the module's config resolver."""
    path = tmp_path / "users" / "users.json"
    monkeypatch.setattr(auth_users, "_configured_users_file", lambda: path)
    return path


def _seed(path: Path, records: list[du.UserRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.to_line() + "\n" for r in records), encoding="utf-8")
    os.chmod(path, 0o600)


def _args(**kwargs) -> Namespace:
    base = {"password_stdin": True, "json": False}
    base.update(kwargs)
    return Namespace(**base)


def _add(monkeypatch, capsys, login: str, password: str, **kwargs) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(password + "\n"))
    rc = auth_users.cmd_users_add(_args(login=login, display_name=kwargs.get("display_name", ""),
                                        role=kwargs.get("role", "user"),
                                        aliases=kwargs.get("aliases", []),
                                        telegram_id=kwargs.get("telegram_id", ""),
                                        email=kwargs.get("email", "")))
    out, err = capsys.readouterr()
    return rc, out, err


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestList:
    def test_lists_a_record_without_its_hash(self, users_path, capsys):
        _seed(users_path, [du.UserRecord(login="yury", password_hash=du.hash_password("pw"),
                                         role="owner", telegram_id="900", aliases=("y",))])
        assert auth_users.cmd_users_list(_args()) == 0
        out, err = capsys.readouterr()
        combined = out + err
        assert "yury" in combined and "owner" in combined and "900" in combined
        assert "scrypt$" not in combined
        assert "pw" not in out.replace("owner", "")

    def test_json_output_has_no_password_hash_key(self, users_path, capsys):
        _seed(users_path, [du.UserRecord(login="a", password_hash=du.hash_password("pw"))])
        assert auth_users.cmd_users_list(_args(json=True)) == 0
        out, _err = capsys.readouterr()
        payload = json.loads(out)
        assert payload and "password_hash" not in payload[0]
        assert "scrypt$" not in out

    def test_missing_file_is_reported_not_an_error(self, users_path, capsys):
        assert auth_users.cmd_users_list(_args()) == 0
        out, _ = capsys.readouterr()
        assert "does not exist" in out

    def test_unusable_file_exits_nonzero_with_the_field_named(self, users_path, capsys):
        users_path.parent.mkdir(parents=True, exist_ok=True)
        users_path.write_text('{"login": "a"}\n', encoding="utf-8")
        os.chmod(users_path, 0o600)
        assert auth_users.cmd_users_list(_args()) == 1
        _out, err = capsys.readouterr()
        assert "password_hash" in err


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

class TestAdd:
    def test_adds_with_password_from_stdin_and_0600(self, users_path, monkeypatch, capsys):
        rc, out, _err = _add(monkeypatch, capsys, "yury", "hunter2", role="owner")
        assert rc == 0 and "added yury" in out
        assert stat.S_IMODE(users_path.stat().st_mode) == 0o600
        record = du.load_users_file(users_path).by_login("yury")
        assert record is not None and record.role == "owner"
        assert du.verify_password("hunter2", record.password_hash)

    def test_password_never_appears_in_argv(self):
        """There must be no argv flag that carries a password (it would show up in `ps`)."""
        import inspect

        source = inspect.getsource(auth_users)
        assert '"--password"' not in source and "'--password'" not in source
        assert "--password-stdin" in source

    def test_duplicate_login_is_refused(self, users_path, monkeypatch, capsys):
        _seed(users_path, [du.UserRecord(login="yury", password_hash=du.hash_password("pw"))])
        rc, _out, err = _add(monkeypatch, capsys, "yury", "new")
        assert rc == 1 and "already exists" in err

    def test_duplicate_alias_is_refused(self, users_path, monkeypatch, capsys):
        _seed(users_path, [du.UserRecord(login="yury", password_hash=du.hash_password("pw"),
                                         aliases=("y",))])
        rc, _out, err = _add(monkeypatch, capsys, "someone", "new", aliases=["y"])
        assert rc == 1 and "resolves to an existing record" in err

    def test_no_users_file_configured(self, monkeypatch, capsys):
        monkeypatch.setattr(auth_users, "_configured_users_file", lambda: None)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("pw\n"))
        rc = auth_users.cmd_users_add(_args(login="a", display_name="", role="user", aliases=[],
                                            telegram_id="", email=""))
        assert rc == 1
        _out, err = capsys.readouterr()
        assert "no users file configured" in err


# ---------------------------------------------------------------------------
# passwd / disable / enable
# ---------------------------------------------------------------------------

class TestGenRaising:
    def _seed_one(self, path: Path) -> None:
        _seed(path, [du.UserRecord(login="anna", password_hash=du.hash_password("old"), gen=3)])

    def test_passwd_changes_the_hash_and_raises_gen(self, users_path, monkeypatch, capsys):
        self._seed_one(users_path)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("new\n"))
        assert auth_users.cmd_users_passwd(_args(login="anna")) == 0
        out, _err = capsys.readouterr()
        assert "gen 3 -> 4" in out
        record = du.load_users_file(users_path).by_login("anna")
        assert record.gen == 4
        assert du.verify_password("new", record.password_hash)
        assert not du.verify_password("old", record.password_hash)

    def test_disable_then_enable_each_raise_gen(self, users_path, capsys):
        self._seed_one(users_path)
        assert auth_users.cmd_users_disable(_args(login="anna")) == 0
        record = du.load_users_file(users_path).by_login("anna")
        assert record.disabled is True and record.gen == 4

        assert auth_users.cmd_users_enable(_args(login="anna")) == 0
        record = du.load_users_file(users_path).by_login("anna")
        assert record.disabled is False and record.gen == 5

    def test_disabling_twice_says_so_and_does_not_bump_gen(self, users_path, capsys):
        _seed(users_path, [du.UserRecord(login="anna", password_hash=du.hash_password("x"),
                                         disabled=True, gen=7)])
        assert auth_users.cmd_users_disable(_args(login="anna")) == 0
        out, _ = capsys.readouterr()
        assert "already disabled" in out
        assert du.load_users_file(users_path).by_login("anna").gen == 7

    def test_unknown_login_is_a_clean_error(self, users_path, capsys):
        self._seed_one(users_path)
        assert auth_users.cmd_users_passwd(_args(login="ghost")) == 1
        _out, err = capsys.readouterr()
        assert "no user with login 'ghost'" in err


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

EXPORT = "\n".join([
    "login|display_name|password_hash|telegram_id|role|created_at|email",
    "yury.molodykh|Юрий|HASH1|900|owner|2026-01-05|yury@example.org",
    "yury|Юрий (old)|HASH2|900|owner|2026-01-06|yury@example.org",
    "anna.k|Анна|HASH3|901|user|2026-02-01|anna@example.org",
    "petr|Пётр|pbkdf2$nonsense|902|user|2026-02-02|",
    "olga|Ольга|HASH4||user|2026-03-01|olga@example.org",
]) + "\n"


class TestImportCommand:
    def _export(self, tmp_path: Path, text: str = EXPORT) -> Path:
        path = tmp_path / "export.txt"
        # Fill the placeholder hashes with real scrypt values so the file is importable as-is.
        path.write_text(
            text.replace("HASH1", du.hash_password("p1")).replace("HASH2", du.hash_password("p2"))
                .replace("HASH3", du.hash_password("p3")).replace("HASH4", du.hash_password("p4")),
            encoding="utf-8")
        return path

    def test_five_rows_one_duplicate(self, users_path, tmp_path, capsys):
        export = self._export(tmp_path)
        assert auth_users.cmd_users_import(Namespace(file=str(export), overwrite=False, dry_run=False)) == 0
        out, _err = capsys.readouterr()
        assert "added: 4" in out
        assert "merged: 1" in out
        assert "conflicts: 0" in out
        assert "unknown hash format: 1" in out
        assert "petr" in out, "the report names the login with the unknown hash"

        records = du.load_users_file(users_path).users
        assert len(records) == 4
        merged = next(r for r in records if r.login == "yury.molodykh")
        assert "yury" in merged.aliases

    def test_report_contains_no_hash_material(self, users_path, tmp_path, capsys):
        export = self._export(tmp_path)
        auth_users.cmd_users_import(Namespace(file=str(export), overwrite=False, dry_run=False))
        out, err = capsys.readouterr()
        for blob in (out, err):
            assert "scrypt$" not in blob
            assert "pbkdf2$nonsense" not in blob

    def test_dry_run_writes_nothing(self, users_path, tmp_path, capsys):
        export = self._export(tmp_path)
        assert auth_users.cmd_users_import(Namespace(file=str(export), overwrite=False, dry_run=True)) == 0
        out, _ = capsys.readouterr()
        assert "dry run" in out
        assert not users_path.exists()

    def test_existing_records_are_not_overwritten_by_default(self, users_path, tmp_path, capsys):
        original = du.hash_password("keep-me")
        _seed(users_path, [du.UserRecord(login="anna.k", password_hash=original, telegram_id="901",
                                         email="anna@example.org")])
        export = self._export(tmp_path)
        auth_users.cmd_users_import(Namespace(file=str(export), overwrite=False, dry_run=False))
        record = du.load_users_file(users_path).by_login("anna.k")
        assert record.password_hash == original

    def test_overwrite_replaces_the_hash(self, users_path, tmp_path):
        _seed(users_path, [du.UserRecord(login="anna.k", password_hash=du.hash_password("keep-me"),
                                         telegram_id="901", email="anna@example.org")])
        export = self._export(tmp_path)
        auth_users.cmd_users_import(Namespace(file=str(export), overwrite=True, dry_run=False))
        record = du.load_users_file(users_path).by_login("anna.k")
        assert du.verify_password("p3", record.password_hash)

    def test_malformed_export_imports_nothing(self, users_path, tmp_path, capsys):
        bad = tmp_path / "bad.txt"
        bad.write_text("a|A\n", encoding="utf-8")
        assert auth_users.cmd_users_import(Namespace(file=str(bad), overwrite=False, dry_run=False)) == 1
        _out, err = capsys.readouterr()
        assert "malformed" in err
        assert not users_path.exists()


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------

class TestParserWiring:
    def test_the_subtree_registers_under_auth(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        auth = sub.add_parser("auth")
        auth_sub = auth.add_subparsers(dest="auth_action")
        auth_users.build_auth_users_parser(auth_sub)

        args = parser.parse_args(["auth", "users", "list"])
        assert args.auth_action == "users"
        assert args.func is auth_users.cmd_users_list

    def test_a_bare_users_has_its_own_handler(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        auth = sub.add_parser("auth")
        auth_sub = auth.add_subparsers(dest="auth_action")
        auth_users.build_auth_users_parser(auth_sub)

        args = parser.parse_args(["auth", "users"])
        assert args.func is auth_users._users_no_verb

    def test_no_arg_takes_a_password_value(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        auth = sub.add_parser("auth")
        auth_sub = auth.add_subparsers(dest="auth_action")
        auth_users.build_auth_users_parser(auth_sub)

        with pytest.raises(SystemExit):
            parser.parse_args(["auth", "users", "add", "x", "--password", "secret"])
