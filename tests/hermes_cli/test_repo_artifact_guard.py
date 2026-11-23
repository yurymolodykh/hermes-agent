"""The card-artifact gate is wired into the VERSIONED hooks, not just the local `.git/hooks`.

`scripts/check_card_artifacts.sh` is the rule (tests/scripts/test_card_artifacts_guard.py);
this file proves the wiring that keeps it live on a fresh clone: `lefthook.yml` declares the
job, `just artifact-guard` runs the script against the real index, and `just hooks-install`
(scripts/install_git_hooks.sh) reports the hook it armed. Without this the gate could be
dropped from `lefthook.yml` — or the install recipe removed — and every commit would pass
while the script's own tests stayed green.

The failure this closes: `.git/hooks/` is NOT versioned, so a gate armed only by hand in one
working copy disappears on a fresh clone or after the repository is re-created.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("just") is None or shutil.which("git") is None,
    reason="needs `just` and `git` on PATH to drive the real recipe",
)


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "ALLOW_ARTIFACT_COMMIT"}
    env.update(extra)
    return env


def _index_with(tmp_path: Path, *paths: str) -> Path:
    """A private index holding ``paths``, so the repository's own index is never touched."""
    index = tmp_path / "index"
    env = _clean_env(GIT_INDEX_FILE=str(index))
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


def _artifact_guard(index: Path, **extra_env: str):
    return subprocess.run(
        ["just", "artifact-guard"],
        cwd=REPO, capture_output=True, text=True, check=False,
        env=_clean_env(GIT_INDEX_FILE=str(index), **extra_env),
    )


def test_recipe_refuses_an_artifact_in_the_index(tmp_path):
    result = _artifact_guard(_index_with(tmp_path, "PROGRESS.md", "durable/t_1/REPORT.md"))
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "PROGRESS.md" in output
    assert "durable/t_1/REPORT.md" in output


def test_recipe_passes_a_clean_index(tmp_path):
    result = _artifact_guard(_index_with(tmp_path, "hermes_cli/cli.py"))
    assert result.returncode == 0, result.stderr + result.stdout


def test_recipe_honours_the_explicit_bypass(tmp_path):
    result = _artifact_guard(_index_with(tmp_path, "PROGRESS.md"), ALLOW_ARTIFACT_COMMIT="1")
    assert result.returncode == 0, result.stderr + result.stdout


def test_lefthook_runs_the_guard_first_in_pre_commit():
    """The hook chain is configuration data: a job that runs before the lints, or no gate."""
    hooks = yaml.safe_load((REPO / "lefthook.yml").read_text(encoding="utf-8"))
    jobs = hooks["pre-commit"]["jobs"]
    names = [job["name"] for job in jobs]
    assert names[0] == "guard card artifacts", names
    assert jobs[0]["run"] == "just artifact-guard"


def test_install_recipe_arms_the_unversioned_hooks_dir():
    """`.git/hooks/` is not versioned, so the install path has to be a versioned recipe."""
    listed = subprocess.run(
        ["just", "--list"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    assert "hooks-install" in listed
    script = REPO / "scripts" / "install_git_hooks.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK), "install script must be executable"


def test_install_script_reports_the_hook_it_armed():
    result = subprocess.run(
        ["bash", str(REPO / "scripts" / "install_git_hooks.sh")],
        cwd=REPO, capture_output=True, text=True, check=False, env=_clean_env(),
    )
    assert result.returncode == 0, result.stderr
    hooks_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert f"{hooks_dir}/pre-commit" in result.stdout
