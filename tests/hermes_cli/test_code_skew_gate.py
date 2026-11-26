"""The code-skew gate: split RPC contracts refuse a worker start; matching ones let it run.

26.11.2026: a gateway serving ``da0f60614069`` answered every new worker's ``usage.main`` as
``invalid_params`` (the worker sent ``prompt_cache_clock``), the receipt queue rolled back and each
turn died ~60 s in — five cards hit retry limit on one host condition. These tests keep the gate
honest in both directions: it must refuse a PROVEN split, and it must never refuse a healthy host.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

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


# --- extras live in the code, and the gate reads them there --------------------------------------

def _repo_root() -> "Path":
    return Path(__file__).resolve().parents[2]


def _read_repo(rel: str):
    try:
        return (_repo_root() / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _edition(drop: str = ""):
    """The repository's own sources, optionally with one field removed from the extras constant.

    Built from the REAL modules (not from invented names), so this follows the code: whatever the
    handler accepts is what the gate must compare.
    """
    usage = _read_repo("hermes_state_usage.py")
    assert usage, "hermes_state_usage.py must exist in the checkout"
    if drop:
        needle = f'"{drop}"'
        assert needle in usage, f"{drop} must be accepted by the real handler to test it"
        usage = usage.replace(needle, '"_replaced_placeholder_"', 1)

    def read(rel: str):
        if rel == "hermes_state_usage.py":
            return usage
        return _read_repo(rel)

    contract = gate.contract_from_sources(read)
    assert contract is not None, "the real checkout's sources must parse into a contract"
    return contract


def test_extras_are_read_from_the_code_not_a_literal():
    """``prompt_cache_clock`` is in the real handler's accepted set, and the gate sees it there.

    This is the D1 regression: the earlier revision declared an extras literal in the gate, never
    read it, and compared only ``_MODEL_USAGE_FIELDS`` — structurally blind to the very field the
    incident was about. Both halves are pinned: the gate parses the extras out of the source, and
    removing a field from that ONE constant changes the parsed contract.
    """
    assert not hasattr(gate, "_USAGE_MAIN_EXTRA_FIELDS"), \
        "a literal copy of the extras set in the gate goes stale; it must be parsed from the code"
    usage = _read_repo("hermes_state_usage.py")
    assert usage and "USAGE_MAIN_EXTRA_FIELDS" in usage, \
        "the handler's extras must be a named constant"
    full = _edition()
    assert "prompt_cache_clock" in full.usage_main_fields, \
        "the real handler accepts prompt_cache_clock, so the contract must list it"
    for field in ("pricing_version", "absolute", "task"):
        assert field in full.usage_main_fields, f"{field} is accepted by the real handler"
    assert "prompt_cache_clock" not in _edition(drop="prompt_cache_clock").usage_main_fields


def test_two_editions_differing_only_in_extras_are_not_mistaken_for_one_contract():
    """The field channel decides on the extras too: same model fields, different extras, different verdict.

    ``disk`` sends ``prompt_cache_clock`` and the gateway does not accept it: a refusal that names
    the field. The opposite order is an ordinary forward commit (gateway ahead) and must stay
    allowed — the requirement is a subset test, and refusing a gateway that accepts a superset would
    itself be the false refusal this gate is forbidden from making.
    """
    with_clock = _edition()
    without_clock = _edition(drop="prompt_cache_clock")
    assert with_clock.usage_main_fields != without_clock.usage_main_fields
    assert not (with_clock.usage_main_fields - without_clock.usage_main_fields
                - {"prompt_cache_clock"}), "the two editions differ ONLY in the extras field"

    problems = gate.compare_contracts(with_clock, without_clock, frozenset())
    assert problems, "a field set differing only in extras must produce a verdict"
    assert "prompt_cache_clock" in problems[0] and "usage.main" in problems[0]
    assert gate.compare_contracts(without_clock, with_clock, frozenset()) == ()


def test_the_real_incident_pair_is_refused_and_its_reverse_still_passes():
    """Disk ``34086e258353`` (accepts ``prompt_cache_clock``) vs gateway ``4713ca0ab46`` (does not).

    The pair the previous verdict walked through as PASSING. Skipped only when this checkout does
    not carry those objects (a shallow clone); never silently green without the objects present.
    """
    repo = _repo_root()
    def present(sha: str) -> bool:
        return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                              capture_output=True).returncode == 0
    if not (present("34086e258353") and present("4713ca0ab46")):
        pytest.skip("the incident commits are not in this checkout's object database")
    skew = gate.check_code_skew(disk_sha="34086e258353", gateway_sha="4713ca0ab46", repo=repo)
    assert skew is not None, "disk accepts a field the gateway rejects: this is the incident"
    assert "prompt_cache_clock" in skew.problems[0]
    # Reverse: the gateway accepts MORE than disk sends — a forward commit, not skew.
    assert gate.check_code_skew(disk_sha="4713ca0ab46", gateway_sha="34086e258353",
                                repo=repo) is None


def test_disk_contract_reads_the_extras_of_this_checkout():
    """``disk_contract()`` on the real checkout includes the named extras (D4: real modules only)."""
    disk = gate.disk_contract()
    assert disk is not None
    usage = _read_repo("hermes_state_usage.py")
    assert usage and "_MODEL_USAGE_FIELDS" in usage
    # every field the module names as an accepted extra is in the parsed contract
    assert {"pricing_version", "absolute", "task"} <= set(disk.usage_main_fields)
    assert "prompt_cache_clock" in disk.usage_main_fields


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


def test_sibling_branches_with_a_common_fork_are_not_diverged(tmp_path):
    """Two branches of one repository share history even though neither contains the other.

    The earlier revision answered "share no history" for exactly this pair (it tested ancestry
    in both directions instead of asking for a merge base) and refused a healthy host whose
    worker and gateway merely sat on different branches. A merge base exists: not divergence.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "fork point")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "worker-side")
    (repo / "w.txt").write_text("worker\n")
    _git(repo, "add", "w.txt")
    _git(repo, "commit", "-qm", "worker work")
    worker = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "master")
    (repo / "g.txt").write_text("gateway\n")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-qm", "gateway work")
    gateway = _git(repo, "rev-parse", "HEAD").strip()

    # Neither tip contains the other, yet both hang off one fork point.
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", worker, gateway],
                          capture_output=True).returncode == 1
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", gateway, worker],
                          capture_output=True).returncode == 1
    assert subprocess.run(["git", "-C", str(repo), "merge-base", worker, gateway],
                          capture_output=True, text=True).stdout.strip() == base
    assert gate._diverged(worker, gateway, repo) is False
    # The whole gate must not refuse: unparsable layouts + shared history == PASS.
    assert gate.check_code_skew(disk_sha=worker, gateway_sha=gateway, repo=repo) is None


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
