"""What a worker's environment needs before it may start: the card tree's dependency state.

Two responsibilities that both belong to the SPAWN side of a worker, not to its own boot:

* **Dependency-state bridge (the provisioning race).** A worker boots with ``cwd = workspace``, and
  under ``-m`` the interpreter puts ``""`` (the cwd) first on ``sys.path``, so ``import
  hermes_bootstrap`` resolves the module from the WORKSPACE tree. PM keys dependency state by that
  path (``pm.environments.install_key`` is ``sha256(realpath)``), so a worktree created seconds ago
  has a key of its own with nothing committed under it, and every worker in it dies at boot with
  ``NoCommittedEnvironment: no dependency environment is committed for this install`` — measured at
  60-72 s per attempt, i.e. on bootstrap, never on work. ``ops/bin/card-worktree-provision.py``
  bridges the two keys with a symlink, but from a TIMER, so the first spawns of a new tree lose the
  race. :func:`ensure_tree_install_state` is the same bridge on the spawn path: the dispatcher calls
  it once the workspace exists and before the worker starts.
* **Worker terminal budget.** :func:`worker_terminal_timeout_env` keeps a long-running card from
  having its own terminal calls cut off by the generic default first.

Every probe here asks PM's own committed selection (``pm.environments.committed_venv``) rather than
re-deriving anything, so "ready" means exactly what ``activate_dependencies`` will accept at boot.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Stable machine marker for "this tree cannot boot a worker yet". It is deliberately independent of
# the key pool and of any card-specific failure: the condition is the HOST's, not the card's.
UNREADY_REASON = "dependency_environment_unready"

# Keep a little wall-clock budget for the worker to observe a terminal timeout and make a terminal
# board call before ``max_runtime_seconds`` kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30


def worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


# A checkout's own ``hermes_bootstrap`` is the only thing that can shadow ours from a workspace: the
# worker's ``-m`` launch puts the cwd first on ``sys.path``, so the module that decides "which install
# owns my dependencies" comes from the workspace exactly when the workspace IS a Hermes tree.
_BOOTSTRAP_MODULE = "hermes_bootstrap.py"


def _running_root() -> Path:
    """The tree this module runs from: the dispatcher's own install."""
    return Path(__file__).resolve().parents[1]


def _dependency_source_root(start: Path) -> Optional[Path]:
    """The tree that owns *start*'s dependencies: the main checkout a linked worktree belongs to.

    A dispatcher started from a card worktree must bridge to the checkout ABOVE it, not to another
    bridge (a chain of links is one rename away from dangling). The anchoring is git's, not a walk up
    the filesystem: the common git dir of a linked worktree is ``<main checkout>/.git``. When *start*
    is not a linked worktree it is its own candidate.
    """
    from pm.environments import committed_venv

    candidates = [start.resolve()]
    common = _git_common_dir(candidates[0])
    if common is not None and common.name == ".git":
        candidates.insert(0, common.parent)
    for candidate in candidates:
        try:
            if committed_venv(candidate) is not None:
                return candidate
        except (OSError, RuntimeError):
            continue
    return None


def _git_common_dir(path: Path) -> Optional[Path]:
    """``git rev-parse --git-common-dir`` for *path*, absolute; ``None`` when it is not a repo."""
    import subprocess

    try:
        done = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out:
        return None
    return Path(out).resolve(strict=False)


def _committed_or_raise(tree: Path):
    from pm.environments import committed_venv

    return committed_venv(tree)


def _unready(tree: Path, detail: str) -> str:
    return (
        f"workspace: dependency environment for the card tree {tree} is not ready: {detail}"
        f" [{UNREADY_REASON}]"
    )


def ensure_tree_install_state(
    workspace: Path | str, *, source_root: Optional[Path] = None,
) -> tuple[bool, str]:
    """Make sure a worker started in *workspace* finds a committed dependency environment.

    Returns ``(True, "")`` when the worker may start — the tree is not a Hermes checkout (nothing in
    it shadows our bootstrap), it already has a committed environment of its own, or the bridge was
    created/looked correct. Returns ``(False, reason)`` when it may not: the caller must then keep the
    worker unspawned and report *reason* until the condition is fixed by an operator.

    Idempotent and non-destructive: an existing real directory or a link to another install is
    reported, never replaced — a card tree that points somewhere else is a decision, not a race.
    """
    tree = Path(workspace).resolve()
    try:
        shadows = (tree / _BOOTSTRAP_MODULE).is_file()
    except OSError as exc:
        return False, _unready(tree, f"cannot inspect the workspace: {exc}")
    if not shadows:
        return True, ""

    try:
        if _committed_or_raise(tree) is not None:
            return True, ""
    except (OSError, RuntimeError) as exc:
        return False, _unready(tree, f"its dependency record is unusable: {exc}")

    from pm.environments import install_key, install_state_dir, installs_root

    running = Path(source_root).resolve() if source_root else _running_root()
    source_tree = _dependency_source_root(running)
    if source_tree is None:
        return False, _unready(
            tree, f"the running install {running} has no committed dependency environment",
        )
    source = installs_root() / install_key(source_tree)
    if not source.is_dir():
        return False, _unready(tree, f"no dependency directory for {source_tree} ({source})")
    # Link the real directory, never another bridge: a dispatcher started from a card worktree must
    # still produce ONE hop to the install that owns the dependencies.
    try:
        source = source.resolve()
    except OSError as exc:
        return False, _unready(tree, f"cannot resolve {source}: {exc}")

    link = install_state_dir(tree)
    try:
        if link.is_symlink():
            current = Path(os.readlink(link)).resolve(strict=False)
            if current == source:
                pass  # already bridged
            elif not link.exists():
                link.unlink()  # dangling: points at an install that no longer exists
                link.symlink_to(source, target_is_directory=True)
            else:
                return False, _unready(
                    tree, f"it is linked to a different dependency directory: {current}",
                )
        elif link.exists():
            return False, _unready(
                tree, f"a real directory occupies {link} but holds no committed environment",
            )
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        return False, _unready(tree, f"cannot link {link} -> {source}: {exc}")

    try:
        if _committed_or_raise(tree) is None:
            return False, _unready(
                tree, f"{link} -> {source} exists but still yields no committed environment",
            )
    except (OSError, RuntimeError) as exc:
        return False, _unready(tree, f"its dependency record is unusable: {exc}")
    return True, ""
