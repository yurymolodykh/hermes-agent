"""`profile_name_for_home` remembers its answer: the WS roster poll must not resolve paths.

The roster (``profiles.list``, polled every 5 s per connection) reaches the resolver several
times per served bot, and each call cost a ``realpath`` syscall under ``Path.resolve``. These
tests pin the two halves of the fix: a repeated call touches no filesystem, and the answer still
follows the disk once the profile is reset/renamed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import hermes_constants as hc


@pytest.fixture(autouse=True)
def _clear_cache():
    hc.reset_hermes_home_key_cache()
    yield
    hc.reset_hermes_home_key_cache()


@pytest.fixture
def resolved(monkeypatch):
    """Every ``Path.resolve`` the resolver performs, by path."""
    calls = []
    real = Path.resolve

    def counting(self, *args, **kwargs):
        calls.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', counting)
    return calls


def _profile_home(tmp_path, name):
    home = tmp_path / 'profiles' / name
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('model:\n  default: fixture\n')
    return home


class TestRemembersTheAnswer:
    def test_repeated_call_resolves_nothing(self, tmp_path, resolved):
        home = _profile_home(tmp_path, 'alpha')
        assert hc.profile_name_for_home(home) == 'alpha'
        after_first = len(resolved)
        assert hc.profile_name_for_home(home) == 'alpha'
        assert hc.profile_name_for_home(str(home)) == 'alpha'
        assert len(resolved) >= after_first  # the first call did resolve (the cache is not the answer)
        assert len(resolved) == after_first

    def test_a_different_path_is_still_resolved(self, tmp_path, resolved):
        alpha, beta = _profile_home(tmp_path, 'alpha'), _profile_home(tmp_path, 'beta')
        assert hc.profile_name_for_home(alpha) == 'alpha'
        before = len(resolved)
        assert hc.profile_name_for_home(beta) == 'beta'
        assert len(resolved) > before


class TestInvalidation:
    def test_reset_forgets_the_answer(self, tmp_path, monkeypatch):
        home = _profile_home(tmp_path, 'alpha')
        link = tmp_path / 'served'
        link.symlink_to(home)
        assert hc.profile_name_for_home(link) == 'alpha'
        # The served link is repointed away from ``profiles/``: the memo still answers the old name.
        plain = tmp_path / 'plain'
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        assert hc.profile_name_for_home(link) == 'alpha'
        hc.reset_hermes_home_key_cache()
        assert hc.profile_name_for_home(link) is None

    def test_the_answer_expires(self, tmp_path, monkeypatch):
        home = _profile_home(tmp_path, 'alpha')
        link = tmp_path / 'served'
        link.symlink_to(home)
        monkeypatch.setattr(hc, '_PROFILE_NAME_TTL', 0.05)
        assert hc.profile_name_for_home(link) == 'alpha'
        plain = tmp_path / 'plain'
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        assert hc.profile_name_for_home(link) == 'alpha'  # still inside the TTL
        from tests.waiting import wait_until
        wait_until(lambda: hc.profile_name_for_home(link) is None, timeout=5, interval=0.01)


class TestSameAnswerAsBefore:
    def test_a_named_profile(self, tmp_path):
        assert hc.profile_name_for_home(_profile_home(tmp_path, 'alpha')) == 'alpha'

    def test_the_default_home(self):
        assert hc.profile_name_for_home(hc.get_default_hermes_root()) == 'default'

    def test_a_home_outside_profiles(self, tmp_path):
        plain = tmp_path / 'elsewhere'
        plain.mkdir()
        assert hc.profile_name_for_home(plain) is None

    def test_empty_input(self):
        assert hc.profile_name_for_home(None) is None
        assert hc.profile_name_for_home('   ') is None

    def test_a_trailing_separator(self, tmp_path):
        home = _profile_home(tmp_path, 'alpha')
        assert hc.profile_name_for_home(str(home) + os.sep) == 'alpha'
