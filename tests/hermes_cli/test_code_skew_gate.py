"""The code-skew gate: split RPC contracts refuse a worker start; matching ones let it run.

26.11.2026: a gateway serving ``da0f60614069`` answered every new worker's ``usage.main`` as
``invalid_params`` (the worker sent ``prompt_cache_clock``), the receipt queue rolled back and each
turn died ~60 s in — five cards hit retry limit on one host condition. These tests keep the gate
honest in both directions: it must refuse a PROVEN split, and it must never refuse a healthy host.
"""
from __future__ import annotations

import ast
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

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_repo(rel: str):
    try:
        return (_repo_root() / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# A field that exists in no revision: the two editions of the handler differ ONLY in this extra, so
# the helper stays usable before AND after the contract grew a real ``prompt_cache_clock`` field.
_PROBE_EXTRA = "probe_extra_field"


def _with_probe_extra(runtime: str, extra: str = _PROBE_EXTRA) -> str:
    """Return ``runtime`` with one field added to the ``usage.main`` accepted set.

    Addressed by CONDITION, never by position: the gate's own parser finds the ``_worker_usage``
    main branch (``allowed = X if auxiliary else Y``, or ``if auxiliary: X`` / ``else: Y``) and the
    field goes at the END of exactly that expression. Touching the first ``allowed =`` line in file
    order breaks as soon as the contract grows: on the t_7403b0fe revision the first such line with
    ``_MODEL_USAGE_FIELDS`` is the AUXILIARY branch, and an injected real field there is a no-op.
    """
    tree = ast.parse(runtime)
    handler = next((node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_worker_usage"), None)
    assert handler is not None, "hermes_state_runtime must declare _worker_usage"
    expr = gate._main_allowed_expr(handler)
    assert expr is not None and getattr(expr, "end_col_offset", None) is not None, \
        "_worker_usage must build its usage.main accepted set as a named expression"
    # AST offsets are UTF-8 byte offsets, so splice bytes and decode once.
    lines = runtime.encode("utf-8").splitlines(keepends=True)
    index = expr.end_lineno - 1
    column = expr.end_col_offset
    lines[index] = lines[index][:column] + f" | {{{extra!r}}}".encode() + lines[index][column:]
    return b"".join(lines).decode("utf-8")


def _edition(extra: str = "") -> gate.RpcContract:
    """A contract built from THIS checkout's real modules, with one field added to the handler's set.

    Nothing is hand-written: the field set comes from ``hermes_state_usage._MODEL_USAGE_FIELDS`` and
    from the accepted-set expression inside the real ``_worker_usage``. ``extra`` injects one more
    field into the ``usage.main`` branch of that expression — the shape of the commit that added
    ``prompt_cache_clock`` — so the two editions differ ONLY in the extras, exactly as the incident
    pair did.
    """
    usage = _read_repo("hermes_state_usage.py")
    runtime = _read_repo("hermes_state_runtime.py")
    assert usage and runtime, "the real modules must exist in this checkout"
    if extra:
        runtime = _with_probe_extra(runtime, extra)

    def read(rel):
        if rel == "hermes_state_usage.py":
            return usage
        if rel == "hermes_state_runtime.py":
            return runtime
        return _read_repo(rel)

    contract = gate.contract_from_sources(read)
    assert contract is not None, "the real checkout's sources must parse into a contract"
    return contract


def test_extras_are_read_from_the_code_not_a_literal():
    """The gate carries no extras literal and the parsed set contains the handler's extras.

    D1 regression: the earlier revision declared ``_USAGE_MAIN_EXTRA_FIELDS`` in the gate, never
    read it, and parsed only ``_MODEL_USAGE_FIELDS`` — structurally blind to the field that caused
    the incident. Extras must come from the code, so adding one field to the handler's expression
    changes the parsed contract without touching the gate.

    The probe field is a name no revision accepts, so this holds before AND after the contract grew
    a real ``prompt_cache_clock``: the helper's job is "one extra reaches usage.main", not "this
    particular field is new".
    """
    assert not hasattr(gate, "_USAGE_MAIN_EXTRA_FIELDS"), \
        "a literal copy of the extras set in the gate goes stale; it must be parsed from the code"
    plain = _edition()
    with_probe = _edition(_PROBE_EXTRA)
    assert _PROBE_EXTRA not in plain.usage_main_fields
    assert _PROBE_EXTRA in with_probe.usage_main_fields, \
        "a field added to the handler's accepted set must appear in the parsed contract"
    # the extras that already exist in this checkout came out of the same expression
    assert with_probe.usage_main_fields - plain.usage_main_fields == {_PROBE_EXTRA}


def test_the_probe_field_lands_in_the_usage_main_branch_not_the_auxiliary_one():
    """The helper addresses the ``usage.main`` branch by condition, never by file order.

    t_7403b0fe split the handler into an auxiliary/main pair; the first ``allowed =`` line that
    names ``_MODEL_USAGE_FIELDS`` in that shape is the AUXILIARY branch. Injecting there changed
    nothing the gate parses, so the "extras are read from the code" claim stopped being exercised —
    a silent loss of the test, not a code regression.

    Shape-agnostic on purpose (``X if auxiliary else Y`` and ``if auxiliary: X / else: Y`` are both
    real revisions of this handler): the probe must appear exactly ONCE, inside the span the gate's
    own parser reports as the ``usage.main`` expression.
    """
    runtime_source = _read_repo("hermes_state_runtime.py")
    assert runtime_source, "hermes_state_runtime.py must exist in this checkout"
    runtime = _with_probe_extra(runtime_source, _PROBE_EXTRA)
    handler = next(node for node in ast.walk(ast.parse(runtime))
                   if isinstance(node, ast.FunctionDef) and node.name == "_worker_usage")
    main_expr = gate._main_allowed_expr(handler)
    assert main_expr is not None
    start = (main_expr.lineno, main_expr.col_offset)
    end = (main_expr.end_lineno, main_expr.end_col_offset)
    probe_nodes = [node for node in ast.walk(handler)
                   if isinstance(node, ast.Constant) and node.value == _PROBE_EXTRA]
    assert len(probe_nodes) == 1, "the probe must be injected exactly once"
    probe = probe_nodes[0]
    assert start <= (probe.lineno, probe.col_offset) < end, \
        "the probe must land inside the usage.main expression; elsewhere the gate never sees it"
    parsed = gate.contract_from_sources(
        lambda rel: runtime if rel == "hermes_state_runtime.py" else _read_repo(rel))
    assert parsed is not None and _PROBE_EXTRA in parsed.usage_main_fields


def test_check_code_skew_still_refuses_a_probe_field_the_gateway_lacks(monkeypatch):
    """Anti-weakening: a real extra on disk against a gateway without it is a REFUSAL, not a pass.

    Both sides come from this checkout's real modules; only the two readers are pinned. If the
    helper ever put the field where the gate does not look, this assertion would go green on a
    broken gate — which is exactly what the card requires to be impossible.
    """
    with_probe = _edition(_PROBE_EXTRA)
    without_probe = _edition()
    assert _PROBE_EXTRA not in without_probe.usage_main_fields
    monkeypatch.setattr(gate, "disk_contract", lambda *_a, **_kw: with_probe)
    monkeypatch.setattr(gate, "commit_contract", lambda *_a, **_kw: without_probe)
    skew = gate.check_code_skew(disk_sha="1" * 40, gateway_sha="2" * 40)
    assert skew is not None, "an extra field the gateway rejects must refuse the worker start"
    assert _PROBE_EXTRA in skew.problems[0] and "usage.main" in skew.problems[0]
    # and the safe direction (gateway ahead of disk) still runs
    monkeypatch.setattr(gate, "disk_contract", lambda *_a, **_kw: without_probe)
    monkeypatch.setattr(gate, "commit_contract", lambda *_a, **_kw: with_probe)
    assert gate.check_code_skew(disk_sha="1" * 40, gateway_sha="2" * 40) is None


def test_two_editions_differing_only_in_extras_are_not_mistaken_for_one_contract():
    """Same model fields, different extras: the field channel must see a difference, both ways.

    The unsafe direction (disk accepts an extra field, gateway does not) is a refusal that names
    the field. The safe direction (the gateway accepts a superset) is a forward commit, so it is
    NOT a refusal — but it is never reported as "identical contracts" either: the difference is
    visible in the parsed sets, which is what the blind revision lost.
    """
    without_probe = _edition()
    with_probe = _edition(_PROBE_EXTRA)
    assert with_probe.usage_main_fields - without_probe.usage_main_fields == {_PROBE_EXTRA}
    assert without_probe.usage_main_fields - with_probe.usage_main_fields == set()

    problems = gate.compare_contracts(with_probe, without_probe, frozenset())
    assert problems, "a field the gateway does not accept must be reported"
    assert _PROBE_EXTRA in problems[0] and "usage.main" in problems[0]
    # reverse order: parsed on both sides (so the difference is visible), yet allowed by the
    # subset rule — a gateway ahead of disk is an ordinary forward commit.
    assert gate.compare_contracts(without_probe, with_probe, frozenset()) == ()
    # ...but the difference is never invisible: the symmetric view names the field from BOTH sides,
    # which is precisely what the blind revision could not do.
    forward = gate.field_set_differences(without_probe, with_probe)
    assert forward and _PROBE_EXTRA in forward[0]
    backward = gate.field_set_differences(with_probe, without_probe)
    assert backward and _PROBE_EXTRA in backward[0]
    assert gate.field_set_differences(with_probe, with_probe) == ()


def test_the_real_incident_pair_is_refused_by_contract():
    """Disk ``34086e258353`` accepts ``prompt_cache_clock``; gateway ``4713ca0ab46`` does not.

    The exact pair the previous verdict walked through as PASSING. Both sides are parsed out of the
    object database, so this holds regardless of which checkout runs the test; skipped (never
    silently green) when the objects are absent.
    """
    repo = _repo_root()

    def present(sha: str) -> bool:
        return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                              capture_output=True, check=False).returncode == 0

    if not (present("34086e258353") and present("4713ca0ab46")):
        pytest.skip("the incident commits are not in this checkout's object database")
    disk = gate.commit_contract("34086e258353", repo)
    running = gate.commit_contract("4713ca0ab46", repo)
    assert disk is not None and running is not None
    assert "prompt_cache_clock" in disk.usage_main_fields
    assert "prompt_cache_clock" not in running.usage_main_fields
    problems = gate.compare_contracts(disk, running, frozenset())
    assert problems, "this is the incident: a real split must not compare as equal"
    assert "prompt_cache_clock" in problems[0]
    assert gate.compare_contracts(running, disk, frozenset()) == ()
    # the split is visible in both directions, not only as a refusal
    assert "prompt_cache_clock" in gate.field_set_differences(disk, running)[0]
    assert "prompt_cache_clock" in gate.field_set_differences(running, disk)[0]


def test_the_pre_split_store_layout_is_parsed_too():
    """A checkout that keeps the handler under ``persistence/sqlite/workers/`` must parse as well.

    Commit ``8fc03744473`` is the shape that caused the incident: the store modules live under
    ``persistence/sqlite/``, not at the repository root. A gate that only knows the root layout
    cannot read that side at all, and silently falls back to the history check.
    """
    repo = _repo_root()
    if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", "8fc03744473^{commit}"],
                      capture_output=True, check=False).returncode != 0:
        pytest.skip("the pre-split commit is not in this checkout's object database")
    contract = gate.commit_contract("8fc03744473", repo)
    assert contract is not None, "the pre-split store layout must parse into a contract"
    assert "prompt_cache_clock" in contract.usage_main_fields, \
        "that revision accepts prompt_cache_clock; the gate must see it"
    assert "usage.main" in contract.operations


def test_disk_contract_reads_the_extras_of_this_checkout():
    """``disk_contract()`` on the real checkout matches the handler's own accepted set (D4)."""
    disk = gate.disk_contract(repo=_repo_root())
    assert disk is not None
    expected = _edition()
    assert disk.usage_main_fields == expected.usage_main_fields, \
        "disk_contract must agree with the real handler's expression, not with a literal"
    assert {"pricing_version", "absolute", "task"} <= set(disk.usage_main_fields)


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


def test_the_field_channel_refuses_end_to_end(monkeypatch):
    """``check_code_skew`` — not just ``compare_contracts`` — refuses on the extras alone.

    Both sides are the REAL parsed contracts of the incident pair; only the two readers are pinned
    (a checkout on 34086e258353 talking to a gateway on 4713ca0ab46). Without this, a gate could
    parse the field correctly and still never reach the refusal, which is the other half of the
    defect: "sees the field" and "refuses because of it" are separate claims.
    """
    repo = _repo_root()
    if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", "34086e258353^{commit}"],
                      capture_output=True, check=False).returncode != 0:
        pytest.skip("the incident commits are not in this checkout's object database")
    disk = gate.commit_contract("34086e258353", repo)
    running = gate.commit_contract("4713ca0ab46", repo)
    assert disk is not None and running is not None
    monkeypatch.setattr(gate, "disk_contract", lambda *_a, **_kw: disk)
    monkeypatch.setattr(gate, "commit_contract", lambda *_a, **_kw: running)

    skew = gate.check_code_skew(disk_sha="34086e258353", gateway_sha="4713ca0ab46", repo=repo)
    assert skew is not None, "the field channel must produce a refusal, not only a comparison"
    assert "prompt_cache_clock" in skew.problems[0]
    assert "does not match the running gateway" in skew.message

    # and the safe direction stays allowed
    monkeypatch.setattr(gate, "disk_contract", lambda *_a, **_kw: running)
    monkeypatch.setattr(gate, "commit_contract", lambda *_a, **_kw: disk)
    assert gate.check_code_skew(disk_sha="4713ca0ab46", gateway_sha="34086e258353",
                                repo=repo) is None


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
                          capture_output=True, check=False).returncode == 1
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", gateway, worker],
                          capture_output=True, check=False).returncode == 1
    assert subprocess.run(["git", "-C", str(repo), "merge-base", worker, gateway],
                          capture_output=True, text=True, check=False).stdout.strip() == base
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
