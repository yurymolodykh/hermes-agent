"""``acl.multi_user`` is a deployment flag: a profile home inherits the root's answer.

Reading only the process ``HERMES_HOME``'s ``config.yaml`` reported "off" inside every per-profile
process — a profile-scoped socket runs in ``owner_scope(served)``, where ``HERMES_HOME`` is the
served profile's directory and its ``config.yaml`` has no ``acl`` section. The whole multi-user layer
(including the socket profile gate) was therefore disabled exactly where it had to hold. Measured
before the fix: root → True, ``profiles/01-maria-safronova`` → False, ``profiles/01-novikov`` → False.

An explicit value in the profile's own file still wins: a per-profile override is a deliberate act,
and the shipped ``DEFAULT_CONFIG`` already carries the section, so the merged read cannot tell
"off" from "unset" — only the user layer can.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import hermes_acl_identity


@pytest.fixture
def root(monkeypatch, tmp_path):
    """A root with ``profiles/01-maria``; ``HERMES_HOME`` starts at the root."""
    home = tmp_path / "root"
    (home / "profiles" / "01-maria").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_acl_identity, "_ROOT_CACHE", None, raising=False)
    return home


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _profile_mode(root: Path, monkeypatch) -> Path:
    profile_home = root / "profiles" / "01-maria"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    return profile_home


def test_the_root_flag_is_read_from_the_root(root):
    _write(root / "config.yaml", "acl:\n  multi_user: true\n")
    assert hermes_acl_identity.multi_user_enabled() is True


def test_a_profile_without_an_acl_section_inherits_the_root(root, monkeypatch):
    """The defect: this answered False inside a profile-scoped process."""
    _write(root / "config.yaml", "acl:\n  multi_user: true\n")
    profile_home = _profile_mode(root, monkeypatch)
    _write(profile_home / "config.yaml", "model:\n  name: x\n")
    assert hermes_acl_identity.multi_user_enabled() is True


def test_a_profile_that_says_off_still_wins_over_the_root(root, monkeypatch):
    _write(root / "config.yaml", "acl:\n  multi_user: true\n")
    profile_home = _profile_mode(root, monkeypatch)
    _write(profile_home / "config.yaml", "acl:\n  multi_user: false\n")
    assert hermes_acl_identity.multi_user_enabled() is False


def test_nobody_asking_means_off(root, monkeypatch):
    """Legacy default intact: neither file mentions the flag."""
    _write(root / "config.yaml", "{}\n")
    profile_home = _profile_mode(root, monkeypatch)
    _write(profile_home / "config.yaml", "{}\n")
    assert hermes_acl_identity.multi_user_enabled() is False


def test_a_profile_home_whose_root_has_no_flag_stays_off(root, monkeypatch):
    profile_home = _profile_mode(root, monkeypatch)
    _write(profile_home / "config.yaml", "{}\n")
    assert hermes_acl_identity.multi_user_enabled() is False
