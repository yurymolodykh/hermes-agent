"""``hermes user add`` — one command, four artefacts, all-or-nothing.

A user in this install is a profile ``01-<login>`` (personal SOUL + questionnaire), a
``person/<login>`` ACL binding, a users-file record with a password, and a
``gateway.profile_routes`` entry that sends their Telegram messages to the profile through
the owner's single bot. These tests pin the three properties that make the command safe to
run unattended:

* **complete** — one call leaves all four registries agreeing;
* **all-or-nothing** — a failure injected at any step removes everything earlier steps wrote
  and returns non-zero, so a half-created person can never sign in;
* **refused before writing** — a duplicate profile, binding, login or route exits ``2``
  without touching a single file.

Everything runs against the per-test isolated ``HERMES_HOME`` (``tests/conftest.py``); no test
reads or writes the operator's real profiles, config, or users file.
"""

from __future__ import annotations

import json
import stat

import pytest

import hermes_acl_bindings as hb
from hermes_cli import user_admin as ua
from hermes_cli.config import get_config_path
from hermes_cli.config_backend import read_config_doc


@pytest.fixture(autouse=True)
def _clean_registry_cache():
    """The bindings store caches by path; a test must never inherit another test's view."""
    hb.reset_cache()
    yield
    hb.reset_cache()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A HERMES_HOME whose *profiles root* is also isolated.

    ``profiles`` are anchored to ``get_default_hermes_root()`` rather than to
    ``HERMES_HOME``, so a test that only set the variable would create directories in the
    operator's real ``~/.hermes/profiles``.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: root)
    return root


def _routes() -> list[dict]:
    document = read_config_doc(get_config_path()) or {}
    gateway = document.get("gateway") if isinstance(document, dict) else None
    return list((gateway or {}).get("profile_routes") or [])


def _registry():
    return hb.store().read()


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_add_creates_profile_soul_binding_password_and_route(home):
    result = ua.add_user("demo1", telegram_id="555000111", password="S3cret-Passw0rd")

    profile_dir = home / "profiles" / "01-demo1"
    assert result["profile"] == "01-demo1"
    assert profile_dir.is_dir()

    soul = profile_dir / "SOUL.md"
    assert soul.is_file() and soul.read_text(encoding="utf-8").strip(), "SOUL must not be empty"
    assert (profile_dir / ua.QUESTIONNAIRE_FILENAME).is_file()

    registry = _registry()
    record = registry.record("person/demo1")
    assert record is not None
    assert record.profiles == ("01-demo1",)
    assert record.subjects == ("tg:555000111",)

    users_file = home / "users.jsonl"
    assert users_file.is_file()
    assert stat.S_IMODE(users_file.stat().st_mode) == 0o600
    from hermes_cli import dashboard_users as du

    loaded = du.load_users_file(users_file)
    assert loaded.ok, loaded.problems
    stored = du.lookup(loaded.users, "demo1")
    assert stored is not None and stored.telegram_id == "555000111"

    routes = _routes()
    assert len(routes) == 1
    route = routes[0]
    assert route["platform"] == "telegram"
    assert route["profile"] == "01-demo1"
    assert route["user_id"] == "555000111"
    assert route["bot_profile"] is None, "everyone rides the owner's single bot"


def test_add_writes_the_password_to_an_owner_only_file(home):
    result = ua.add_user("demo2", password="Pas$word-with-length")

    path = home / "credentials" / "demo2.password"
    assert result["password_file"] == str(path)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8").strip() == "Pas$word-with-length"


def test_add_never_prints_the_full_password(home, capsys):
    secret = "Kx9-QwErTy-UiOpAsDfGh"
    assert ua.cmd_user_add(ua._args_for_test("demo3", password=secret)) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret[:7] in captured.out, "the 7-character prefix is how the owner recognises it"
    assert "credentials/demo3.password" in captured.out.replace("\\", "/")


def test_add_without_telegram_id_warns_and_still_creates_the_user(home, capsys):
    assert ua.cmd_user_add(ua._args_for_test("demo4", telegram_id="", password="x" * 20)) == 0
    assert "no --telegram-id" in capsys.readouterr().err
    assert (home / "profiles" / "01-demo4").is_dir()
    assert _routes() == []


# ---------------------------------------------------------------------------
# refusals happen before the first write
# ---------------------------------------------------------------------------


def test_duplicate_profile_is_refused_without_touching_anything(home):
    ua.add_user("demo5", telegram_id="555000555", password="first-password")
    config_before = get_config_path().read_text(encoding="utf-8")
    registry_before = get_config_path().parent.joinpath("acl", "profile-bindings.json").read_text(
        encoding="utf-8")
    users_before = (home / "users.jsonl").read_text(encoding="utf-8")

    with pytest.raises(ua.Refused):
        ua.add_user("demo5", telegram_id="555000999", password="second-password")

    assert get_config_path().read_text(encoding="utf-8") == config_before
    assert get_config_path().parent.joinpath("acl", "profile-bindings.json").read_text(
        encoding="utf-8") == registry_before
    assert (home / "users.jsonl").read_text(encoding="utf-8") == users_before
    assert len(_routes()) == 1


def test_duplicate_telegram_id_is_refused(home):
    ua.add_user("demo6", telegram_id="555111222", password="a-password")
    with pytest.raises(ua.Refused) as excinfo:
        ua.add_user("demo7", telegram_id="555111222", password="b-password")
    assert "already bound" in str(excinfo.value)
    assert not (home / "profiles" / "01-demo7").exists()


def test_cli_returns_exit_code_2_on_a_duplicate(home, capsys):
    assert ua.cmd_user_add(ua._args_for_test("demo8", password="one")) == 0
    capsys.readouterr()
    assert ua.cmd_user_add(ua._args_for_test("demo8", password="two")) == 2
    assert "refused" in capsys.readouterr().err


def test_an_invalid_login_is_refused(home):
    with pytest.raises(ua.Refused):
        ua.add_user("../escape", password="x" * 12)
    assert not (home / "profiles").exists() or not any(
        (home / "profiles").iterdir()), "nothing may be created for an invalid login"


# ---------------------------------------------------------------------------
# all-or-nothing
# ---------------------------------------------------------------------------


def test_a_failure_after_the_profile_rolls_everything_back(home, monkeypatch):
    """A route write that explodes must leave no profile, binding, record or password."""
    def boom(*_args, **_kwargs):
        raise OSError("simulated config write failure")

    monkeypatch.setattr(ua, "write_route_undoable", boom)

    with pytest.raises(ua.Failed):
        ua.add_user("demo9", telegram_id="555999888", password="doomed-password")

    assert not (home / "profiles" / "01-demo9").exists(), "the profile must be gone"
    assert _registry() is None or _registry().record("person/demo9") is None
    assert not (home / "users.jsonl").exists() or "demo9" not in (
        home / "users.jsonl").read_text(encoding="utf-8")
    assert not (home / "credentials" / "demo9.password").exists()


def test_a_failure_while_binding_rolls_the_profile_back(home, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated registry failure")

    monkeypatch.setattr(hb.BindingStore, "bind", boom)

    with pytest.raises(ua.Failed):
        ua.add_user("demo10", telegram_id="555777666", password="doomed-two")

    assert not (home / "profiles" / "01-demo10").exists()
    assert _routes() == []


def test_cli_returns_non_zero_after_a_rollback(home, monkeypatch, capsys):
    monkeypatch.setattr(ua, "write_route_undoable",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert ua.cmd_user_add(ua._args_for_test("demo11", telegram_id="555", password="p")) == 1
    assert "nothing was created" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# list / remove
# ---------------------------------------------------------------------------


def test_list_reports_the_four_registries_and_never_a_hash(home, capsys):
    ua.add_user("demo12", telegram_id="555123123", password="visible-password")
    rows = ua.list_users()
    row = next(entry for entry in rows if entry["login"] == "demo12")
    assert row["bound"] is True
    assert row["has_password"] is True
    assert row["route"]["bot_profile"] is None
    assert row["subjects"] == ["tg:555123123"]

    assert ua.cmd_user_list(ua._args_for_test(None, action="list")) == 0
    printed = capsys.readouterr().out
    assert "demo12" in printed
    assert "scrypt$" not in printed, "a listing must never leak a password hash"


def test_remove_takes_the_route_binding_record_and_profile(home):
    ua.add_user("demo13", telegram_id="555321321", password="removable")
    ua.remove_user("demo13")

    assert not (home / "profiles" / "01-demo13").exists()
    assert _routes() == []
    registry = _registry()
    assert registry is None or registry.record("person/demo13") is None
    assert "demo13" not in (home / "users.jsonl").read_text(encoding="utf-8")


def test_remove_of_a_missing_user_is_refused(home):
    with pytest.raises(ua.Refused):
        ua.remove_user("nobody-here")
