"""Backstop: every place a PROCESS can be born must be accounted for, or CI fails.

The archive's ``tool_firewall`` is a second line of defence for a DENYLIST config (tools can be
switched off, so a regression turns one back on). Our config is different: tools are never
switched off, they are limited by permission, so the failure mode is different too — a new spawn
site appears (an upstream update adds a tool, a refactor adds a helper) and no boundary is
applied to it. Nothing in the code fails; the child simply runs unconfined.

So the second line is this: an explicit inventory of every spawn site in production trees, each
classified as one of

* ``confined`` — the argv goes through ``hermes_acl.policy.wrap_argv`` (directly or via a helper
  that does), so the kernel confines it when ``acl.enabled=true``;
* ``test`` — test-only/kernel-level fixture, not a user-facing spawn;
* ``exempt`` — deliberately outside the boundary, WITH a reason (a platform that has no
  Landlock: desktop sidecar, systemd-run wrapper around an already-confined payload, the
  confinement launcher itself).

A spawn site that is in none of these categories fails the test. The inventory is a FILE so a
reviewer sees the classification, and the test checks the file against the tree in BOTH
directions: an unclassified site fails, and a stale entry (no longer in the tree) fails too, so
the list cannot rot into a comfortable lie.

Deliberately NOT a runtime hook: importing a guard into every spawn path costs the hot path and
still misses a ``subprocess.Popen`` made through an alias. Static inventory + review is the check
that actually catches "someone added a spawn" — which is the regression this exists for.
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Trees whose code runs in production. ``tests/``, ``evals/``, ``scripts/`` are excluded: their
#: spawns are fixtures, not surfaces a user reaches.
PRODUCTION_TREES = ("tools", "agent", "gateway", "hermes_cli", "cron", "acp_adapter", "plugins",
                    "hermes", "tui_gateway", "mcp")

#: Shells that run a command string when given ``-c`` (and the login/interactive variants).
SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "busybox"})

#: Interpreters whose ``-c`` payload is a SCRIPT WE WROTE, not a command from the turn.
#: ``python -c "$(user input)"`` does not exist in this codebase: a user-supplied Python snippet
#: arrives through the terminal tool (covered by ``_run_bash``), so treating every
#: ``sys.executable -c <literal>`` as a boundary site would only bury the real ones.
_SCRIPT_INTERPRETERS = frozenset({"python", "python3", "py"})

#: Flags that make a shell run the string in the NEXT argument as a command.
_COMMAND_FLAGS = frozenset({"-c", "-ic", "-lic", "-lc", "-cl", "-ci", "+m"})


@dataclass(frozen=True)
class CommandSite:
    """A spawn site that runs an ARBITRARY command, not a fixed program.

    This — not the 400+ spawns of fixed binaries (``git``, ``rg``, ``uname``) — is the set that
    needs the boundary: a fixed argv was decided by us, a command string was decided by whoever
    wrote the turn. Confining ``git status`` adds nothing; confining ``bash -c <anything>`` is
    the whole point.
    """

    path: str
    line: int
    call: str
    reason: str  # "shell-command" | "shell-true" | "exec-argv"

#: Calls that can produce a child process. Kept as attribute chains so ``ast`` matching is exact.
_SPAWN_CALLS = frozenset({
    ("subprocess", "Popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("os", "system"),
    ("os", "popen"),
    ("os", "posix_spawn"),
    ("os", "posix_spawnp"),
    ("os", "spawnl"),
    ("os", "spawnle"),
    ("os", "spawnlp"),
    ("os", "spawnlpe"),
    ("os", "spawnv"),
    ("os", "spawnve"),
    ("os", "spawnvp"),
    ("os", "spawnvpe"),
    ("os", "execv"),
    ("os", "execve"),
    ("os", "execvp"),
    ("os", "execvpe"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
    ("pty", "fork"),
})

#: Bare names that spawn when imported directly (``from hermes_cli.process_identity import
#: spawn_owned``) — the call is then ``spawn_owned(...)`` with no attribute chain.
_SPAWN_BARE = frozenset({"spawn_owned"})


def _dotted(node: ast.AST) -> Optional[str]:
    """``subprocess.Popen`` for an attribute chain; ``None`` for anything else."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _is_spawn_call(node: ast.Call, aliases: dict[str, str]) -> Optional[str]:
    """The canonical ``module.func`` name for a spawn call, honouring import aliases."""
    func = node.func
    if isinstance(func, ast.Name):
        local = func.id
        if local in _SPAWN_BARE:
            return local
        target = aliases.get(local)
        if not target:
            return None
        # An alias is only a spawn when what it points AT is a spawn: ``Path`` -> ``pathlib.Path``
        # is an alias too, and treating every alias as a spawn is how an inventory becomes noise.
        module, _, tail = target.rpartition(".")
        if (module, tail) in _SPAWN_CALLS:
            return target
        return "spawn_owned" if tail in _SPAWN_BARE else None
    dotted = _dotted(func)
    if not dotted:
        return None
    head, _, tail = dotted.rpartition(".")
    module = aliases.get(head, head)
    canon = f"{module}.{tail}"
    if (module, tail) in _SPAWN_CALLS:
        return canon
    if tail in _SPAWN_BARE:
        return "spawn_owned"
    return None


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """``import subprocess as sp`` -> ``{"sp": "subprocess"}``; ``from x import y`` -> ``{"y": "x.y"}``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    aliases.setdefault(head, head)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


@dataclass(frozen=True)
class Site:
    path: str      # relative to REPO_ROOT
    line: int
    call: str      # canonical call name

    @property
    def key(self) -> str:
        return f"{self.path}:{self.line}"

    def to_json(self) -> dict:
        return {"path": self.path, "line": self.line, "call": self.call}


def scan(root: Path = REPO_ROOT) -> list[Site]:
    """Every spawn call in the production trees, sorted for stable diffs."""
    sites: list[Site] = []
    for tree in PRODUCTION_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree_ast = ast.parse(source)
            except (OSError, SyntaxError):
                continue
            aliases = _module_aliases(tree_ast)
            for node in ast.walk(tree_ast):
                if isinstance(node, ast.Call):
                    canon = _is_spawn_call(node, aliases)
                    if canon:
                        sites.append(Site(str(path.relative_to(root)), node.lineno, canon))
    return sorted(sites, key=lambda s: (s.path, s.line))


def _literal_str(node: ast.AST | None) -> Optional[str]:
    """The string value of a literal (or f-string prefix), else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _python_like(node: ast.AST | None) -> bool:
    """``sys.executable`` / ``python_path`` / ``str(python)`` — an interpreter running OUR script."""
    if node is None:
        return False
    if isinstance(node, ast.Attribute):
        return _dotted(node) in {"sys.executable", "sys.base_prefix"}
    if isinstance(node, ast.Name):
        return "python" in node.id.lower() or node.id in {"sys_executable", "interpreter", "exe"}
    if isinstance(node, ast.Subscript):
        return _python_like(node.value)
    if isinstance(node, ast.Call):
        # ``str(python)`` / ``_python_exe()``: judge by the call's own name and its argument.
        name = (_dotted(node.func) or "").rsplit(".", 1)[-1].lower()
        if "python" in name:
            return True
        return bool(node.args) and _python_like(node.args[0])
    if isinstance(node, ast.IfExp):
        return _python_like(node.body) or _python_like(node.orelse)
    return False


def _shell_like(node: ast.AST | None) -> bool:
    """Whether position 0 names a SHELL (literal, variable or call) — used for a bare ``-c``.

    ``-c`` alone is not evidence of a shell: ffmpeg uses ``-c`` for a codec, and a bare ``-c``
    followed by a non-literal would then make every media command look like a command site. The
    multi-letter shell flags (``-lic``, ``-ic``) are safe on their own — no other tool takes them.
    """
    if node is None:
        return False
    text = _literal_str(node)
    if text is not None:
        words = text.split()
        return bool(words) and os.path.basename(words[0]) in SHELL_NAMES
    name = ""
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Call):
        name = (_dotted(node.func) or "").rsplit(".", 1)[-1]
    low = name.lower()
    return any(token in low for token in ("shell", "bash", "zsh")) or low in {"sh", "dash", "ksh"}


def _shell_flags(argv: ast.AST | None) -> Optional[str]:
    """``"command"`` when this argv runs a shell command string, else ``None``.

    The question is "was this argv decided by US or by the turn?" — three shapes mean the latter:

    * ``[<shell>, "-c"/"-lic"/..., <str>]`` — a command string;
    * ``[<shell>]`` with the string appended later via ``*`` / ``+=`` — treated as a command,
      because a shell with no ``-c`` still reads stdin;
    * ``["... -c ..."]`` built as one f-string, where the shell name is inside the literal.

    A fixed argv such as ``["git", "status"]`` is NOT a command site: confining it adds nothing
    the caller does not already control. Nor is ``[sys.executable, "-c", <our script>]`` — that
    payload is code from THIS repository, and calling it a user command would bury the real sites
    under a hundred false positives.
    """
    if argv is None:
        return None
    if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
        first = argv.elts[0]
        if _python_like(first):
            return None
        first_text = _literal_str(first)
        if first_text is not None and first_text.strip().startswith("-"):
            # A bare flag list (``["-c", str(window)]`` for llama-server), not an argv: there is
            # no program in position 0 at all.
            return None
    items = argv.elts if isinstance(argv, (ast.List, ast.Tuple)) else [argv]
    texts = [t for t in (_literal_str(i) for i in items) if t is not None]
    if not texts:
        return None
    # A shell NAME in a tuple of names (``_SHELL_NAMES = ("bash", "sh", ...)``) is a constant
    # list, not an argv: every element is a bare word, there is no flag, and no element is a
    # program that would run anything. ``["bash", "-c"]`` is NOT this shape — the flag is what
    # makes it an argv.
    if (len(texts) > 1
            and all(len(t.split()) == 1 for t in texts)
            and not any(t.strip().startswith("-") for t in texts)):
        return None
    head = os.path.basename(texts[0].split()[0]) if texts[0].strip() else ""
    if head in SHELL_NAMES:
        return "shell-command"
    if head in _SCRIPT_INTERPRETERS:
        return None
    # The shell is often a VARIABLE (``bash``, ``_find_shell()``), so the name is not a literal.
    # The flag is what identifies the shape: a ``-c``-family flag followed by a NON-literal is a
    # command string by construction. ``git -c key=value`` has a literal after it and is not this
    # shape; the login/interactive variants have no other user in the ecosystem at all.
    for idx, item in enumerate(items):
        text = _literal_str(item)
        if text is None or text.strip() not in _COMMAND_FLAGS:
            continue
        if text.strip() in {"-ic", "-lic", "-lc", "-ci", "-cl"}:
            return "shell-command"
        if text.strip() != "-c":
            continue
        # A bare ``-c`` needs the shell to be identifiable in position 0, or it is some other
        # tool's flag (ffmpeg ``-c aac``, git ``-c key=value``) and not a command string.
        if idx > 0 and not _shell_like(items[0]):
            continue
        following = items[idx + 1] if idx + 1 < len(items) else None
        if following is not None and _literal_str(following) is None:
            return "shell-command"
    for text in texts:
        words = text.split()
        if not words:
            continue
        base = os.path.basename(words[0])
        if base in SHELL_NAMES and any(w in _COMMAND_FLAGS for w in words[1:]):
            return "shell-command"
    return None


def _self_referential(value: ast.AST, name: str) -> bool:
    """``args = _wrap(args, ...)`` — an assignment that only re-points the same variable."""
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(value))


def _resolve_argv(argv: ast.AST | None, scope: ast.AST | None,
                  call_line: int | None = None) -> ast.AST | None:
    """Follow ONE level of ``argv = [...]`` inside the enclosing function.

    The two most important terminal paths assign their argv into a local before spawning
    (``args = [bash, "-c", cmd_string]`` ... ``subprocess.Popen(args, ...)``). Without this the
    scanner would miss exactly the sites the boundary depends on — a backstop with a blind spot
    at the hot path is worse than none, because it looks green.

    The assignment chosen is the NEAREST ONE ABOVE the call: a function that builds argv in two
    branches (pipe vs pty) ends with the last-written assignment, which may belong to the other
    branch and describe a different shape.
    """
    if not isinstance(argv, ast.Name) or scope is None:
        return argv
    # Two hops, because a boundary wrapper is itself an assignment above the spawn:
    # ``args = [bash, ...]`` -> ``args = _acl_wrap_argv(args, ...)`` -> ``Popen(args)``.
    # Without the second hop the scanner would report the wrapped call as "not a command site"
    # and the very sites this task confines would drop out of the inventory.
    current: ast.AST | None = argv
    for _hop in range(3):
        if not isinstance(current, ast.Name):
            # A call whose first argument is the same name: unwrap it once.
            if (isinstance(current, ast.Call) and current.args
                    and isinstance(current.args[0], ast.Name)
                    and current.args[0].id == argv.id):
                current = current.args[0]
                continue
            break
        best: ast.AST | None = None
        best_line = -1
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Name) and target.id == current.id
                            and node.lineno > best_line
                            and (call_line is None or node.lineno < call_line)
                            and not _self_referential(node.value, current.id)):
                        best, best_line = node.value, node.lineno
            elif isinstance(node, ast.AnnAssign) and node.value:
                if (isinstance(node.target, ast.Name) and node.target.id == current.id
                        and node.lineno > best_line
                        and (call_line is None or node.lineno < call_line)
                        and not _self_referential(node.value, current.id)):
                    best, best_line = node.value, node.lineno
        if best is None:
            break
        current = best
        if isinstance(current, (ast.List, ast.Tuple)):
            break
    return current if current is not None else argv


def _is_command_site(node: ast.Call, canon: str, scope: ast.AST | None = None) -> Optional[str]:
    """Classify one spawn call: ``"shell-command"`` / ``"shell-true"`` / ``"exec-argv"``."""
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return "shell-true"
    if canon in {"os.system", "os.popen", "asyncio.create_subprocess_shell"}:
        return "shell-true"
    if canon in {"os.execv", "os.execve", "os.execvp", "os.execvpe"}:
        # Replaces the process image with an arbitrary argv — no boundary can be applied after.
        return "exec-argv"
    argv = node.args[0] if node.args else None
    if argv is None:
        for kw in node.keywords:
            if kw.arg in ("args", "argv", "cmd", "command"):
                argv = kw.value
                break
    return _shell_flags(_resolve_argv(argv, scope, node.lineno))


def scan_commands(root: Path = REPO_ROOT) -> list[CommandSite]:
    """Spawn sites that run an ARBITRARY command — the set that needs the boundary."""
    out: list[CommandSite] = []
    for tree in PRODUCTION_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree_ast = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            aliases = _module_aliases(tree_ast)
            for scope in ast.walk(tree_ast):
                if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    continue
                for node in ast.walk(scope):
                    if not isinstance(node, ast.Call):
                        continue
                    canon = _is_spawn_call(node, aliases)
                    if not canon:
                        continue
                    reason = _is_command_site(node, canon, scope)
                    if reason:
                        site = CommandSite(str(path.relative_to(root)), node.lineno, canon, reason)
                        out.append(site)
    uniq = {(s.path, s.line, s.call): s for s in out}
    return sorted(uniq.values(), key=lambda s: (s.path, s.line))


def scan_builders(root: Path = REPO_ROOT) -> list[CommandSite]:
    """Functions that BUILD a shell-command argv (``argv = [shell, "-lic", cmd]``).

    These are where the boundary MUST be applied even though no ``Popen`` appears in them: the
    terminal paths hand the argv to a helper (``_spawn_job``, ``PtyProcessCls.spawn``), so a
    scanner keyed only on spawn calls cannot see the payload — its argv arrives as a parameter.
    Missing them would leave the hottest path unclassified while the check reported success.
    """
    out: list[CommandSite] = []
    for tree in PRODUCTION_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree_ast = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree_ast):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not isinstance(value, (ast.List, ast.Tuple)):
                    continue
                if _shell_flags(value) == "shell-command":
                    out.append(CommandSite(str(path.relative_to(root)), node.lineno,
                                           "argv-builder", "shell-command-builder"))
    uniq = {(s.path, s.line, s.reason): s for s in out}
    return sorted(uniq.values(), key=lambda s: (s.path, s.line))


def scan_all(root: Path = REPO_ROOT) -> list[CommandSite]:
    """Spawn sites AND argv builders — the full set that needs the boundary."""
    merged = {(s.path, s.line, s.reason): s for s in [*scan_commands(root), *scan_builders(root)]}
    return sorted(merged.values(), key=lambda s: (s.path, s.line, s.reason))


def load_inventory(path: Path) -> dict[str, dict]:
    """``{relpath: entry}`` from the JSON inventory; a missing file is an empty inventory."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    files = raw.get("files", raw)
    return {str(e["path"]): e for e in files}


def audit(inventory: dict[str, dict], sites: list[Site]) -> tuple[list[str], list[str], list[str]]:
    """``(uncovered, stale, bad)`` — the three ways the inventory can lie.

    Keyed by FILE with an expected COUNT, not by line number. A line entry goes stale on every
    edit above it, and a check that fails for unrelated reasons gets deleted — which is how a
    backstop silently stops backstopping. A count is stable under line drift and changes exactly
    when somebody adds or removes a spawn: the event this test exists for.
    """
    by_file: dict[str, int] = {}
    for site in sites:
        by_file[site.path] = by_file.get(site.path, 0) + 1

    uncovered = sorted(p for p in by_file if p not in inventory)
    stale = sorted(p for p in inventory if p not in by_file)
    bad: list[str] = []
    valid = {"confined", "out-of-scope"}
    for path, entry in inventory.items():
        status = entry.get("status")
        if status not in valid:
            bad.append(f"{path}: status {status!r} not one of {sorted(valid)}")
        elif not str(entry.get("reason") or "").strip():
            bad.append(f"{path}: needs a reason (what confines it, or why it is out of scope)")
        expected = entry.get("spawn_sites")
        if not isinstance(expected, int):
            bad.append(f"{path}: spawn_sites must be an integer")
        elif path in by_file and expected != by_file[path]:
            verb = "new" if by_file[path] > expected else "fewer"
            bad.append(
                f"{path}: inventory says {expected} spawn site(s), the tree has {by_file[path]} "
                f"({verb} spawn{'s' if by_file[path] != 1 else ''}). Classify the change: is the "
                f"new child confined, and how? Then update spawn_sites deliberately.")
    return uncovered, stale, bad
