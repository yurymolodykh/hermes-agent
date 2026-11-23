"""repo_artifact_guard: card service artifacts are refused in the index (pre-commit gate)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import repo_artifact_guard as guard


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
        "nested\\PROGRESS.md",
    ],
)
def test_artifacts_are_detected(path):
    assert guard.is_card_artifact(path)


@pytest.mark.parametrize(
    "path",
    [
        "hermes_cli/repo_artifact_guard.py",
        "docs/REPORT.md",
        "optional-skills/security/oss-forensics/references/evidence-types.md",
        "tests/durable/test_x.py",
        "PROGRESSIVE.md",
        "README.md",
    ],
)
def test_product_paths_pass(path):
    assert not guard.is_card_artifact(path)


def test_bypass_is_explicit_only():
    assert not guard.bypass_requested({})
    assert not guard.bypass_requested({guard.BYPASS_ENV: "0"})
    assert guard.bypass_requested({guard.BYPASS_ENV: "1"})


def test_refusal_is_russian_and_lists_paths(capsys):
    assert guard.main(["hermes_cli/x.py", "durable/t_1/REPORT.md", "PROGRESS.md"]) == 1
    err = capsys.readouterr().err
    assert "Отказ" in err
    assert "durable/t_1/REPORT.md" in err
    assert "PROGRESS.md" in err
    assert guard.BYPASS_ENV in err


def test_clean_index_passes(capsys):
    assert guard.main(["hermes_cli/x.py", "tests/hermes_cli/test_x.py"]) == 0
    assert capsys.readouterr().err == ""


def test_bypass_env_allows_artifacts(monkeypatch, capsys):
    monkeypatch.setenv(guard.BYPASS_ENV, "1")
    assert guard.main(["PROGRESS.md"]) == 0
    assert "пропущена" in capsys.readouterr().err


def test_staged_paths_reads_the_index(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "PROGRESS.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "PROGRESS.md"], cwd=tmp_path, check=True)
    assert guard.staged_paths(tmp_path) == ["PROGRESS.md"]
    # Deleted paths are not artifacts in the index: removal must stay possible.
    subprocess.run(["git", "commit", "-q", "-m", "x", "--no-verify"], cwd=tmp_path, check=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "PATH": os.environ.get("PATH", "")})
    subprocess.run(["git", "rm", "-q", "--cached", "PROGRESS.md"], cwd=tmp_path, check=True)
    assert guard.staged_paths(tmp_path) == []
