"""scripts/check_card_artifacts.sh: card service artifacts are refused in the index.

Regression for the recurring leak into the product repository: ``.gitignore`` and
``.git/info/exclude`` do not apply to already-tracked paths, so workers kept
re-adding ``PROGRESS*.md``, ``durable/`` evidence and ``evidence-*/`` directories.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_card_artifacts.sh"


def _run(paths: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {k: v for k, v in os.environ.items() if k != "ALLOW_ARTIFACT_COMMIT"}
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *paths],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


@pytest.mark.parametrize(
    "path",
    [
        "PROGRESS.md",
        "PROGRESS-t_43a81a76.md",
        "docs/PROGRESS.md",
        "web/PROGRESS-t_abc.md",
        "durable/t_96d65020/REPORT.md",
        "durable/t_96d65020/evidence/green.txt",
        "evidence-t63278be8/probe.txt",
        "REPORT-fixes.md",
    ],
)
def test_artifact_paths_are_refused(path):
    result = _run([path])
    assert result.returncode == 1
    assert path in result.stderr
    assert "Отказ" in result.stderr


def test_refusal_names_where_the_artifact_belongs():
    result = _run(["PROGRESS.md"])
    text = result.stderr
    assert "~/.hermes/durable/kanban/card-artifacts/<task>/" in text
    assert "ALLOW_ARTIFACT_COMMIT=1" in text


@pytest.mark.parametrize(
    "path",
    [
        "hermes_cli/cli.py",
        "docs/REPORT.md",
        "optional-skills/security/oss-forensics/references/evidence-types.md",
        "tests/durable/test_x.py",
        "PROGRESSIVE.md",
        "README.md",
    ],
)
def test_product_paths_pass(path):
    result = _run([path])
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_mixed_paths_report_only_artifacts():
    result = _run(["hermes_cli/cli.py", "durable/t_1/REPORT.md", "PROGRESS.md"])
    assert result.returncode == 1
    assert "durable/t_1/REPORT.md" in result.stderr
    assert "PROGRESS.md" in result.stderr
    assert "hermes_cli/cli.py" not in result.stderr
    assert "найдено 2" in result.stderr


def test_bypass_flag_allows_the_commit():
    result = _run(["PROGRESS.md"], env={"ALLOW_ARTIFACT_COMMIT": "1"})
    assert result.returncode == 0
    assert "пропущена" in result.stderr


@pytest.mark.parametrize("value", ["0", "", "false"])
def test_any_other_bypass_value_still_refuses(value):
    result = _run(["PROGRESS.md"], env={"ALLOW_ARTIFACT_COMMIT": value})
    assert result.returncode == 1


def test_judges_the_index_when_no_paths_given(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            },
        )

    git("init", "-q")
    (tmp_path / "PROGRESS.md").write_text("x", encoding="utf-8")
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    git("add", "-A")

    staged = subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={k: v for k, v in os.environ.items() if k != "ALLOW_ARTIFACT_COMMIT"},
    )
    assert staged.returncode == 1
    assert "PROGRESS.md" in staged.stderr
    assert "app.py" not in staged.stderr

    # A commit that REMOVES the artifact must stay possible: deletions are not judged.
    git("commit", "-q", "-m", "seed", "--no-verify")
    git("rm", "-q", "--cached", "PROGRESS.md")
    removal = subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={k: v for k, v in os.environ.items() if k != "ALLOW_ARTIFACT_COMMIT"},
    )
    assert removal.returncode == 0, removal.stderr
