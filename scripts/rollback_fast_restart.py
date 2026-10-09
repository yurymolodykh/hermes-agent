#!/usr/bin/env python3
"""Rollback for the fast-restart feature (RGZ fast-gateway-restart, wave A).

Restores the pre-feature state installed by two commits:

    A1 = 069650ae863aee1b764a5f05bcc53bb6bb3dea7b  feat(gateway): fast restart path
    A2 = 0f7d8f52e7c583b08abcd1be23f45d31a74b0e33  feat(cron+kanban): resume tokens + generation-scoped claims

and returns the two agent config knobs to their pre-feature values:

    agent.restart_after_turn_timeout = 1800   (in-band /restart waits for active turns)
    agent.cron_drain_timeout          = 30    (cron jobs keep their drain budget)

USAGE
-----
    python scripts/rollback_fast_restart.py --check        # read-only: state + plan (default)
    python scripts/rollback_fast_restart.py --dry-run      # print exact commands, change nothing
    python scripts/rollback_fast_restart.py --apply        # revert commits + restore config
    python scripts/rollback_fast_restart.py --apply --restart-gateway   # ... then restart gateway

Safety rules
------------
* ``--apply`` requires a clean-ish working tree: if the revert target files carry
  uncommitted edits, the script refuses and shows how to stash/commit them first.
  (On the mission branch the tree legitimately carries other cards' uncommitted
  work — NEVER ``--force`` past that without explicitly stashing it.)
* The gateway is NEVER restarted unless ``--restart-gateway`` is passed. The
  default output only shows the exact restart + verification commands so a human
  can run them from a normal shell at a chosen moment. Use ``hermes gateway
  restart`` (no ``--fast``) — that IS the old behavior.
* This repo is the Live Hermes checkout; git revert is a real history change.
  Re-run ``--check`` before every apply so the plan reflects the current HEAD.

VERIFICATION (old behavior back)
--------------------------------
1.  ``hermes -p 01-orchestrator gateway restart --help`` no longer advertises ``--fast``.
2.  ``grep -n FAST_RESTART gateway/restart.py`` finds nothing (A1 removed the flag linkage).
3.  ``hermes -p 01-orchestrator config get agent.restart_after_turn_timeout`` -> 1800
    ``hermes -p 01-orchestrator config get agent.cron_drain_timeout``          -> 30
4.  Gateway log after a normal restart shows no ``FAST_RESTART`` json marker
    (``grep FAST_RESTART <profile>/logs/gateway.log`` empty on the new boot).
5.  A ``/restart`` issued in chat now waits for active turns (old restart_after_turn_timeout
    path) instead of the fast force-drain; after-turn wait is bounded by 1800s.

NOTE on commit order: A2 is a direct child of A1 (checked below), so the revert runs
A2 -> A1. Reverting the child first keeps each revert on the exact tree its inverse
expects, avoiding spurious conflicts.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # repo root (scripts/..)
PROFILE = "01-orchestrator"
A1 = "069650ae863aee1b764a5f05bcc53bb6bb3dea7b"
A2 = "0f7d8f52e7c583b08abcd1be23f45d31a74b0e33"
PRE_A1 = "41e15c24f5fe7036c0fe2e3ce20722ea9cee99ba"  # parent of A1 (pre-feature tip)

# Files that A1/A2 modified and that `git revert` will rewrite — used for the
# dirty-tree guard. (Full set from `git show --stat` of both commits.)
REVERT_FILES = {
    "gateway/restart.py",
    "gateway/run.py",
    "gateway/slash_commands.py",
    "hermes_cli/gateway.py",
    "hermes_cli/subcommands/gateway.py",
    "locales/en.yaml",
    "locales/ru.yaml",
    "cron/jobs.py",
    "cron/scheduler.py",
    "cron/scheduler_provider.py",
    "hermes_cli/kanban_db.py",
    "tests/gateway/test_fast_restart.py",
    "tests/gateway/test_restart_drain.py",
    "tests/gateway/test_restart_service_detection.py",
    "tests/cron/test_resume_tokens.py",
    "tests/cron/test_shutdown_interrupt.py",
    "tests/hermes_cli/test_generation_reclaim.py",
    "tests/tools/test_process_registry.py",
}


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.run(
        [str(c) for c in cmd],
        cwd=str(REPO),
        capture_output=capture,
        text=True,
        check=check,
    )


def git(*args: str) -> str:
    r = run(["git", *args], check=True)
    return r.stdout.strip()


def git_ok(*args: str) -> bool:
    """Run git, return True on exit 0 (e.g. `git merge-base --is-ancestor`)."""
    r = run(["git", *args], check=False)
    return r.returncode == 0


def check_repo() -> bool:
    """Read-only: confirm repo, commit topology, tree cleanliness, config values."""
    ok = True
    print(f"\n[1] Repository: {REPO}")
    if not (REPO / ".git").exists() and not (REPO / ".git").is_dir():
        print("  ERROR: no .git at repo root")
        return False

    head = git("rev-parse", "HEAD")
    print(f"  HEAD          : {head}")
    print(f"  A1 (fast path): {A1}")
    print(f"  A2 (resume+rc): {A2}")

    try:
        git("cat-file", "-e", A1 + "^{commit}")
        git("cat-file", "-e", A2 + "^{commit}")
    except subprocess.CalledProcessError:
        print("  ERROR: A1/A2 commits not present in this repo")
        return False

    # Topology: A2's parent must be A1, A1's parent must be PRE_A1, both ancestors of HEAD.
    parent_a2 = git("log", "-1", "--format=%P", A2)
    parent_a1 = git("log", "-1", "--format=%P", A1)
    print(f"  A2 parent     : {parent_a2}")
    print(f"  A1 parent     : {parent_a1}")
    if parent_a2 != A1:
        print(f"  ERROR: A2 parent is {parent_a2}, expected A1 ({A1})")
        ok = False
    if parent_a1 != PRE_A1:
        print(f"  ERROR: A1 parent is {parent_a1}, expected pre-feature {PRE_A1}")
        ok = False
    for c in (A1, A2):
        if not git_ok("merge-base", "--is-ancestor", c, "HEAD"):
            print(f"  ERROR: {c} is not an ancestor of HEAD")
            ok = False

    # Dirty-tree guard: any uncommitted change on revert target files?
    dirty = set(git("diff", "--name-only").splitlines()) | set(
        git("diff", "--cached", "--name-only").splitlines()
    )
    overlap = sorted(f for f in dirty if f in REVERT_FILES)
    if overlap:
        print(f"  WARNING: uncommitted changes on revert target files ({len(overlap)}):")
        for f in overlap:
            print(f"    - {f}")
        print("  `git revert` will refuse or conflict on these. Stash them FIRST")
        print("  (git stash push -- <file...>) and re-run; NEVER --force past them.")
    else:
        print("  OK: no uncommitted edits on revert target files")

    # Config values (read-only via hermes CLI on the target profile).
    cfg = shutil.which("hermes") or str(REPO / "venv" / "Scripts" / "hermes.exe")
    for key in ("agent.restart_after_turn_timeout", "agent.cron_drain_timeout"):
        try:
            r = subprocess.run(
                [cfg, "-p", PROFILE, "config", "get", key],
                capture_output=True, text=True, timeout=60, cwd=str(REPO),
            )
            val = r.stdout.strip()
            print(f"  config {key:<38}= {val}  (target in parentheses)")
        except FileNotFoundError as e:  # pragma: no cover
            print(f"  (cannot resolve hermes CLI: {e})")
    return ok


def enforce_clean_revert_files() -> bool:
    """Refuse --apply when the revert target files are dirty."""
    dirty = set(git("diff", "--name-only").splitlines()) | set(
        git("diff", "--cached", "--name-only").splitlines()
    )
    overlap = sorted(f for f in dirty if f in REVERT_FILES)
    if overlap:
        print("\nREFUSING --apply: dirty revert-target files. Stash them first, e.g.:")
        print("  git stash push -- " + " ".join(overlap))
        print("  git revert --no-edit " + A2)
        print("  git revert --no-edit " + A1)
        print("  git stash pop")
        return False
    return True


def plan() -> None:
    print("\n[2] Commands a human runs from the repo root (C:/Users/hermes/AppData/Local/hermes/hermes-agent):\n")
    print("  # a) If any revert-target file has uncommitted edits, stash them first:")
    print("  git stash push -m rollback-wip -- " + " ".join(sorted(REVERT_FILES)))
    print()
    print("  # b) Revert child-first (A2 then A1) so each inverse lands on the exact tree it expects:")
    print(f"  git revert --no-edit {A2}")
    print(f"  git revert --no-edit {A1}")
    print()
    print("  # c) Restore pre-feature config values on the gateway profile:")
    print(f"  hermes -p {PROFILE} config set agent.restart_after_turn_timeout 1800")
    print(f"  hermes -p {PROFILE} config set agent.cron_drain_timeout 30")
    print()
    print("  # d) Restart the gateway by the NORMAL path (this is the old behavior — no --fast):")
    print(f"  hermes -p {PROFILE} gateway restart")
    print()
    print("  # e) Verify old behavior /restart waits for turns instead of fast restart:")
    print(f"  hermes -p {PROFILE} gateway restart --help          # --fast must be GONE")
    print("  grep -n FAST_RESTART gateway/restart.py             # must print nothing")
    print(f"  hermes -p {PROFILE} config get agent.restart_after_turn_timeout   # -> 1800")
    print(f"  hermes -p {PROFILE} config get agent.cron_drain_timeout            # -> 30")
    print(f"  grep FAST_RESTART {os.path.expandvars('%LOCALAPPDATA%')}/hermes/profiles/{PROFILE}/logs/gateway.log   # new boot: empty")


def apply() -> None:
    if not enforce_clean_revert_files():
        sys.exit(2)
    print("\n[3] Applying revert (A2 -> A1) ...")
    git("revert", "--no-edit", A2)
    git("revert", "--no-edit", A1)
    head = git("rev-parse", "HEAD")
    print(f"  new HEAD: {head}")
    tip = git("log", "-1", "--format=%s")
    print(f"  tip msg : {tip}")
    print("\n[4] Restoring config values ...")
    cfg = shutil.which("hermes") or str(REPO / "venv" / "Scripts" / "hermes.exe")
    for key, val in (("agent.restart_after_turn_timeout", "1800"), ("agent.cron_drain_timeout", "30")):
        subprocess.run([cfg, "-p", PROFILE, "config", "set", key, val],
                       cwd=str(REPO), check=False)
    print("\n[5] Config restored. Restart + verify (run manually, see --dry-run output):")
    plan_tail = [l for l in plan.__doc__.splitlines() if l.strip()]
    print("  hermes -p %s gateway restart" % PROFILE)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="read-only state + plan (default)")
    ap.add_argument("--dry-run", action="store_true", help="print exact commands, change nothing")
    ap.add_argument("--apply", action="store_true", help="revert commits + restore config")
    ap.add_argument("--restart-gateway", action="store_true",
                    help="with --apply: also restart the gateway (normal path)")
    args = ap.parse_args()

    if args.apply:
        if not check_repo():
            return 1
        apply()
        if args.restart_gateway:
            print("\n[6] Restarting gateway by the normal path ...")
            cfg = shutil.which("hermes") or str(REPO / "venv" / "Scripts" / "hermes.exe")
            subprocess.run([cfg, "-p", PROFILE, "gateway", "restart"], cwd=str(REPO), check=False)
            print("  gateway restart issued. Verify per section VERIFICATION above.")
        else:
            print("\n[6] Gateway NOT restarted (no --restart-gateway). Restart it intentionally:")
            print("  hermes -p %s gateway restart   # normal path = old behavior" % PROFILE)
        return 0

    if not check_repo():
        return 1
    plan()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
