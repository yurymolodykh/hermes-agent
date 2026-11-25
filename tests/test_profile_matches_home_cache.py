"""`profile_matches_home` remembers its answer — and forgets it when the served set changes.

The WS request path (``tui_gateway/ws.py`` → ``gateway/session_controls.py::_sibling_for`` →
``hermes_cli/profiles.py::profile_matches_home``) reached this resolver on every routed request,
and each call resolved two paths: ``realpath`` syscalls on the gateway's main thread (5 % of the
leaf samples, one 343 ms loop stall). The fix is a memo keyed by the two paths' normalized
spelling, with the same lock/TTL/ceiling and the same reset hook as ``profile_name_for_home``.

These tests pin the three things that make the memo safe: a repeated call touches no filesystem,
the answer is still the pre-fix answer, and a profile connect/disconnect drops it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import hermes_constants as hc
import hermes_cli.profiles as profiles


@pytest.fixture(autouse=True)
def _clear_cache():
    hc.reset_hermes_home_key_cache()
    yield
    hc.reset_hermes_home_key_cache()


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A default Hermes root with named profiles under it."""
    (tmp_path / "config.yaml").write_text("{}")
    for name in ("alpha", "beta"):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}")
    monkeypatch.setattr(hc, "get_default_hermes_root", lambda *a, **k: tmp_path)
    return tmp_path


@pytest.fixture
def resolved(monkeypatch):
    """Every ``Path.resolve`` performed, by path."""
    calls = []
    real = Path.resolve

    def counting(self, *args, **kwargs):
        calls.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting)
    return calls


class TestSameAnswerAsBefore:
    def test_own_home_matches(self, root):
        assert profiles.profile_matches_home("alpha", root / "profiles" / "alpha") is True

    def test_a_sibling_does_not_match(self, root):
        assert profiles.profile_matches_home("alpha", root / "profiles" / "beta") is False

    def test_the_default_home(self, root):
        assert profiles.profile_matches_home("default", root) is True
        assert profiles.profile_matches_home("default", root / "profiles" / "alpha") is False

    def test_a_symlinked_home_matches_its_target(self, root):
        link = root / "served"
        link.symlink_to(root / "profiles" / "alpha")
        assert profiles.profile_matches_home("alpha", link) is True

    def test_an_invalid_name_is_still_false(self, root):
        assert profiles.profile_matches_home("../escape", root) is False
        assert profiles.profile_matches_home(0, root) is False

    def test_a_trailing_separator_and_dot_segments_are_the_same_home(self, root):
        home = root / "profiles" / "alpha"
        assert profiles.profile_matches_home("alpha", Path(str(home) + os.sep)) is True
        assert profiles.profile_matches_home("alpha", home / "." / ".." / "alpha") is True


class TestRemembersTheAnswer:
    def test_repeated_call_resolves_nothing(self, root, resolved):
        home = root / "profiles" / "alpha"
        assert profiles.profile_matches_home("alpha", home) is True
        after_first = len(resolved)
        assert after_first > 0  # the first call really resolved: the cache is not the answer
        for _ in range(50):
            assert profiles.profile_matches_home("alpha", home) is True
        assert len(resolved) == after_first

    def test_a_spelling_variant_shares_the_answer(self, root, resolved):
        home = root / "profiles" / "alpha"
        assert profiles.profile_matches_home("alpha", home) is True
        after_first = len(resolved)
        assert profiles.profile_matches_home("alpha", Path(str(home) + os.sep)) is True
        assert len(resolved) == after_first

    def test_a_different_home_is_still_resolved(self, root, resolved):
        assert profiles.profile_matches_home("alpha", root / "profiles" / "alpha") is True
        before = len(resolved)
        assert profiles.profile_matches_home("alpha", root / "profiles" / "beta") is False
        assert len(resolved) > before


class TestInvalidationOnProfileSwitch:
    def test_forget_drops_a_stale_verdict(self, root):
        """A served link repointed away from the profile: the memo answers the old verdict until
        the served set changes, then the filesystem answer wins."""
        link = root / "served"
        link.symlink_to(root / "profiles" / "alpha")
        assert profiles.profile_matches_home("alpha", link) is True
        plain = root / "plain"
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        assert profiles.profile_matches_home("alpha", link) is True  # memoized
        hc.forget_home_match_cache()
        assert profiles.profile_matches_home("alpha", link) is False

    def test_reset_hermes_home_key_cache_drops_it_too(self, root):
        link = root / "served"
        link.symlink_to(root / "profiles" / "alpha")
        assert profiles.profile_matches_home("alpha", link) is True
        plain = root / "plain"
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        hc.reset_hermes_home_key_cache()
        assert profiles.profile_matches_home("alpha", link) is False

    def test_connecting_a_profile_drops_verdicts(self, root):
        """The served set is what the verdicts are about: ``SessionAuthorities.add`` (a profile
        connect) must invalidate them."""
        from gateway.session_authorities import SessionAuthorities

        link = root / "served"
        link.symlink_to(root / "profiles" / "alpha")
        assert profiles.profile_matches_home("alpha", link) is True
        plain = root / "plain"
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        assert profiles.profile_matches_home("alpha", link) is True  # memoized
        registry = SessionAuthorities(root, multiplexed=True)
        registry.add(root / "profiles" / "beta", object(), name="beta")
        assert profiles.profile_matches_home("alpha", link) is False

    def test_disconnecting_a_profile_drops_verdicts(self, root):
        from gateway.session_authorities import SessionAuthorities

        registry = SessionAuthorities(root, multiplexed=True)
        registry.add(root / "profiles" / "alpha", object(), name="alpha")
        link = root / "served"
        link.symlink_to(root / "profiles" / "alpha")
        assert profiles.profile_matches_home("alpha", link) is True
        plain = root / "plain"
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        assert profiles.profile_matches_home("alpha", link) is True  # memoized
        registry.remove(root / "profiles" / "alpha")
        assert profiles.profile_matches_home("alpha", link) is False

    def test_the_answer_expires(self, root, monkeypatch):
        link = root / "served"
        link.symlink_to(root / "profiles" / "alpha")
        monkeypatch.setattr(hc, "_PROFILE_NAME_TTL", 0.05)
        assert profiles.profile_matches_home("alpha", link) is True
        plain = root / "plain"
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        assert profiles.profile_matches_home("alpha", link) is True  # still inside the TTL
        from tests.waiting import wait_until
        wait_until(lambda: profiles.profile_matches_home("alpha", link) is False, timeout=5, interval=0.01)


class TestCeiling:
    def test_the_memo_stays_bounded(self, root, monkeypatch):
        monkeypatch.setattr(hc, "_PROFILE_NAME_MAX_ENTRIES", 4)
        for _ in range(20):
            profiles.profile_matches_home("alpha", root / "profiles" / "alpha")
        assert len(hc._HOME_MATCH_CACHE) <= 4
