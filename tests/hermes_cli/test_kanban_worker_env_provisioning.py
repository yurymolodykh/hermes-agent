"""The dispatcher must not start a worker in a tree whose dependencies are not reachable.

A worker boots with ``cwd = workspace``; under ``-m`` the cwd is ``sys.path[0]``, so a Hermes tree
in the workspace supplies its own ``hermes_bootstrap`` and therefore its own PM install key. A fresh
card worktree's key holds nothing until ``~/.hermes/installs/<key(tree)>`` is linked to the main
tree's install directory, and until that link exists every worker in the tree dies on bootstrap with
``no dependency environment is committed for this install`` after 60-72 s — spending the card's
failure budget on a host condition. The link used to be made by a timer
(``ops/bin/card-worktree-provision.py``), which loses the race against the first spawn.

These tests pin the two guarantees of the spawn-path fix:
* the bridge exists AT THE MOMENT the spawn is invoked (not "eventually"), and the tree then
  resolves a committed environment exactly as ``activate_dependencies`` would;
* when the bridge cannot be made, NO spawn happens, the card keeps its status, and
  ``consecutive_failures`` does not grow.

``_dependency_source_root`` is redirected to the temporary checkout in the dispatch tests: a test
must never read the real dispatcher's own install record (``tests/home_io_guard.py`` refuses I/O
against the real home), and "which install do I bridge to" is a separate, separately tested question
(:func:`test_source_root_is_the_main_checkout_of_a_linked_worktree`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_worker_env as kbenv
from pm import environments as pm_env

TREE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def _main_repo(tmp_path: Path) -> Path:
    """A checkout-shaped repo: a git root carrying the REAL ``hermes_bootstrap``.

    The real module is what refuses to boot an uncommitted tree, so copying it is what makes the
    child probe below exercise production behaviour instead of a placeholder.
    """
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )
    (repo / "hermes_bootstrap.py").write_bytes((TREE_ROOT / "hermes_bootstrap.py").read_bytes())
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _commit_install(home: Path, tree: Path) -> Path:
    """Give *tree* a committed dependency environment the way PM records one."""
    state = home / "installs" / pm_env.install_key(tree)
    venv = state / "environments" / "gen-test" / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    version = pm_env.venv_python_version(venv) or (sys.version_info.major, sys.version_info.minor)
    (venv / "lib" / f"python{version[0]}.{version[1]}" / "site-packages").mkdir(parents=True, exist_ok=True)
    (state / "facts.json").write_text(
        json.dumps({"schema": 1, "packages": {"venv": {"environment": str(venv)}}}), encoding="utf-8",
    )
    return venv


def _worktree(repo: Path, task_id: str) -> Path:
    target = repo / ".worktrees" / task_id
    _git(repo, "worktree", "add", str(target), "-b", f"wt/{task_id}", "HEAD")
    return target


def _dispatch(conn, spawn_fn):
    return kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_spawn=None)


def _source_root(monkeypatch, repo: Path) -> None:
    """Stand in for the dispatcher's own install: the temporary checkout owns the dependencies."""
    monkeypatch.setattr(kbenv, "_dependency_source_root", lambda _start: repo)


def _bridge(conn, task_id: str, repo: Path, spawn_fn=None):
    """Dispatch one worktree card and return ``(result, workspace, link_path)``."""
    res = kbd.dispatch_once(
        conn, spawn_fn=spawn_fn or (lambda task, workspace, board=None: 4242), max_spawn=None,
    )
    workspace = repo / ".worktrees" / task_id
    return res, workspace, res


# ---------------------------------------------------------------------------
# 1. The bridge is on the spawn path
# ---------------------------------------------------------------------------


def test_spawn_bridges_the_install_link_before_the_worker_starts(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """At the instant the spawn happens the tree already resolves a committed environment."""
    repo = _main_repo(kanban_home.parent)
    _source_root(monkeypatch, repo)
    main_venv = _commit_install(kanban_home, repo)
    seen: dict = {}

    def spawn(task, workspace, board=None):
        # The dispatcher's own contract: the tree must be bootable NOW.
        link = kanban_home / "installs" / pm_env.install_key(Path(workspace))
        seen["link"] = link.is_symlink()
        seen["link_exists"] = link.exists()
        seen["committed"] = pm_env.committed_venv(Path(workspace))
        return 4242

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fresh worktree", assignee="alice",
                             workspace_kind="worktree",
                             workspace_path=str(repo / ".worktrees" / "t_fresh"))
        res = _dispatch(conn, spawn)
        # The dispatcher names the tree after the card; the link follows from that path.
        tree = Path(res.spawned[0][2]) if res.spawned else repo / ".worktrees" / tid
        link = kanban_home / "installs" / pm_env.install_key(tree)

    assert [s[0] for s in res.spawned] == [tid]
    assert seen["link"] is True, "the install link must exist when the worker is spawned"
    assert seen["link_exists"] is True, "the bridge must not dangle"
    assert Path(link).resolve() == (kanban_home / "installs" / pm_env.install_key(repo)).resolve()
    # Exactly what hermes_bootstrap's activate_dependencies selects at boot.
    assert seen["committed"] is not None
    assert Path(seen["committed"]) == main_venv


def test_worker_boot_after_the_bridge_does_not_raise_no_committed_environment(kanban_home, tmp_path):
    """The real boot path (the STORE python, cwd=card tree) gets past PM instead of raising.

    Only a store (non-venv) interpreter refuses to boot on an uncommitted install, and only when it
    lives under that install's store root — so the probe runs on ``sys._base_executable`` with the
    temporary home's ``tools/`` pointing at the real store, exactly the layout ``hermes -m`` gives a
    worker.
    """
    store_python = getattr(sys, "_base_executable", None)
    if not store_python or not Path(store_python).exists():
        pytest.skip("no store interpreter available to reproduce the bootstrap refusal")
    # ``<store>/python-X/bin/pythonY`` -> ``<store>``; reading it via PM would touch the real home.
    real_store = Path(store_python).resolve().parents[2]
    (kanban_home / "tools").symlink_to(real_store, target_is_directory=True)

    repo = _main_repo(tmp_path)
    _commit_install(kanban_home, repo)
    tree = _worktree(repo, "t_boot")
    assert pm_env.committed_venv(tree) is None, "precondition: the tree has no environment yet"
    env = {**os.environ, "PYTHONPATH": str(TREE_ROOT), "HERMES_HOME": str(kanban_home)}

    # Before the bridge the worker's own boot raises exactly the reported error (red baseline).
    before = subprocess.run(
        [str(store_python), "-c", "import hermes_bootstrap"],
        cwd=str(tree), env=env, capture_output=True, text=True, timeout=180, check=False,
        stdin=subprocess.DEVNULL,
    )
    assert "no dependency environment is committed" in (before.stdout + before.stderr), (
        "red baseline: an unbridged tree must refuse to boot"
    )

    ready, reason = kbenv.ensure_tree_install_state(tree, source_root=repo)
    assert ready, reason

    probe = (
        "import hermes_bootstrap\n"
        "from pathlib import Path\n"
        "from pm.environments import committed_venv\n"
        "print('BOOT', Path(hermes_bootstrap._root))\n"
        "print('VENV', committed_venv(Path(hermes_bootstrap._root)))\n"
    )
    after = subprocess.run(
        [str(store_python), "-c", probe],
        cwd=str(tree), env=env, capture_output=True, text=True, timeout=180, check=False,
        stdin=subprocess.DEVNULL,
    )
    combined = after.stdout + after.stderr
    assert "no dependency environment is committed" not in combined, combined
    assert after.returncode == 0, combined
    # The tree still owns the import (cwd wins over PYTHONPATH) — the fix does not defeat isolation.
    assert f"BOOT {tree}" in after.stdout, combined
    assert "gen-test" in after.stdout, combined


def test_existing_correct_link_is_left_alone(kanban_home):
    """Idempotent: a second dispatch tick writes nothing and still reports ready."""
    repo = _main_repo(kanban_home.parent)
    _commit_install(kanban_home, repo)
    tree = _worktree(repo, "t_twice")

    assert kbenv.ensure_tree_install_state(tree, source_root=repo)[0] is True
    link = kanban_home / "installs" / pm_env.install_key(tree)
    inode = link.lstat().st_ino

    assert kbenv.ensure_tree_install_state(tree, source_root=repo)[0] is True
    assert link.lstat().st_ino == inode, "the bridge must not be re-created on every tick"


def test_dangling_link_is_replaced(kanban_home):
    """A link to an install that no longer exists is repaired, not treated as a decision."""
    repo = _main_repo(kanban_home.parent)
    main_venv = _commit_install(kanban_home, repo)
    tree = _worktree(repo, "t_dangling")
    link = kanban_home / "installs" / pm_env.install_key(tree)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(kanban_home / "installs" / "gone", target_is_directory=True)

    assert kbenv.ensure_tree_install_state(tree, source_root=repo)[0] is True
    assert Path(link).resolve() == main_venv.parents[2].resolve()


def test_tree_without_a_hermes_checkout_needs_no_bridge(kanban_home, tmp_path):
    """A scratch directory has nothing that shadows our bootstrap, so nothing is linked."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert kbenv.ensure_tree_install_state(plain, source_root=_main_repo(tmp_path)) == (True, "")
    assert not (kanban_home / "installs" / pm_env.install_key(plain)).exists()


def test_source_root_is_the_main_checkout_of_a_linked_worktree(kanban_home, tmp_path):
    """A dispatcher started from a card worktree bridges to the checkout above it, not to a chain."""
    repo = _main_repo(tmp_path)
    _commit_install(kanban_home, repo)
    tree = _worktree(repo, "t_source")
    assert kbenv._dependency_source_root(tree) == repo.resolve()


# ---------------------------------------------------------------------------
# 2. No bridge possible → no spawn, no failure budget spent
# ---------------------------------------------------------------------------


def test_no_spawn_when_the_running_install_has_no_dependencies(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    """No committed environment in the source install → the card is held, not crashed."""
    repo = _main_repo(tmp_path)  # note: nothing committed for it
    _source_root(monkeypatch, repo)
    spawns: list[str] = []

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unprovisionable", assignee="alice",
                             workspace_kind="worktree",
                             workspace_path=str(repo / ".worktrees" / "t_nodeps"))
        res = _dispatch(conn, lambda task, workspace, board=None: spawns.append(task.id) or 1)
        task = kb.get_task(conn, tid)
        events = [e.kind for e in kb.list_events(conn, tid)]

    assert task is not None
    assert spawns == [], "a worker must not be started in a tree that cannot boot"
    assert res.spawned == []
    assert [t for t, _reason in res.skipped_env_unready] == [tid]
    assert kbenv.UNREADY_REASON in res.skipped_env_unready[0][1]
    assert task.status == "ready", "the card waits for the host, it is not blocked"
    assert (task.consecutive_failures or 0) == 0, "a host condition must not spend the card's budget"
    assert "spawn_failed" in events


def test_no_spawn_when_the_link_cannot_be_created(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A read-only installs directory (no permission) also gives a reason, never a crash-loop."""
    repo = _main_repo(kanban_home.parent)
    _source_root(monkeypatch, repo)
    _commit_install(kanban_home, repo)
    spawns: list[str] = []

    def refuse(self, target, target_is_directory=False):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "symlink_to", refuse)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unwritable link", assignee="alice",
                             workspace_kind="worktree",
                             workspace_path=str(repo / ".worktrees" / "t_nowrite"))
        res = _dispatch(conn, lambda task, workspace, board=None: spawns.append(task.id) or 1)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert spawns == []
    assert [t for t, _reason in res.skipped_env_unready] == [tid]
    assert "Permission denied" in res.skipped_env_unready[0][1]
    assert task.status == "ready"
    assert (task.consecutive_failures or 0) == 0


def test_env_unready_is_named_in_the_suppression_summary(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    """``describe_suppression`` reports the hold, so a held card is not read as a stall."""
    repo = _main_repo(tmp_path)
    _source_root(monkeypatch, repo)
    with kbc.connect() as conn:
        kb.create_task(conn, title="held", assignee="alice",
                       workspace_kind="worktree", workspace_path=str(repo / ".worktrees" / "t_held"))
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, max_spawn=None)
    assert "env_unready=1" in kbd.describe_suppression([res])


def test_unready_card_is_retried_once_the_environment_appears(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    """The hold is a wait, not a block: once deps exist the card spawns on the next tick."""
    repo = _main_repo(tmp_path)
    _source_root(monkeypatch, repo)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="later", assignee="alice",
                             workspace_kind="worktree",
                             workspace_path=str(repo / ".worktrees" / "t_later"))
        held = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, max_spawn=None)
        assert [t for t, _ in held.skipped_env_unready] == [tid]

        _commit_install(kanban_home, repo)  # the host catches up (pm install / repair)
        spawns: list[str] = []
        again = kbd.dispatch_once(
            conn, spawn_fn=lambda task, workspace, board=None: spawns.append(task.id) or 7,
            max_spawn=None,
        )

    assert spawns == [tid]
    assert [s[0] for s in again.spawned] == [tid]
