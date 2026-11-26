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

# The keys ``usage.main`` accepts on top of ``_MODEL_USAGE_FIELDS`` — ``hermes_state_runtime.
# _worker_usage``'s non-auxiliary branch: ``_MODEL_USAGE_FIELDS | {'pricing_version', 'absolute',
# 'task'}``. Kept here as one literal so both sides of the comparison are computed the same way.
_USAGE_MAIN_EXTRA_FIELDS = frozenset({"pricing_version", "absolute", "task"})

_USAGE_MODULE = "hermes_state_usage.py"
_RUNTIME_MODULE = "hermes_state_runtime.py"
_LIFECYCLE_MODULE = "hermes_state_worker_lifecycle.py"
_COMPRESSION_MODULE = "hermes_state_worker_compression.py"
_STORE_MODULE = "agent/runtime_session_store.py"

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


def _usage_fields_from(source: str) -> Optional[frozenset[str]]:
    """``_MODEL_USAGE_FIELDS`` from a ``hermes_state_usage.py`` source, else ``None``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "_MODEL_USAGE_FIELDS" for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.args:
            value = value.args[0]
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return None
        fields = [e.value for e in value.elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return frozenset(fields) if fields else None
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
    """The RPC contract *read* describes, or ``None`` when the layout is not this one."""
    usage_source = read(_USAGE_MODULE)
    fields = _usage_fields_from(usage_source) if usage_source else None
    ops: set[str] = set()
    runtime_source = read(_RUNTIME_MODULE)
    if runtime_source:
        ops.update(_dispatch_ops_from(runtime_source))
    for module, name in ((_LIFECYCLE_MODULE, "WORKER_LIFECYCLE_HANDLERS"),
                         (_COMPRESSION_MODULE, "WORKER_COMPRESSION_HANDLERS")):
        source = read(module)
        if source:
            ops.update(_handler_keys_from(source, name))
    if fields is None or not ops:
        return None
    return RpcContract(usage_main_fields=fields, operations=frozenset(ops))


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
    """``True`` when the two commits sit on unrelated histories, ``None`` when unknowable."""
    root = repo or _repo_dir()
    if root is None:
        return None

    def _is_ancestor(a: str, b: str) -> Optional[bool]:
        try:
            res = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", a, b],
                                 capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode == 0:
            return True
        if res.returncode == 1:
            return False
        return None  # unknown revision / broken repo

    try:
        one_way = _is_ancestor(left, right)
        if one_way is None:
            return None
        if one_way:
            return False
        other_way = _is_ancestor(right, left)
        if other_way is None:
            return None
        return not other_way
    except Exception:
        return None


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
