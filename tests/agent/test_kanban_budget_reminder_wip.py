"""The soft budget reminder saves the tree too — including on a repo-less card.

``agent.budget_warning_ratio`` fires at 60% of the card budget, when the worker
still has iterations left and may well finish. It used to park the tree only for
git checkouts (``preserve_wip_ref`` returns ``None`` without a repository), which
is the minority of cards: most worktree-less cards run in a ``scratch`` directory,
so their warning arrived and their files stayed unprotected. These contracts hold
the reminder to saving *something* on both shapes, and to not piling up copies.
"""

from __future__ import annotations

import subprocess
import tarfile

import pytest


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False,
    )


class _Budget:
    def __init__(self, used: int, max_total: int):
        self.used = used
        self.max_total = max_total


class _Agent:
    """Only the attributes the reminder reads — no model, no session DB."""

    def __init__(self, used: int, max_total: int):
        self.budget_warning_ratio = 0.6
        self.iteration_budget = _Budget(used, max_total)
        self.valid_tool_names = {"kanban_complete", "kanban_comment"}
        self._iteration_budget_warning_injected = False


def _tool_turn():
    return [
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "wrote report.md"},
    ]


@pytest.fixture
def card_env(tmp_path, monkeypatch):
    """A dispatcher-shaped worker envelope plus a sandboxed board home."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "kanban-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "run-probe")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_PROFILE", "03-software-engineer")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    return workspace, home


def _archives(home):
    from hermes_cli.kanban_db import task_attachments_dir

    return sorted(task_attachments_dir("t_probe").glob("wip-*.tar.gz"))


def _remind(agent, messages):
    from agent.turn_iteration_prep import _maybe_inject_iteration_budget_warning

    return _maybe_inject_iteration_budget_warning(agent, messages)


def test_soft_budget_archives_a_workspace_without_a_repository(card_env):
    """A scratch card gets a tarball of its tree, named in the reminder text."""
    workspace, home = card_env
    (workspace / "report.md").write_text("# done\n" * 20, encoding="utf-8")
    (workspace / "PROGRESS.md").write_text("# progress\n- [ ] step\n", encoding="utf-8")
    messages = _tool_turn()

    # 108 of 180 iterations: past 0.6, the run continues.
    assert _remind(_Agent(used=108, max_total=180), messages) is True

    archives = _archives(home)
    assert len(archives) == 1, f"expected one archive, got {archives}"
    with tarfile.open(archives[0]) as tar:
        assert sorted(tar.getnames()) == ["PROGRESS.md", "report.md"]
        assert tar.extractfile("report.md").read() == b"# done\n" * 20
    # The model is told where the copy lives, in the very row it will read next.
    assert str(archives[0]) in messages[-1]["content"]


def test_soft_budget_keeps_the_git_ref_and_writes_no_archive(card_env):
    """A git card is unchanged: a parked ref, no tarball."""
    workspace, home = card_env
    _git(workspace, "init", "-q", "-b", "wt/t_probe")
    _git(workspace, "config", "user.email", "probe@example.invalid")
    _git(workspace, "config", "user.name", "probe")
    (workspace / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(workspace, "add", "base.py")
    _git(workspace, "commit", "-q", "-m", "base")
    (workspace / "mid.py").write_text("partial = True\n", encoding="utf-8")
    messages = _tool_turn()

    assert _remind(_Agent(used=108, max_total=180), messages) is True

    refs = _git(workspace, "for-each-ref", "refs/hermes-wip/", "--format=%(refname)").stdout
    assert refs.split() == ["refs/hermes-wip/t_probe"]
    assert _git(workspace, "show", "refs/hermes-wip/t_probe:mid.py").stdout == "partial = True\n"
    assert _archives(home) == []
    assert "refs/hermes-wip/t_probe" in messages[-1]["content"]


def test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive(card_env):
    """The threshold can fire more than once; identical copies are not more safety."""
    workspace, home = card_env
    (workspace / "report.md").write_text("# done\n", encoding="utf-8")

    _remind(_Agent(used=108, max_total=180), _tool_turn())
    first = _archives(home)
    assert len(first) == 1

    # Same tree, next turn (the latch is per turn, so the notice can fire again).
    messages = _tool_turn()
    _remind(_Agent(used=120, max_total=180), messages)

    assert _archives(home) == first
    # The reminder still names the existing copy rather than going silent.
    assert str(first[0]) in messages[-1]["content"]

    # A real edit does earn a fresh archive.
    (workspace / "report.md").write_text("# done\n" * 3, encoding="utf-8")
    _remind(_Agent(used=130, max_total=180), _tool_turn())
    assert len(_archives(home)) == 2
