"""The card-artifact gate is wired into the VERSIONED hooks, not just the local `.git/hooks`.

`scripts/check_card_artifacts.sh` is the rule; this file proves the wiring that makes it
live on a fresh clone: `lefthook.yml` declares the job, `just artifact-guard` runs the
script against the real index, and `just hooks-install` (scripts/install_git_hooks.sh)
refuses to claim success without an installed hook. Without this, the gate could be
deleted from `lefthook.yml` — or the install recipe dropped — and every commit would pass
while the tests of the script itself stayed green.

Placement note: the mechanism spans `lefthook.yml` + `justfile` + `scripts/`, so the file
lives where the card that introduced the gate asked for it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("just") is None or shutil.which("git") is None,
    reason="needs `just` and `git` on PATH to drive the real recipe",
)


def _index_with(tmp_path: Path, *paths: str) -> Path:
    """A private index holding ``paths``, so the repo's own index is never touched."""
    index = tmp_path / "index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    env.pop("ALLOW_ARTIFACT_COMMIT", None)
    for path in paths:
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=REPO, input="probe\n", capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
            cwd=REPO, capture_output=True, text=True, check=True, env=env,
        )
    return index


def _just_artifact_guard(index: Path, extra_env: dict[str, str] | None = None):
    env = {**os.environ, "GIT_INDEX_FILE": str(index), **(extra_env or {})}
    env.pop("ALLOW_ARTIFACT_COMMIT", None) if not extra_env else None
    return subprocess.run(
        ["just", "artifact-guard"],
        cwd=REPO, capture_output=True, text=True, env=env, check=False,
    )


def test_recipe_refuses_an_artifact_in_the_index(tmp_path):
    result = _just_artifact_guard(_index_with(tmp_path, "PROGRESS.md", "durable/t_1/REPORT.md"))
    assert result.returncode != 0
    assert "PROGRESS.md" in result.stderr + result.stdout
    assert "durable/t_1/REPORT.md" in result.stderr + result.stdout


def test_recipe_passes_a_clean_index(tmp_path):
    result = _just_artifact_guard(_index_with(tmp_path, "hermes_cli/cli.py"))
    assert result.returncode == 0, result.stderr + result.stdout


def test_recipe_honours_the_explicit_bypass(tmp_path):
    result = _just_artifact_guard(
        _index_with(tmp_path, "PROGRESS.md"), {"ALLOW_ARTIFACT_COMMIT": "1"}
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_lefthook_declares_the_guard_job():
    """The hook chain is data: no job in lefthook.yml means no gate on any commit."""
    text = (REPO / "lefthook.yml").read_text(encoding="utf-8")
    pre_commit = text.split("pre-commit:", 1)[1].split("\n\n", 1)[0]
    assert "just artifact-guard" in pre_commit
    assert text.index("guard card artifacts") < text.index("lint python")


def test_install_recipe_serves_the_unversioned_hooks_dir():
    """`.git/hooks/` is not versioned: the recipe must exist and be wired to the script."""
    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    assert "hooks-install:" in justfile
    script = REPO / "scripts" / "install_git_hooks.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK), "install script must be executable"
    assert "lefthook install" in script.read_text(encoding="utf-8")


def test_install_script_reports_the_real_hook_path():
    result = subprocess.run(
        ["bash", str(REPO / "scripts" / "install_git_hooks.sh")],
        cwd=REPO, capture_output=True, text=True, check=False,
        env={k: v for k, v in os.environ.items() if k != "ALLOW_ARTIFACT_COMMIT"},
    )
    assert result.returncode == 0, result.stderr
    hooks_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert f"{hooks_dir}/pre-commit" in result.stdout
