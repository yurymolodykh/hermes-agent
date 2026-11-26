"""The code-skew gate: split RPC contracts refuse a worker start; matching ones let it run.

26.11.2026: a gateway serving ``da0f60614069`` answered every new worker's ``usage.main`` as
``invalid_params`` (the worker sent ``prompt_cache_clock``), the receipt queue rolled back and each
turn died ~60 s in — five cards hit retry limit on one host condition. These tests keep the gate
honest in both directions: it must refuse a PROVEN split, and it must never refuse a healthy host.
"""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import code_skew_gate as gate

OLD_FIELDS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens"})
NEW_FIELDS = OLD_FIELDS | {"prompt_cache_clock"}
OPS = frozenset({"usage.main", "session.append", "transcript.append"})


def _skew(disk_fields, gateway_fields=OLD_FIELDS, ops=OPS, required=OPS):
    disk = gate.RpcContract(usage_main_fields=frozenset(disk_fields), operations=frozenset(ops))
    gateway = gate.RpcContract(usage_main_fields=frozenset(gateway_fields),
                               operations=frozenset(ops))
    return gate.compare_contracts(disk, gateway, frozenset(required))


# --- the incident, exactly ------------------------------------------------------------------------

def test_new_field_sent_to_an_old_gateway_is_refused():
    """The 26.11.2026 shape: disk sends ``prompt_cache_clock``, the gateway's field set lacks it."""
    problems = _skew(NEW_FIELDS)
    assert problems, "a field the gateway does not accept must be reported"
    assert "prompt_cache_clock" in problems[0]
    assert "usage.main" in problems[0]


def test_matching_contracts_let_the_worker_run():
    """Versions agree: nothing to report, the worker starts."""
    assert _skew(OLD_FIELDS) == ()
    assert _skew(NEW_FIELDS, NEW_FIELDS) == ()


def test_gateway_ahead_of_disk_is_not_skew():
    """A gateway that accepts a superset serves older workers fine — an ordinary forward commit."""
    assert _skew(OLD_FIELDS, NEW_FIELDS) == ()


def test_missing_operation_is_refused():
    """A dispatched ``worker.persist`` operation the gateway cannot route is a split too."""
    problems = _skew(OLD_FIELDS, OLD_FIELDS, ops=OPS, required=OPS | {"turn.release"})
    assert problems and "turn.release" in problems[0]


# --- the refusal text is actionable ---------------------------------------------------------------

def test_refusal_message_names_both_commits_and_the_fix():
    skew = gate.CodeSkew(
        disk_sha="8fc03744473d4f4c2f0b6bdbd0d6d2e2f0b0f4c1",
        gateway_sha="da0f60614069c1a2b3c4d5e6f7a8b9c0d1e2f3a4",
        problems=("usage.main on disk sends `prompt_cache_clock`, which the gateway rejects",),
        gateway_pid=1234,
    )
    assert "8fc037444" in skew.message and "da0f606140" in skew.message
    assert "gateway pid 1234" in skew.message
    assert "hermes gateway restart" in skew.message
    # the card's wording: the refusal must say the code does not match, not merely "error"
    assert "does not match the running gateway" in skew.message


# --- an unknowable side NEVER refuses -------------------------------------------------------------

def test_unknown_gateway_sha_passes():
    """No advertised endpoint (a cron job, a fresh host): the gate stays out of the way."""
    assert gate.check_code_skew(disk_sha="a" * 40, gateway_sha=None) is None


def test_unknown_disk_sha_passes():
    assert gate.check_code_skew(disk_sha=None, gateway_sha="b" * 40) is None


def test_same_commit_passes_without_reading_any_file():
    sha = "c" * 40
    assert gate.check_code_skew(disk_sha=sha, gateway_sha=sha) is None


def test_env_escape_is_honoured(monkeypatch):
    """``HERMES_SKIP_CODE_SKEW_GATE`` is the documented opt-out for a host mid-update."""
    monkeypatch.setenv("HERMES_SKIP_CODE_SKEW_GATE", "1")
    assert gate.check_code_skew(gateway_sha="d" * 40) is None


# --- unparsable layouts fall back to proven divergence only ----------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_shared_history_with_unreadable_layouts_is_not_skew(tmp_path):
    """Two real commits in one history: without parsed contracts the gate must not guess."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "one")
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("2\n")
    _git(repo, "commit", "-qam", "two")
    second = _git(repo, "rev-parse", "HEAD").strip()
    assert gate._diverged(first, second, repo) is False
    assert gate.check_code_skew(disk_sha=second, gateway_sha=first, repo=repo) is None


def test_disjoint_histories_refuse(tmp_path):
    """No shared ancestor and no parsable contract: the one case the fallback may refuse."""
    defs = []
    for name in ("one", "two"):
        repo = tmp_path / name
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "t")
        (repo / "a.txt").write_text(name + "\n")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-qm", name)
        defs.append(_git(repo, "rev-parse", "HEAD").strip())
    # Both commits are objects in the SECOND repo only if fetched; graft one in to make the
    # objects readable but unrelated.
    repo = tmp_path / "two"
    _git(tmp_path / "one", "remote", "add", "origin", str(repo))
    _git(repo, "remote", "add", "one", str(tmp_path / "one"))
    _git(repo, "fetch", "-q", "one")
    skew = gate.check_code_skew(disk_sha=defs[1], gateway_sha=defs[0], repo=repo)
    assert skew is not None
    assert "share no history" in skew.problems[0]


# --- live host: the gate agrees with the running gateway -------------------------------------------

@pytest.mark.parametrize("marker", ["ok"])
def test_live_host_contracts_parse(marker):
    """The gate must be able to read BOTH sides on this host, or it silently never refuses."""
    disk = gate.disk_contract()
    assert disk is not None, "the disk contract must parse from this checkout"
    assert disk.usage_main_fields, "usage.main must be non-empty"
    assert disk.operations, "worker.persist operations must be non-empty"
    commit = gate.disk_code_sha()
    assert commit and len(commit) >= 7
    assert gate.commit_contract(commit) is not None, "the committed side must parse too"
