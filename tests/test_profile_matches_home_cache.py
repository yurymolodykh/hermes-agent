"""`profile_matches_home` remembers its answer — and forgets it when the served set changes.

The WS request path (``tui_gateway/ws.py`` → ``gateway/session_controls.py::_sibling_for`` →
``hermes_cli/profiles.py::profile_matches_home``) reached this resolver on every routed request,
and each call resolved two paths: ``realpath`` syscalls on the gateway's main thread (5 % of the
leaf samples, one 343 ms loop stall). The fix remembers each operand's RESOLVED spelling per input
path — the same key shape as ``profile_name_for_home`` (path string + default root), so one entry
per distinct path, O(profiles) not O(profiles²) — and compares the two strings.

These tests pin the four things that make the memo safe: a repeated call touches no filesystem,
the answer is still the pre-fix answer, a profile connect/replace/disconnect drops it, and the
working set stays O(paths) on a real fleet-sized roster instead of thrashing the ceiling.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import hermes_constants as hc
from hermes_cli import profiles


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
        assert profiles.profile_matches_home(0, root) is False  # pyright: ignore[reportArgumentType]

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

    def test_replacing_a_reserved_slot_drops_verdicts(self, root):
        """``replace`` fills a slot reserved by ``add(home, None)`` — the instant a profile starts
        serving. A verdict from the previous served set (which could not name that home) must not
        outlive it."""
        from gateway.session_authorities import SessionAuthorities

        registry = SessionAuthorities(root, multiplexed=True)
        registry.add(root, object())  # launch slot
        home = root / "profiles" / "alpha"
        registry.add(home, None, name="alpha")  # reserved, not serving yet
        link = root / "served"
        link.symlink_to(home)
        assert profiles.profile_matches_home("alpha", link) is True
        plain = root / "plain"
        plain.mkdir()
        link.unlink()
        link.symlink_to(plain)
        assert profiles.profile_matches_home("alpha", link) is True  # remembered
        registry.replace(home, object())
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


class TestFleetWorkingSet:
    """The failure the pair key had: a host-scoped socket naming every served profile walks
    profile x home, so a pair key needed n² entries against a flat ceiling and cleared the whole
    dict on every insert. Assert the property that fixes it — the memo HITS at fleet size: no
    resolves in steady state, and a working set that scales with paths, not with names x homes."""

    FLEET = 38

    @pytest.fixture
    def fleet(self, tmp_path, monkeypatch):
        """38 served profiles, one host-scoped socket naming all of them (the live roster shape).
        The root carries the Hermes-home markers so ``get_profile_dir`` resolves real profiles."""
        from gateway.session_authorities import SessionAuthorities
        from gateway.session_controls import AuthorityConnection
        from types import SimpleNamespace

        (tmp_path / "config.yaml").write_text("{}")
        names = [f"p{i:02d}" for i in range(self.FLEET)]
        homes = {}
        for name in names:
            home = tmp_path / "profiles" / name
            home.mkdir(parents=True)
            (home / "config.yaml").write_text("{}")
            homes[name] = home
        monkeypatch.setattr(hc, "get_default_hermes_root", lambda *a, **k: tmp_path)

        registry = SessionAuthorities(tmp_path, multiplexed=True)
        launch = SimpleNamespace(profile_id=str(tmp_path), instance_id="inst-launch",
                                 events={}, runner=None, name=None)
        registry.add(tmp_path, launch, name=None)
        runner = SimpleNamespace(session_authorities=registry)
        launch.runner = runner
        authorities = []
        for name in names:
            auth = SimpleNamespace(profile_id=str(homes[name]), instance_id=f"inst-{name}",
                                   events={}, runner=runner, name=name)
            registry.add(homes[name], auth, name=name)
            authorities.append(auth)
        connection = AuthorityConnection(launch, object(),
                                         {"user_id": "u1", "profile_scope": "host"})
        return connection, names

    def test_a_second_sweep_resolves_nothing(self, fleet, resolved):
        """Steady state at fleet scale: warming over every served profile once is enough — the
        second sweep performs no ``resolve`` at all. (The old pair key re-resolved ~40 paths per
        call here because the working set overflowed the ceiling.)"""
        connection, names = fleet
        for name in names:
            connection._sibling_for(name)
        after_warm = len(resolved)
        assert len(hc._HOME_MATCH_CACHE) <= 2 * (self.FLEET + 1)  # one entry per path, not per pair
        for _ in range(3):
            for name in names:
                connection._sibling_for(name)
        assert len(resolved) == after_warm

    def test_the_working_set_does_not_grow_with_names_times_homes(self, fleet):
        """One entry per DISTINCT PATH: sweeping every name against every sibling home adds no
        n² blow-up, so the ceiling is never reached and the cache is never wiped."""
        connection, names = fleet
        clears = []
        cache = hc._HOME_MATCH_CACHE

        class _Watched(dict):
            def clear(inner):
                clears.append(1)
                super().clear()

        watched = _Watched(cache)
        hc._HOME_MATCH_CACHE = watched
        try:
            for _ in range(2):
                for name in names:
                    connection._sibling_for(name)
            assert clears == []
            assert len(watched) <= 2 * (self.FLEET + 1)
        finally:
            hc._HOME_MATCH_CACHE = cache
