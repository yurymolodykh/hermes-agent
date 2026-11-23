"""The soft budget reminder saves the tree too — including on a repo-less card.

``agent.budget_warning_ratio`` fires at 60% of the card budget, when the worker
still has iterations left and may well finish. It used to park the tree only for
git checkouts (``preserve_wip_ref`` returns ``None`` without a repository), which
is the minority of cards: most worktree-less cards run in a ``scratch`` directory,
so their warning arrived and their files stayed unprotected. These contracts hold
the reminder to saving *something* on both shapes, and to not piling up copies.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

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


def test_a_truncated_archive_is_never_the_one_the_notice_names(card_env, monkeypatch):
    """A copy cut off at the size cap must never stand in for the real tree.

    A truncated tarball sits in the same directory as the complete ones. Once the
    tree returns to an already-copied state, the newest file by mtime is the
    truncated one, so without a completeness marker the reminder points the model
    at an archive holding zero entries.
    """
    from agent import kanban_budget_checkpoint as kbc

    workspace, _home = card_env
    (workspace / "a.txt").write_text("A" * 100, encoding="utf-8")
    (workspace / "b.txt").write_text("B" * 100, encoding="utf-8")
    before = {n: (workspace / n).stat() for n in ("a.txt", "b.txt")}

    first = kbc.preserve_wip_tree(task_id="t_probe")
    assert first.mode == "archive" and first.locator

    # monkeypatch.undo() would also drop the fixture's env; restore just this knob.
    cap = kbc._ARCHIVE_MAX_BYTES
    monkeypatch.setattr(kbc, "_ARCHIVE_MAX_BYTES", 1024)
    try:
        (workspace / "a.txt").write_text("A" * 5000, encoding="utf-8")
        over_cap = kbc.preserve_wip_tree(task_id="t_probe")
    finally:
        kbc._ARCHIVE_MAX_BYTES = cap
    # Nothing the model is shown may present the cut-off copy as the tree.
    assert over_cap.mode != "archive"
    assert over_cap.locator != first.locator

    # Back to exactly the copied state: same bytes, same sizes, same mtimes.
    (workspace / "a.txt").write_text("A" * 100, encoding="utf-8")
    for name, st in before.items():
        os.utime(workspace / name, ns=(st.st_atime_ns, st.st_mtime_ns))

    again = kbc.preserve_wip_tree(task_id="t_probe")
    assert again.mode == "archive"
    assert str(again.locator) == str(first.locator)
    with tarfile.open(again.locator) as tar:
        assert sorted(tar.getnames()) == ["a.txt", "b.txt"]


def test_over_cap_repeats_write_one_partial_copy_and_say_so(card_env, monkeypatch):
    """Over the cap the threshold fires every turn; four copies is not more safety.

    The copy that is written is partial by construction, so the reminder must say
    so instead of promising that "that archive survives the budget stop".
    """
    from agent import kanban_budget_checkpoint as kbc

    workspace, _home = card_env
    for i in range(4):
        (workspace / f"part{i}.bin").write_bytes(b"P" * (256 * 1024))
    monkeypatch.setattr(kbc, "_ARCHIVE_MAX_BYTES", 300 * 1024)

    outcomes = [kbc.preserve_wip_tree(task_id="t_probe") for _ in range(4)]

    from hermes_cli.kanban_db import task_attachments_dir

    on_disk = sorted(task_attachments_dir("t_probe").glob("*"))
    assert len(on_disk) <= 2, f"the cap path must not pile up copies: {on_disk}"
    assert all(o.mode != "archive" for o in outcomes), [
        (o.mode, o.locator) for o in outcomes
    ]
    written = [o for o in outcomes if o.mode == "partial"]
    assert written, [o.detail for o in outcomes]
    for out in written:
        assert out.locator and os.path.exists(out.locator)
        assert out.detail and "част" in out.detail.lower()

    messages = _tool_turn()
    assert _remind(_Agent(used=108, max_total=180), messages) is True
    notice = messages[-1]["content"]
    assert "partial" in notice.lower()
    assert "survives the budget stop" not in notice


def test_a_complete_copy_clears_the_loss_record_and_a_shrunk_tree_is_copied(
    card_env, monkeypatch,
):
    """After a partial record, a tree that fits again must get a real copy.

    Otherwise a stale ``wip-partial.json`` would keep telling successors "no copy
    exists" long after the duplicate/scratch files that blew the cap were cleaned up.
    """
    from agent import kanban_budget_checkpoint as kbc

    workspace, _home = card_env
    for i in range(3):
        (workspace / f"part{i}.bin").write_bytes(b"P" * (256 * 1024))
    monkeypatch.setattr(kbc, "_ARCHIVE_MAX_BYTES", 300 * 1024)
    over = kbc.preserve_wip_tree(task_id="t_probe")
    assert over.mode == "partial"

    for i in range(3):
        (workspace / f"part{i}.bin").unlink()
    (workspace / "small.txt").write_text("kept\n", encoding="utf-8")

    fits = kbc.preserve_wip_tree(task_id="t_probe")
    assert fits.mode == "archive" and fits.locator
    with tarfile.open(fits.locator) as tar:
        assert tar.getnames() == ["small.txt"]
    from hermes_cli.kanban_db import task_attachments_dir
    assert not (task_attachments_dir("t_probe") / "wip-partial.json").exists()


def test_reuse_of_an_existing_archive_clears_the_stale_loss_record(card_env, monkeypatch):
    """A tree that comes back to an already-copied state must not keep the loss record.

    The reuse branch returns the existing complete archive before the write path
    runs, so the overwritten ``wip-partial.json`` ("the tree is over the cap, not
    one file was copied") used to outlive the loss it described. A human opening
    the card's attachments then read a false statement next to a real copy.
    """
    from agent import kanban_budget_checkpoint as kbc
    from hermes_cli.kanban_db import task_attachments_dir

    workspace, _home = card_env
    (workspace / "a.txt").write_text("A" * 100, encoding="utf-8")
    (workspace / "b.txt").write_text("B" * 100, encoding="utf-8")
    before = {n: (workspace / n).stat() for n in ("a.txt", "b.txt")}

    first = kbc.preserve_wip_tree(task_id="t_probe")
    assert first.mode == "archive" and first.locator
    out_dir = task_attachments_dir("t_probe")

    monkeypatch.setattr(kbc, "_ARCHIVE_MAX_BYTES", 1024)
    (workspace / "a.txt").write_text("A" * 5000, encoding="utf-8")
    over = kbc.preserve_wip_tree(task_id="t_probe")
    assert over.mode == "partial"
    assert (out_dir / "wip-partial.json").exists()

    # Back to exactly the copied state: same bytes, same sizes, same mtimes.
    (workspace / "a.txt").write_text("A" * 100, encoding="utf-8")
    for name, st in before.items():
        os.utime(workspace / name, ns=(st.st_atime_ns, st.st_mtime_ns))

    again = kbc.preserve_wip_tree(task_id="t_probe")
    assert again.mode == "archive"
    assert str(again.locator) == str(first.locator)
    assert not (out_dir / "wip-partial.json").exists(), (
        "the loss record outlived the loss it describes"
    )


def test_archive_retention_bounds_the_complete_pairs(card_env):
    """Every distinct tree earns its own copy; the directory must not grow forever."""
    from agent import kanban_budget_checkpoint as kbc
    from hermes_cli.kanban_db import task_attachments_dir

    workspace, _home = card_env
    written = []
    for i in range(6):
        (workspace / "a.txt").write_text(f"gen-{i}\n", encoding="utf-8")
        # A distinct mtime per generation: the fingerprint includes it by design.
        os.utime(workspace / "a.txt", ns=(1_700_000_000_000_000_000 + i * 10**9,) * 2)
        out = kbc.preserve_wip_tree(task_id="t_probe")
        assert out.mode == "archive"
        written.append(str(out.locator))

    out_dir = task_attachments_dir("t_probe")
    tarballs = sorted(p.name for p in out_dir.glob("wip-*.tar.gz"))
    markers = sorted(p.name for p in out_dir.glob("wip-*.tar.gz.done"))
    assert len(tarballs) <= kbc._ARCHIVE_RETENTION, f"unbounded history: {tarballs}"
    assert len(markers) == len(tarballs), f"pairs must stay paired: {tarballs} / {markers}"
    assert Path(written[-1]).name in tarballs, "the newest copy is always kept"
    # No orphan markers left behind by the pruning.
    for marker in out_dir.glob("wip-*.tar.gz.done"):
        assert marker.with_name(marker.name[: -len(".done")]).exists()


def test_archive_retention_keeps_one_generation_of_unmarked_tarballs(card_env):
    """Legacy archives (no ``.done``) are not reusable but one generation is kept."""
    from agent import kanban_budget_checkpoint as kbc
    from hermes_cli.kanban_db import task_attachments_dir

    workspace, _home = card_env
    out_dir = task_attachments_dir("t_probe")
    out_dir.mkdir(parents=True, exist_ok=True)
    legacy = []
    for index, stamp in enumerate(("20260101T000000", "20260102T000000", "20260103T000000")):
        archive = out_dir / f"wip-{stamp}.tar.gz"
        archive.write_bytes(b"legacy")
        os.utime(archive, ns=(1_700_000_000_000_000_000 + index * 10**9,) * 2)
        legacy.append(archive)

    (workspace / "a.txt").write_text("fresh\n", encoding="utf-8")
    assert kbc.preserve_wip_tree(task_id="t_probe").mode == "archive"

    surviving = [p.name for p in legacy if p.exists()]
    assert surviving == [legacy[-1].name], (
        f"one generation of unmarked archives survives, not all: {surviving}"
    )


def test_retention_never_deletes_the_copy_the_reminder_names(card_env):
    """A tree restored to an older state reuses an old copy; pruning must spare it.

    The reuse branch hands the model the archive that already holds this exact
    tree — which is now the *oldest* file on disk. Pruning by recency alone would
    delete the very path the reminder text names, so the live pointer is exempt.
    """
    from agent import kanban_budget_checkpoint as kbc
    from hermes_cli.kanban_db import task_attachments_dir

    workspace, _home = card_env
    (workspace / "a.txt").write_text("gen-0\n", encoding="utf-8")
    os.utime(workspace / "a.txt", ns=(1_700_000_000_000_000_000,) * 2)
    first = kbc.preserve_wip_tree(task_id="t_probe")
    assert first.mode == "archive"
    base_bytes = (workspace / "a.txt").read_bytes()

    for i in range(1, 6):
        (workspace / "a.txt").write_text(f"gen-{i}\n", encoding="utf-8")
        os.utime(workspace / "a.txt", ns=(1_700_000_000_000_000_000 + i * 10**9,) * 2)
        assert kbc.preserve_wip_tree(task_id="t_probe").mode == "archive"

    # Back to the first generation: the reuse branch names the oldest archive.
    (workspace / "a.txt").write_bytes(base_bytes)
    os.utime(workspace / "a.txt", ns=(1_700_000_000_000_000_000,) * 2)
    reused = kbc.preserve_wip_tree(task_id="t_probe")
    assert reused.mode == "archive"
    assert str(reused.locator) == str(first.locator)

    named = Path(reused.locator)
    assert named.exists(), "the copy the reminder names was pruned"
    assert named.with_name(named.name + ".done").exists()
    out_dir = task_attachments_dir("t_probe")
    assert len(list(out_dir.glob("wip-*.tar.gz"))) <= kbc._ARCHIVE_RETENTION + 1


def test_a_legacy_tarball_without_a_done_marker_is_not_reused(card_env):
    """Archives written before the marker existed are not provably complete.

    A tree identical to one of them gets a fresh, marked copy instead of being
    handed back a file whose completeness nobody recorded.
    """
    from agent import kanban_budget_checkpoint as kbc
    from hermes_cli.kanban_db import task_attachments_dir

    workspace, _home = card_env
    (workspace / "a.txt").write_text("A", encoding="utf-8")
    first = kbc.preserve_wip_tree(task_id="t_probe")
    out_dir = task_attachments_dir("t_probe")
    # Drop the marker: this is what an archive from the previous code looks like.
    legacy = [p for p in out_dir.glob("wip-*.tar.gz.done")]
    assert legacy, "a complete copy must carry a completeness marker"
    for marker in legacy:
        marker.unlink()

    legacy_archive = Path(first.locator)
    second = kbc.preserve_wip_tree(task_id="t_probe")
    assert second.mode == "archive"
    assert str(second.locator) != str(first.locator)
    assert legacy_archive.exists(), "the unmarked archive must be left alone, not deleted"
