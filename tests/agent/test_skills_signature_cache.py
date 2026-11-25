"""The skills tree signature must not walk every directory on every status poll.

``_dir_tree_signature`` opens a ``scandir`` per catalog directory (~161 on a real profile,
~450 ``openat`` + ~440 ``getdents64`` per walk) and the status path asks for the signature
repeatedly for a tree that did not change. The walk is now memoised per root, keyed on the
mtimes it recorded, so a repeat only re-stats those directories.

The freshness the catalog promises is unchanged: a skill installed or removed anywhere in
the tree must be visible on the very NEXT call — including two changes inside one coarse
mtime tick, which is why a directory whose mtime is inside that window still forces a walk.
"""

from __future__ import annotations

import os

import pytest

import tools.skills_tool as st


@pytest.fixture(autouse=True)
def _fresh_signature_cache():
    st.clear_skills_cache()
    yield
    st.clear_skills_cache()


def _make_skill(root, name, description=None):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or f'Description for {name}.'}\n---\n\nDo it.\n",
        encoding="utf-8")
    return skill_dir


def _age_tree(root, seconds=10):
    """Move every mtime in the tree outside the coarse-tick window (a settled catalog)."""
    stamp = os.stat(root).st_mtime - seconds
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in [*filenames, *dirnames]:
            path = os.path.join(dirpath, name)
            try:
                os.utime(path, (stamp, stamp), follow_symlinks=False)
            except OSError:
                pass
        try:
            os.utime(dirpath, (stamp, stamp))
        except OSError:
            pass


def test_repeat_signature_does_not_walk_the_tree(tmp_path):
    """A settled, unchanged tree costs stats — never a ``scandir`` that enumerates it."""
    _make_skill(tmp_path, "steady")
    _age_tree(tmp_path)

    st._skills_scan_signature([tmp_path], frozenset())
    assert str(tmp_path) in st._SIGNATURE_CACHE

    walks = []
    real = st._dir_tree_signature

    def counting(root, sig, *args, **kwargs):
        walks.append(str(root))
        return real(root, sig, *args, **kwargs)

    st._dir_tree_signature = counting
    try:
        for _ in range(5):
            st._skills_scan_signature([tmp_path], frozenset())
    finally:
        st._dir_tree_signature = real

    assert walks == [], f"an unchanged tree was re-walked {len(walks)} time(s)"


def test_signature_is_identical_with_and_without_the_cache(tmp_path):
    """The cached value must equal a fresh walk's value, byte for byte."""
    _make_skill(tmp_path, "one")
    _make_skill(tmp_path / "cat", "two")

    first = st._skills_scan_signature([tmp_path], frozenset())
    st._SIGNATURE_CACHE.clear()
    second = st._skills_scan_signature([tmp_path], frozenset())
    assert first == second

    _age_tree(tmp_path)
    st._SIGNATURE_CACHE.clear()
    settled = st._skills_scan_signature([tmp_path], frozenset())
    cached = st._skills_scan_signature([tmp_path], frozenset())
    assert cached == settled


def test_install_inside_one_mtime_tick_is_still_visible(tmp_path):
    """The coarse-tick window: install + uninstall back to back must not be hidden by the memo.

    ``first`` and ``installed`` are created and removed within the same second, so a
    name-only signature could look unchanged while the catalog differs.
    """
    _make_skill(tmp_path, "first")
    before = st._skills_scan_signature([tmp_path], frozenset())

    _make_skill(tmp_path, "installed-elsewhere")
    _age_tree(tmp_path, seconds=0)  # keep the change INSIDE the coarse-tick window
    after = st._skills_scan_signature([tmp_path], frozenset())
    assert after != before, "an install inside one mtime tick was hidden by the cache"


def test_removing_a_skill_changes_the_signature(tmp_path):
    """An uninstall must move the signature, cached or not."""
    import shutil

    _make_skill(tmp_path, "first")
    _make_skill(tmp_path, "second")
    _age_tree(tmp_path)
    before = st._skills_scan_signature([tmp_path], frozenset())

    shutil.rmtree(tmp_path / "second")
    after = st._skills_scan_signature([tmp_path], frozenset())
    assert after != before, "the removal of a skill did not move the signature"


def test_vanished_root_falls_back_to_a_walk(tmp_path):
    """A root that disappears must not answer from a memo of the old tree."""
    root = tmp_path / "skills"
    _make_skill(root, "here")
    _age_tree(root)
    st._skills_scan_signature([root], frozenset())

    import shutil

    shutil.rmtree(root)
    sig = st._skills_scan_signature([root], frozenset())
    assert isinstance(sig, tuple), "a vanished root must still yield a signature"


def test_clear_skills_cache_drops_the_signature_memo(tmp_path):
    """An explicit reload must re-walk, never be served from the memo."""
    _make_skill(tmp_path, "steady")
    _age_tree(tmp_path)
    st._skills_scan_signature([tmp_path], frozenset())
    assert st._SIGNATURE_CACHE

    st.clear_skills_cache()
    assert not st._SIGNATURE_CACHE, "clear_skills_cache left the tree memo behind"
