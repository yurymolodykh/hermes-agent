"""The update rule: a move that leaves the live tree detached on FOREIGN history is refused.

26.11.2026 03:19: a host ended up serving ``8fc03744473`` from a detached checkout while its
gateway kept running ``da0f60614069``; the two sides disagreed on ``usage.main`` and every laned
turn died ~60 s in. An update must not be able to leave the tree that way: the refusal prints
``CODE UPDATE SKIPPED``, returns the checkout to the branch it came from, and changes nothing else.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A repo with one branch commit, then a foreign commit reachable only by detached HEAD."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("branch\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "branch commit")
    branch_sha = _git(root, "rev-parse", "HEAD").strip()

    # A second, unrelated root commit: history the branch does not contain.
    _git(root, "checkout", "-q", "--orphan", "foreign")
    _git(root, "commit", "-qm", "foreign commit")   # orphan checkout keeps a.txt staged
    foreign_sha = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "checkout", "-q", "main")
    _git(root, "branch", "-q", "-D", "foreign")
    return root, branch_sha, foreign_sha


def _detach(root, sha):
    _git(root, "checkout", "-q", "--detach", sha)


def _head(root):
    return _git(root, "rev-parse", "HEAD").strip()


def _main_stub(root):
    """``_m()`` without importing ``hermes_cli.main``: its import settles the REAL checkout's
    interrupted-pull marker, which is I/O against the live install and forbidden in tests."""
    stub = SimpleNamespace(PROJECT_ROOT=root)
    return patch.object(update_cmd, "_m", lambda: stub)


def _refuse(root, *, pre_pull_sha, pre_branch="main", release_tag=False):
    with _main_stub(root), \
            patch.object(update_cmd, "_record_stop") as record:
        with pytest.raises(SystemExit) as exc:
            update_cmd._refuse_detached_off_branch(
                ["git"], "main", pre_pull_sha=pre_pull_sha, pre_branch=pre_branch,
                release_tag=release_tag)
    return exc.value.code, record


def test_detached_on_foreign_history_is_refused_and_restored(repo, capsys):
    root, branch_sha, foreign_sha = repo
    _detach(root, foreign_sha)

    code, record = _refuse(root, pre_pull_sha=branch_sha)

    assert code == 1, "the update must stop, not report success"
    record.assert_called_once_with("detached_off_branch")
    out = capsys.readouterr().out
    assert "CODE UPDATE SKIPPED" in out
    assert foreign_sha[:10] in out and "main" in out
    # the tree came back to the branch, never left parked on foreign history
    assert _head(root) == branch_sha
    assert _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_detached_inside_the_branch_history_is_not_refused(repo):
    """A release/tag checkout at a commit the branch contains serves no foreign code."""
    root, branch_sha, _ = repo
    _detach(root, branch_sha)
    with _main_stub(root), patch.object(update_cmd, "_record_stop") as record:
        update_cmd._refuse_detached_off_branch(
            ["git"], "main", pre_pull_sha=branch_sha, pre_branch="main", release_tag=False)
    record.assert_not_called()
    assert _head(root) == branch_sha


def test_a_named_branch_is_never_refused(repo):
    """On a branch the wrong-branch gate judges; this rule only speaks for a detached HEAD."""
    root, branch_sha, _ = repo
    with _main_stub(root), patch.object(update_cmd, "_record_stop") as record:
        update_cmd._refuse_detached_off_branch(
            ["git"], "main", pre_pull_sha=branch_sha, pre_branch="main", release_tag=False)
    record.assert_not_called()


def test_release_update_is_exempt(repo):
    """A release lands detached on its tag by design: the rule must not fight it."""
    root, _, foreign_sha = repo
    _detach(root, foreign_sha)
    with _main_stub(root), patch.object(update_cmd, "_record_stop") as record:
        update_cmd._refuse_detached_off_branch(
            ["git"], "main", pre_pull_sha=foreign_sha, pre_branch="main", release_tag=True)
    record.assert_not_called()
    assert _head(root) == foreign_sha, "a release checkout stays where the release put it"


def test_restores_the_branch_it_came_from(repo):
    """When the update moved off another branch, the refusal returns THAT branch, not the target."""
    root, branch_sha, foreign_sha = repo
    _git(root, "checkout", "-q", "-b", "work")
    (root / "w.txt").write_text("w\n")
    _git(root, "add", "w.txt")
    _git(root, "commit", "-qm", "work commit")
    work_sha = _git(root, "rev-parse", "HEAD").strip()
    _detach(root, foreign_sha)

    _refuse(root, pre_pull_sha=work_sha, pre_branch="work")

    assert _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "work"
    assert _head(root) == work_sha
