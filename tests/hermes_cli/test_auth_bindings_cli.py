"""``hermes auth bindings`` — the CLI surface over the person→profiles registry.

The CLI is driven through the real argparse tree (``build_auth_parser``) so the verb registration
is exercised too, and every test points ``bindings_path`` at a tmp file: the default lives under the
Hermes ROOT and must never be written by a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_acl_bindings as hb


@pytest.fixture(autouse=True)
def _clean_cache():
    hb.reset_cache()
    yield
    hb.reset_cache()


@pytest.fixture
def path(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "acl" / "profile-bindings.json"
    monkeypatch.setattr(hb, "bindings_path", lambda: target)
    hb.reset_cache()
    return target


@pytest.fixture
def parser():
    """The REAL auth parser tree, so a missing/renamed verb fails here and not in production."""
    import argparse

    from hermes_cli.subcommands.auth import build_auth_parser

    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    build_auth_parser(sub, cmd_auth=lambda args: None)
    return root


def _dispatch(parser, argv: list[str]) -> int:
    from hermes_cli.auth_commands import auth_command

    args = parser.parse_args(argv)
    try:
        auth_command(args)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return 0


# ---------------------------------------------------------------------------
# 1. Registration and no-verb behaviour
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_all_verbs_are_registered(self, parser):
        for verb in ("list", "show", "bind", "unbind", "share", "owner"):
            args = parser.parse_args(["auth", "bindings", verb, "person/x"] if
                                     verb in ("show", "bind", "unbind") else ["auth", "bindings", verb])
            assert args.auth_action == "bindings"
            assert args.bindings_action == verb

    def test_no_verb_is_a_usage_error_not_a_traceback(self, parser, capsys):
        assert _dispatch(parser, ["auth", "bindings"]) == 2
        assert "choose list, show, bind, unbind, share or owner" in capsys.readouterr().err

    @pytest.mark.parametrize("verb", ["bind", "owner"])
    def test_profile_flag_is_not_registered_on_the_bindings_verbs(self, parser, verb):
        """`--profile/-p` is consumed by main._apply_profile_override BEFORE argparse runs, so a
        verb that accepted it would silently RE-HOME the process instead of naming a bound profile.
        `--holds` is the deliberate spelling; this test is what keeps the trap from coming back."""
        argv = ["auth", "bindings", verb, "--profile", "01-orchestrator"]
        if verb != "owner":
            argv.insert(3, "person/alice")
        with pytest.raises(SystemExit) as refused:
            parser.parse_args(argv)
        assert refused.value.code == 2  # argparse rejects it; it never reaches main's override

    def test_holds_flag_names_the_bound_profiles(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "bind", "person/alice",
                                  "--holds", "01-alice", "--holds", "16-personal"]) == 0
        record = hb.BindingStore(path).read().record("person/alice")
        assert record is not None and record.profiles == ("01-alice", "16-personal")

    def test_the_dispatch_reaches_the_verb(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "list"]) == 0
        assert "no records yet" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2. owner — the first binding
# ---------------------------------------------------------------------------


class TestOwnerVerb:
    def test_owner_writes_the_record_and_names_the_backup(self, parser, path, capsys):
        code = _dispatch(parser, ["auth", "bindings", "owner", "--slug", "person/yury",
                                  "--holds", "01-orchestrator",
                                  "--subject", "uid:1001", "--subject", "cron-owner"])
        out = capsys.readouterr().out
        assert code == 0
        assert "profiles=['01-orchestrator']" in out
        assert "backup:" in out
        assert "acl.enabled is unchanged" in out
        registry = hb.BindingStore(path).read()
        assert registry.person_of("uid:1001") == "person/yury"

    def test_owner_defaults_to_the_host_stamps(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "owner"]) == 0
        record = hb.BindingStore(path).read().record("person/owner")
        assert record is not None
        assert "cron-owner" in record.subjects
        assert any(s.startswith("uid:") or s.startswith("sid:") for s in record.subjects)
        assert record.profiles == ("01-orchestrator",)

    def test_owner_dry_run_writes_nothing(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "owner", "--dry-run"]) == 0
        assert "would write" in capsys.readouterr().out
        assert not path.exists()

    def test_owner_can_mark_profiles_shared(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "owner",
                                  "--shared", "02-hermes-platform",
                                  "--shared", "03-software-engineer"]) == 0
        assert hb.BindingStore(path).read().is_shared("02-hermes-platform") is True


# ---------------------------------------------------------------------------
# 3. bind / show / list / unbind / share
# ---------------------------------------------------------------------------


class TestVerbs:
    def test_bind_then_show(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "bind", "person/alice",
                                  "--holds", "01-alice", "--subject", "uid:2002"]) == 0
        assert _dispatch(parser, ["auth", "bindings", "show", "person/alice"]) == 0
        out = capsys.readouterr().out
        assert '"person/alice"' in out
        assert "person_of('person/alice') -> 'person/alice'" in out
        assert "['uid:2002']" in out

    def test_bind_requires_a_profile(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "bind", "person/alice"]) == 2
        assert "--holds is required" in capsys.readouterr().err

    def test_bind_rejects_an_invalid_slug(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "bind", "alice",
                                  "--holds", "01-alice"]) == 1
        assert "neither 'person/<login>'" in capsys.readouterr().err

    def test_bind_reports_a_profile_held_by_another_person(self, parser, path, capsys):
        _dispatch(parser, ["auth", "bindings", "bind", "person/yury",
                           "--holds", "01-orchestrator", "--subject", "uid:1001"])
        capsys.readouterr()
        assert _dispatch(parser, ["auth", "bindings", "bind", "person/alice",
                                  "--holds", "01-orchestrator", "--subject", "uid:2002"]) == 1
        assert "belongs to both" in capsys.readouterr().err

    def test_list_prints_a_table(self, parser, path, capsys):
        _dispatch(parser, ["auth", "bindings", "bind", "person/yury",
                           "--holds", "01-orchestrator", "--subject", "uid:1001"])
        capsys.readouterr()
        assert _dispatch(parser, ["auth", "bindings", "list"]) == 0
        out = capsys.readouterr().out
        assert "slug" in out and "person/yury" in out and "01-orchestrator" in out
        assert str(path) in out

    def test_list_json_is_the_document(self, parser, path, capsys):
        _dispatch(parser, ["auth", "bindings", "bind", "person/yury",
                           "--holds", "01-orchestrator", "--subject", "uid:1001"])
        capsys.readouterr()
        assert _dispatch(parser, ["auth", "bindings", "list", "--json"]) == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["persons"][0]["slug"] == "person/yury"

    def test_list_never_prints_hash_material(self, parser, path, capsys):
        """The registry holds no credentials; this asserts the CLI adds none either."""
        _dispatch(parser, ["auth", "bindings", "bind", "person/yury",
                           "--holds", "01-orchestrator", "--subject", "uid:1001"])
        capsys.readouterr()
        _dispatch(parser, ["auth", "bindings", "list", "--json"])
        out = capsys.readouterr().out
        for marker in ("password", "scrypt$", "$2b$", "$argon2"):
            assert marker not in out

    def test_show_of_a_missing_record_fails(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "show", "person/ghost"]) == 1
        assert "no record" in capsys.readouterr().err

    def test_unbind_removes_and_says_so(self, parser, path, capsys):
        _dispatch(parser, ["auth", "bindings", "bind", "person/alice",
                           "--holds", "01-alice", "--subject", "uid:2002"])
        _dispatch(parser, ["auth", "bindings", "owner", "--subject", "uid:1001"])
        capsys.readouterr()
        assert _dispatch(parser, ["auth", "bindings", "unbind", "person/alice"]) == 0
        assert "unbound person/alice" in capsys.readouterr().out
        assert hb.BindingStore(path).read().record("person/alice") is None

    def test_unbind_of_a_missing_record_is_reported_not_failed(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "unbind", "person/ghost"]) == 0
        assert "nothing to remove" in capsys.readouterr().out

    def test_share_replaces_the_public_set(self, parser, path, capsys):
        assert _dispatch(parser, ["auth", "bindings", "share", "02-hermes-platform"]) == 0
        assert "shared profiles" in capsys.readouterr().out
        assert hb.BindingStore(path).read().shared_profiles() == ("02-hermes-platform",)


# ---------------------------------------------------------------------------
# 4. A broken registry is reported and NEVER overwritten
# ---------------------------------------------------------------------------


class TestBrokenRegistry:
    def _corrupt(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

    def test_list_reports_the_refusal_and_the_restore_hint(self, parser, path, capsys):
        self._corrupt(path)
        assert _dispatch(parser, ["auth", "bindings", "list"]) == 1
        err = capsys.readouterr().err
        assert "invalid JSON" in err
        assert "Restore a copy from" in err

    def test_list_does_not_overwrite_the_broken_file(self, parser, path, capsys):
        self._corrupt(path)
        before = path.read_text(encoding="utf-8")
        _dispatch(parser, ["auth", "bindings", "list"])
        assert path.read_text(encoding="utf-8") == before

    def test_bind_reports_the_refusal(self, parser, path, capsys):
        self._corrupt(path)
        assert _dispatch(parser, ["auth", "bindings", "bind", "person/alice",
                                  "--holds", "01-alice"]) == 1
        assert path.read_text(encoding="utf-8") == "{ not json"

    def test_show_reports_the_refusal(self, parser, path, capsys):
        self._corrupt(path)
        assert _dispatch(parser, ["auth", "bindings", "show", "person/alice"]) == 1
        assert "invalid JSON" in capsys.readouterr().err
