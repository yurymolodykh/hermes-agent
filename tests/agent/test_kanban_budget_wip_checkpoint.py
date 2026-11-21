"""A budget stop preserves the card's work instead of losing it.

An iteration budget is a hard break out of the tool loop; until now the run then
closed as ``timed_out`` with whatever the model had written still uncommitted, so
the card read as a give-up while 1459 verified lines sat in the working tree
(``t_f3d28e37``). These are the behaviour contracts for the checkpoint, its
description on the board, and the card-sizing rule that warns earlier.
"""

from __future__ import annotations

import subprocess

import pytest


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=False,
    )


@pytest.fixture
def repo(tmp_path):
    """A real repository on its own branch — the shape a worktree card gets."""
    root = tmp_path / "card"
    root.mkdir()
    _git(root, "init", "-q", "-b", "wt/t_probe")
    _git(root, "config", "user.email", "probe@example.invalid")
    _git(root, "config", "user.name", "probe")
    (root / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "base.py")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_checkpoint_commits_the_working_tree_on_its_own_branch(repo):
    """The stop leaves a commit carrying the work, on the card's branch."""
    from agent.kanban_budget_checkpoint import checkpoint_wip

    (repo / "results.py").write_text("def probe():\n    return 25\n" * 40, encoding="utf-8")
    (repo / "verify.py").write_text("assert True\n", encoding="utf-8")

    result = checkpoint_wip(reason="предел", used=300, max_total=300,
                            workspace=repo, task_id="t_probe")

    assert result.mode == "git" and result.ok
    assert result.branch == "wt/t_probe"
    assert sorted(result.files) == ["results.py", "verify.py"]
    assert "300/300" in _git(repo, "log", "-1", "--pretty=%s").stdout
    # The work is really IN the commit, not just staged.
    committed = _git(repo, "show", "--stat", "--oneline", "HEAD").stdout
    assert "results.py" in committed and "verify.py" in committed
    status = _git(repo, "status", "--porcelain").stdout
    assert status.strip() == "", f"tree should be clean after checkpoint, got {status!r}"


def test_checkpoint_does_not_commit_dependency_trees(repo):
    """A venv/caches in the tree never enter the WIP commit."""
    from agent.kanban_budget_checkpoint import checkpoint_wip

    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "big.py").write_text("y = 2\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "base.cpython-314.pyc").write_bytes(b"\x00\x01")
    (repo / "work.py").write_text("z = 3\n", encoding="utf-8")

    result = checkpoint_wip(reason="предел", used=10, max_total=10,
                            workspace=repo, task_id="t_probe")

    assert result.files == ["work.py"]
    committed = _git(repo, "show", "--name-only", "--pretty=", "HEAD").stdout.split()
    assert committed == ["work.py"]


def test_checkpoint_with_nothing_changed_creates_no_commit(repo):
    """A card that fits its budget never gets a WIP commit."""
    from agent.kanban_budget_checkpoint import checkpoint_wip

    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = checkpoint_wip(reason="предел", used=5, max_total=5,
                            workspace=repo, task_id="t_probe")

    assert not result.ok and result.files == []
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    subjects = _git(repo, "log", "--pretty=%s").stdout
    assert "WIP бюджет" not in subjects


def test_checkpoint_falls_back_to_an_archive_without_a_repository(tmp_path, monkeypatch):
    """A scratch card with no repo still keeps its files, beside the board."""
    import tarfile

    from agent.kanban_budget_checkpoint import checkpoint_wip

    workspace = tmp_path / "scratch"
    workspace.mkdir()
    (workspace / "report.md").write_text("# done\n" * 10, encoding="utf-8")
    attachments = tmp_path / "attachments"
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_DIR", str(attachments))

    result = checkpoint_wip(reason="предел", used=8, max_total=8,
                            workspace=workspace, task_id="t_scratch")

    assert result.mode == "archive" and result.ok
    assert result.files == ["report.md"]
    with tarfile.open(result.archive) as tar:
        assert tar.getnames() == ["report.md"]


def test_preserve_wip_ref_parks_the_tree_without_touching_branch_or_index(repo):
    """The 85% safety net leaves history clean: work is reachable via a ref only."""
    from agent.kanban_budget_checkpoint import preserve_wip_ref

    (repo / "mid.py").write_text("partial = True\n", encoding="utf-8")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    ref = preserve_wip_ref(workspace=repo, task_id="t_probe")

    assert ref == "refs/hermes-wip/t_probe"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    # The file is still in the working tree (stash create does not touch it)...
    assert (repo / "mid.py").exists()
    # ...and its content is recoverable from the parked ref.
    shown = _git(repo, "show", f"{ref}:mid.py").stdout
    assert shown == "partial = True\n"
    assert "mid.py" in _git(repo, "status", "--porcelain").stdout


def test_preserve_wip_ref_is_a_noop_on_a_clean_tree(repo):
    from agent.kanban_budget_checkpoint import preserve_wip_ref

    assert preserve_wip_ref(workspace=repo, task_id="t_probe") is None
    assert _git(repo, "for-each-ref", "refs/hermes-wip/").stdout.strip() == ""


def test_stop_description_names_commit_and_what_is_left(repo):
    """The board text carries the commit and the open items."""
    from agent.kanban_budget_checkpoint import checkpoint_wip, describe_stop

    (repo / "results.py").write_text("done = 1\n", encoding="utf-8")
    (repo / "PROGRESS.md").write_text(
        "# work\n\n- [x] first\n- [ ] wire the loader\n- [ ] rerun the probe\n",
        encoding="utf-8",
    )
    result = checkpoint_wip(reason="предел", used=300, max_total=300,
                            workspace=repo, task_id="t_probe")

    note = describe_stop(result, used=300, max_total=300, workspace=repo)

    assert "300/300" in note
    assert result.commit and result.commit in note
    assert "wire the loader" in note and "rerun the probe" in note
    # Not the bare "give up" phrasing the board used to show.
    assert "could not complete" not in note


def test_stop_description_is_honest_when_nothing_could_be_saved(tmp_path):
    from agent.kanban_budget_checkpoint import describe_stop

    note = describe_stop(None, used=12, max_total=12, workspace=tmp_path)

    assert "12/12" in note
    assert "сохранения нет" in note
