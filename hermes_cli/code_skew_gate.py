"""Refuse to start a worker whose code cannot speak the running gateway's persistence RPC.

Why this exists
---------------
The gateway is a long-lived process: it imports its modules once at boot and keeps serving them.
A worker spawned after ``hermes update`` swapped the checkout starts as a fresh interpreter and
imports whatever is on disk now. If the update moved the tree onto another history, the two sides
stop agreeing on the ``worker.persist`` contract, and the failure is expensive and silent.

Incident 26.11.2026 03:05-03:20 MSK: the gateway kept serving ``da0f60614069`` while workers booted
off ``8fc03744473``. The new agent sent ``usage.main`` with ``prompt_cache_clock``; the old gateway's
``_worker_usage`` (``hermes_state_runtime.py:885``) did not allow that key and answered
``invalid_params``, every receipt of the queue rolled back, ``Session DB append_message failed:
invalid_params`` surfaced, and the worker died ~60 s into its turn. Five cards were blocked on the
retry limit for a host-side condition.

What is compared
----------------
Not a version string. Two RPC facts decide whether this checkout can talk to that gateway:

* the field set ``usage.main`` accepts (``_MODEL_USAGE_FIELDS`` in ``hermes_state_usage.py`` plus the
  keys its validator adds), and
* the operation set the gateway dispatches inside ``worker.persist`` (the handler dict in
  ``hermes_state_runtime.mutate_worker_execution`` plus the lifecycle/compression handler keys).

Both are read as *source*: locally from the files on disk, and for the gateway from the commit its
``identify`` advertises (``git show <sha>:<path>`` inside this repository). The requirement is a
subset test — every field this checkout's writer defines must be allowed by the gateway, and every
operation this checkout's store calls must have a handler there. A gateway that accepts a superset
(an ordinary forward commit awaiting its restart) passes, which is exactly right: it serves older
code that still understands us.

When neither side's contract can be parsed (a tree whose layout moved on), the gate falls back to
history: unrelated histories are the one shape proven to carry a different contract. Unknowable
answers — no git, an unreadable checkout, an owner too old to advertise a commit — always PASS. A
gate that can refuse a healthy host is worse than the failure it guards.

Nothing is started to answer: the gate only reads the advertised endpoint, the checkout and the
object database. It never ensures, starts or restarts a gateway.
"""

from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, FrozenSet, Iterable, Optional

# ``usage.main``'s accepted field set is NEVER restated here: it is read out of the code that
# enforces it — the ``allowed = ...`` expression of ``_worker_usage`` in the runtime, evaluated
# against the module constants that expression names. A literal copy of that set in this gate goes
# stale the moment the handler's contract moves (that is how ``prompt_cache_clock`` stayed
# invisible), so the only thing pinned here is WHICH files to look in: two repository layouts carry
# the same handler (a checkout before and after the store's module split), and the gate must parse
# both or it is blind to exactly the incident it exists for.
_FIELD_LAYOUTS = (
    ("hermes_state_usage.py", "hermes_state_runtime.py",
     "hermes_state_worker_lifecycle.py", "hermes_state_worker_compression.py"),
    ("persistence/sqlite/conversation/_store/usage.py", "persistence/sqlite/workers/_receipt.py",
     "persistence/sqlite/workers/_lifecycle.py", "persistence/sqlite/workers/_compression.py"),
)
_DISPATCH_LAYOUTS = ("hermes_state_runtime.py", "persistence/sqlite/workers/_receipt.py")
_LIFECYCLE_MODULES = tuple(layout[2] for layout in _FIELD_LAYOUTS)
_COMPRESSION_MODULES = tuple(layout[3] for layout in _FIELD_LAYOUTS)
_STORE_MODULES = ("agent/runtime_session_store.py",)

_USAGE_MODULE = "hermes_state_usage.py"
_RUNTIME_MODULE = "hermes_state_runtime.py"
_LIFECYCLE_MODULE = _LIFECYCLE_MODULES[0]
_COMPRESSION_MODULE = _COMPRESSION_MODULES[0]
_STORE_MODULE = _STORE_MODULES[0]

#: Every file the extraction may look at. Read once per side; a layout that does not exist in this
#: checkout simply reads as ``None``.
_CANDIDATE_PATHS = tuple(dict.fromkeys(
    [p for pair in _FIELD_LAYOUTS for p in pair]
    + list(_DISPATCH_LAYOUTS) + list(_LIFECYCLE_MODULES) + list(_COMPRESSION_MODULES)
    + list(_STORE_MODULES)))

_ANONYMOUS_BASE = "HEAD"  # a commit the gateway advertises but this repo cannot resolve


@dataclass(frozen=True)
class RpcContract:
    """What one side of the split speaks: the ``usage.main`` field set and the ``worker.persist`` map."""

    usage_main_fields: frozenset[str]
    operations: frozenset[str]


@dataclass(frozen=True)
class CodeSkew:
    """This checkout and the running gateway disagree; nothing may run."""

    disk_sha: Optional[str]
    gateway_sha: Optional[str]
    problems: tuple[str, ...]
    gateway_pid: Optional[int] = None

    @property
    def message(self) -> str:
        """One actionable line: what disagrees, both commits, and the two ways out."""
        disk = self.disk_sha[:10] if self.disk_sha else "unknown"
        gateway = self.gateway_sha[:10] if self.gateway_sha else "unknown"
        who = f" (gateway pid {self.gateway_pid})" if self.gateway_pid else ""
        detail = "; ".join(self.problems) if self.problems else "the two sides speak different code"
        return (
            "code on disk does not match the running gateway: "
            f"worker code {disk}, gateway{who} code {gateway} — {detail}. "
            "A worker must not run against a gateway that serves other code: its persistence RPCs "
            "are refused and the turn dies ~60 s in. Restart the gateway so it boots the checkout on "
            "disk (`hermes gateway restart`), or put the checkout back on the gateway's commit "
            "(`hermes update` will refuse the move), then re-run this worker."
        )


# --------------------------------------------------------------------------------------------------
# Contract extraction (pure: every reader is injected, so the same parser serves disk and history)
# --------------------------------------------------------------------------------------------------


def _const_strings(node: ast.AST) -> list[str]:
    """Every string literal inside *node* that looks like a dotted RPC operation name."""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _looks_like_op(n.value)]


def _looks_like_op(value: str) -> bool:
    """``turn.acquire``/``usage.main``: one dot, lowercase segments, no whitespace, short."""
    if not 3 <= len(value) <= 40 or " " in value or "\n" in value:
        return False
    head, _, tail = value.partition(".")
    return bool(head) and bool(tail) and "." not in tail and value.islower()


def _set_literal(node: ast.AST) -> Optional[set[str]]:
    """The string members of a set/list/tuple literal (``frozenset({...})``'s argument too)."""
    if isinstance(node, ast.Call) and node.args:
        node = node.args[0]
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return None
    return {e.value for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)}


def _module_constants(source: str) -> dict[str, set[str]]:
    """Every module-level ``NAME = frozenset((...))`` (any literal container) in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        members = _set_literal(node.value) if node.value is not None else None
        if members is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = members
    return out


def _eval_field_set(node: ast.AST, consts: dict[str, set[str]],
                    depth: int = 0) -> Optional[set[str]]:
    """Evaluate a set expression over literals and module constants: ``A | B``, ``A - B``, ``A & B``.

    ``None`` when the expression leans on anything else (a call, an attribute, an unknown name):
    a contract half-understood is worse than no contract, because the gate would then compare a
    smaller set than the handler enforces and pass a real skew.
    """
    if depth > 8:
        return None
    literal = _set_literal(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        found = consts.get(node.id)
        return set(found) if found is not None else None
    if isinstance(node, ast.BinOp):
        left = _eval_field_set(node.left, consts, depth + 1)
        right = _eval_field_set(node.right, consts, depth + 1)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.BitOr):
            return left | right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.BitAnd):
            return left & right
    return None


def _main_allowed_expr(func: ast.FunctionDef) -> Optional[ast.AST]:
    """The ``allowed = ...`` expression of *func*'s NON-auxiliary branch (an ``IfExp`` aware walk).

    The handler computes two sets: the ``usage.auxiliary`` subset and the ``usage.main`` set this
    gate needs. Only the main one is the contract of a normal worker receipt, so both spellings are
    handled — ``allowed = X if auxiliary else Y`` and ``if auxiliary: allowed = X`` / ``else: Y``.
    """
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.IfExp) \
                and any(isinstance(t, ast.Name) and t.id == "allowed" for t in node.targets):
            return node.value.orelse
        if not isinstance(node, ast.If):
            continue
        test_names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "auxiliary" not in test_names:
            continue
        # ``if auxiliary: ... else: <main>`` — the main branch is the else body (or the
        # ``if not auxiliary:`` spelling, where it is the body).
        main_body = node.orelse if not _negated_auxiliary(node.test) else node.body
        for inner in main_body:
            if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.IfExp) \
                    and any(isinstance(t, ast.Name) and t.id == "allowed" for t in inner.targets):
                return inner.value.orelse
            if isinstance(inner, ast.Assign) \
                    and any(isinstance(t, ast.Name) and t.id == "allowed" for t in inner.targets):
                return inner.value
    return None


def _negated_auxiliary(test: ast.AST) -> bool:
    return isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)


def _usage_fields_from(source: str, runtime_source: Optional[str] = None) -> Optional[frozenset[str]]:
    """The field set ``usage.main`` accepts, read out of the code that enforces it.

    *source* is the module declaring the constants (``_MODEL_USAGE_FIELDS`` and the named extras);
    *runtime_source* is the module whose ``_worker_usage`` builds the accepted set. With both, the
    answer is that handler's own expression evaluated over those constants — so a field added to
    either constant lands here without touching this gate. Without a runtime source the model-field
    constant plus a named ``USAGE_MAIN_EXTRA_FIELDS``/``USAGE_MAIN_FIELDS`` constant is used, and
    only if neither exists is the bare model set returned.
    """
    consts = _module_constants(source)
    model = consts.get("_MODEL_USAGE_FIELDS")
    named_extras = consts.get("USAGE_MAIN_EXTRA_FIELDS") or consts.get("USAGE_MAIN_FIELDS")
    if runtime_source:
        try:
            tree = ast.parse(runtime_source)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not (isinstance(node, ast.FunctionDef) and node.name == "_worker_usage"):
                    continue
                expr = _main_allowed_expr(node)
                fields = _eval_field_set(expr, consts) if expr is not None else None
                if fields:
                    return frozenset(fields)
                break
    if model is not None:
        return frozenset(model | (named_extras or set()))
    if named_extras:
        return frozenset(named_extras)
    return None


def _handler_keys_from(source: str, dict_name: str) -> list[str]:
    """String keys of ``<dict_name> = {...}`` in *source* (a handler map), else ``[]``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == dict_name for t in node.targets):
            continue
        return [k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return []


def _dispatch_ops_from(source: str) -> list[str]:
    """Operation names the ``worker.persist`` dispatcher in ``hermes_state_runtime.py`` handles.

    The dispatcher is one dict literal (+ a comprehension over the turn operations + the two
    handler maps it spreads); every dotted literal inside ``mutate_worker_execution`` is one of
    its operation names, so walking that one function is enough and stays stable across refactors
    of the surrounding module.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "mutate_worker_execution":
            return _const_strings(node)
    return []


def contract_from_sources(read: Callable[[str], Optional[str]]) -> Optional[RpcContract]:
    """The RPC contract *read* describes, or ``None`` when the layout is not one of the known ones.

    Each entry of ``_FIELD_LAYOUTS`` is one repository layout: the module with the named field
    constants and the module whose ``_worker_usage`` builds the accepted set. The gate takes the
    first layout whose field set parses, and pairs it with that layout's dispatch / lifecycle /
    compression handler modules, so a checkout on either side of the store's module split yields a
    complete contract instead of a blind one.
    """
    for usage_module, runtime_module, lifecycle_module, compression_module in _FIELD_LAYOUTS:
        usage_source = read(usage_module)
        if not usage_source:
            continue
        runtime_source = read(runtime_module)
        fields = _usage_fields_from(usage_source, runtime_source)
        if fields is None:
            continue
        ops: set[str] = set()
        if runtime_source:
            ops.update(_dispatch_ops_from(runtime_source))
        for handler_module, handler_map in ((lifecycle_module, "WORKER_LIFECYCLE_HANDLERS"),
                                            (compression_module, "WORKER_COMPRESSION_HANDLERS")):
            source = read(handler_module)
            if source:
                ops.update(_handler_keys_from(source, handler_map))
        if not ops:
            continue
        return RpcContract(usage_main_fields=fields, operations=frozenset(ops))
    return None


def required_operations_from_sources(read: Callable[[str], Optional[str]]) -> Optional[frozenset[str]]:
    """Operations this checkout's worker store actually calls (``_apply('<op>')`` constants)."""
    source = read(_STORE_MODULE)
    if not source:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    ops = {n.args[0].value for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "_apply" and n.args
           and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}
    return frozenset(ops) if ops else None


def field_set_differences(left: RpcContract, right: RpcContract) -> tuple[str, ...]:
    """Every field by which the two ``usage.main`` sets differ, in WHICHEVER direction.

    Symmetric on purpose. ``compare_contracts`` answers "may this worker run?" (a subset test, so a
    gateway ahead of disk passes). This answers "do the two sides even describe the same set?" — the
    question the earlier revision could not answer at all, because it parsed only the model fields
    and never the extras. A difference the gate cannot see is a difference it cannot report.
    """
    only_left = sorted(set(left.usage_main_fields) - set(right.usage_main_fields))
    only_right = sorted(set(right.usage_main_fields) - set(left.usage_main_fields))
    differences: list[str] = []
    if only_left:
        differences.append("only the first side accepts " + ", ".join(only_left))
    if only_right:
        differences.append("only the second side accepts " + ", ".join(only_right))
    return tuple(differences)


def compare_contracts(disk: RpcContract, gateway: RpcContract,
                      required_ops: Optional[Iterable[str]] = None) -> tuple[str, ...]:
    """Human-readable incompatibilities: the disk side must be a SUBSET of what the gateway serves."""
    problems: list[str] = []
    unknown_fields = sorted(set(disk.usage_main_fields) - set(gateway.usage_main_fields))
    if unknown_fields:
        problems.append("usage.main on disk sends "
                        + ", ".join(unknown_fields) + " which the gateway does not accept")
    required = set(disk.operations if required_ops is None else required_ops)
    missing_ops = sorted(required - set(gateway.operations))
    if missing_ops:
        problems.append("worker.persist calls " + ", ".join(missing_ops)
                        + " which the gateway has no handler for")
    return tuple(problems)


# --------------------------------------------------------------------------------------------------
# Reading the two sides
# --------------------------------------------------------------------------------------------------


def disk_code_sha() -> Optional[str]:
    """The commit the checkout on disk has *now* (what this process would import)."""
    try:
        from hermes_cli.version_info import installed_commit_sha

        return installed_commit_sha()
    except Exception:
        return None


def _repo_dir() -> Optional[Path]:
    try:
        from hermes_cli.version_info import _resolve_repo_dir

        return _resolve_repo_dir()
    except Exception:
        return None


def disk_contract(repo: Optional[Path] = None) -> Optional[RpcContract]:
    """This checkout's own contract, read from the files it would import."""
    root = repo or _repo_dir()
    if root is None:
        return None

    def read(rel: str) -> Optional[str]:
        try:
            return (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    return contract_from_sources(read)


def commit_contract(sha: str, repo: Optional[Path] = None) -> Optional[RpcContract]:
    """The contract of the commit *sha* advertises, read out of this repository's object database."""
    root = repo or _repo_dir()
    if root is None or not sha:
        return None

    def read(rel: str) -> Optional[str]:
        try:
            res = subprocess.run(["git", "-C", str(root), "show", f"{sha}:{rel}"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return res.stdout if res.returncode == 0 and res.stdout else None

    return contract_from_sources(read)


def _required_ops(repo: Optional[Path] = None) -> Optional[frozenset[str]]:
    root = repo or _repo_dir()
    if root is None:
        return None
    try:
        source = (Path(root) / _STORE_MODULE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    def read(rel: str) -> Optional[str]:
        return source if rel == _STORE_MODULE else None

    return required_operations_from_sources(read)


def gateway_identity(home: Any = None) -> tuple[Optional[str], Optional[int]]:
    """``(code_sha, pid)`` the running owner boots from; ``(None, None)`` when unknown.

    Reads the advertised endpoint only: this runs on every worker start, so it must never start,
    ensure or restore a gateway as a side effect of asking.
    """
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.gateway_runtime import _owner_pid, discover_gateway_endpoint

        owner_home = home or get_hermes_home()
        found = discover_gateway_endpoint(owner_home, timeout=5)
        endpoint = found.endpoint
        sha = getattr(endpoint, "code_sha", None) if endpoint is not None else None
        pid: Optional[int] = None
        try:
            pid = _owner_pid(Path(endpoint.control_home)) if endpoint and endpoint.control_home \
                else _owner_pid(Path(owner_home))
        except Exception:
            pid = None
        return (sha if isinstance(sha, str) and sha else None,
                pid if isinstance(pid, int) and pid > 0 else None)
    except Exception:
        return None, None


def _same_commit(left: str, right: str) -> bool:
    """Compare commits by the shorter of their two spellings (a SHA may be abbreviated)."""
    a, b = left.strip().lower(), right.strip().lower()
    if not a or not b:
        return True
    width = min(len(a), len(b))
    return a[:width] == b[:width]


def _diverged(left: str, right: str, repo: Optional[Path] = None) -> Optional[bool]:
    """``True`` when the two commits sit on unrelated histories, ``None`` when unknowable.

    The question is literally "is there a merge base": ``git merge-base`` answers it in one call and
    a non-empty answer means the two commits share a history — including two SIBLING branches, whose
    merge base is their fork point. Deciding this by two ``merge-base --is-ancestor`` calls, as an
    earlier revision did, reports every non-ancestor pair as "no shared history", which refuses a
    healthy host whose worker and gateway merely sit on different branches of one repository.
    """
    root = repo or _repo_dir()
    if root is None:
        return None
    try:
        res = subprocess.run(["git", "-C", str(root), "merge-base", left, right],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode == 0:
        # exit 0 carries the merge base: shared history, however far apart the two tips are.
        return not (res.stdout or "").strip()
    if res.returncode == 1:
        # exit 1 is git's "no merge base": the only proven-divergence answer.
        return True
    return None  # unknown revision / broken repo / ambiguous arguments


_UNSET: Any = object()


def check_code_skew(home: Any = None, *, disk_sha: Any = _UNSET, gateway_sha: Any = _UNSET,
                    repo: Optional[Path] = None) -> Optional[CodeSkew]:
    """``CodeSkew`` when this worker cannot speak the owner's RPC, else ``None``.

    Order of evidence: a PROVEN contract incompatibility (parsed on both sides) refuses first;
    otherwise PROVEN history divergence (no shared ancestor) refuses; anything unknowable passes.
    ``disk_sha``/``gateway_sha`` let a probe or a test supply both sides; pass ``None`` for either
    to state "unknown".
    """
    if disk_sha is _UNSET:
        if os.environ.get("HERMES_SKIP_CODE_SKEW_GATE"):
            # An explicit, documented escape for a host that knows it is mid-update — an env
            # opt-out, never a silent fallback: the refusal is what a healthy host sees.
            return None
        disk_sha = disk_code_sha()
    if gateway_sha is _UNSET:
        gateway_sha, pid = gateway_identity(home)
    else:
        pid = None
    if not disk_sha or not gateway_sha:
        return None  # an unknown side is never skew
    if _same_commit(disk_sha, gateway_sha):
        return None

    gateway_contract = commit_contract(gateway_sha, repo)
    disk = disk_contract(repo)
    if gateway_contract is not None and disk is not None:
        problems = compare_contracts(disk, gateway_contract, _required_ops(repo))
        if problems:
            return CodeSkew(disk_sha=disk_sha, gateway_sha=gateway_sha,
                            problems=problems, gateway_pid=pid)
        return None  # the gateway accepts everything this checkout sends: an ordinary forward commit

    # The layouts differ (or one side is unreadable): only proven divergence refuses.
    if _diverged(disk_sha, gateway_sha, repo) is not True:
        return None
    return CodeSkew(disk_sha=disk_sha, gateway_sha=gateway_sha, gateway_pid=pid,
                    problems=("the two commits share no history, so their RPC contracts differ",))
