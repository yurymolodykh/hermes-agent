"""Which RESULTS a signed-in person may touch: files, skills, memory, card attachments (МПД Т6).

Steps 3–5 gave every person their own identity, their own sessions and their own orchestrator.
The remaining surface answers a different question — "show me this path / this skill / this
memory / this attachment" — and it answered it for the whole host: ``/api/files*`` and ``/api/fs/*``
resolved any path readable by the process, ``/api/skills*`` and ``/api/memory*`` fell back to the
LAUNCH profile's home when no ``?profile=`` was named, and a card attachment was fetched by its
attachment id alone. One login could therefore read another person's artefacts.

This module is the ONE policy those surfaces ask, in the same spirit as ``hermes_acl_scope``
(sessions) and ``hermes_acl_profiles`` (profiles):

* **the subject's file root** — :func:`file_roots`: the person's own orchestrator home(s) from
  the registry, plus the directories of the TEAM's material — every card folder
  (``kanban/workspaces/<id>``, ``kanban/attachments/<id>``) except a PERSONAL-contour card's, and
  the working directories of the SHARED profiles (:func:`_shared_work_dirs`), where a producer's
  scripts actually land. Everything else on the host is outside the root and refused by
  :func:`require_path`, after the path has been fully resolved by the caller (``..``, symlinks and
  ``/proc/self/root`` collapse there; a string check is never the input).
* **the effective profile** — :func:`resolve_profile`: with no ``?profile=`` the dashboard served
  the LAUNCH profile, which on a shared host is the owner's. For a person it now means their own
  orchestrator root, and a named profile must be one step 5 already let them use.
* **the memory buckets** — :func:`memory_dirs_for`: per-subject memory lives in
  ``memories/u/<subject>``; a person sees their own bucket, never a neighbour's.
* **card materials** — :func:`task_visible` / :func:`require_task` (read) and
  :func:`task_writable` / :func:`require_task_write` (change). A card, its comments, its log and
  its attachments are TEAM material: any verified subject of the installation may read them, and
  only an anonymous call is refused. Ownership (``created_by``/``assignee`` against the person's
  stamps) gates the operations that CHANGE the card — a status transition, an upload, a delete —
  so one person cannot rewrite another's work without holding it. Delivery addresses
  (:func:`delivery_targets`) come from the same registry: a person's own stamps are the route the
  notifier already uses.
* **what stays private** — sessions (``hermes_acl_scope``) and the personal profile
  (``hermes_acl_profiles``). A card that belongs to the PERSONAL contour (explicit
  ``personal:`` tenant marker, see :func:`personal_contour_of`) is private to its person; no such
  card exists today, and the marker is the only way one becomes private.

**The switch.** ``acl.multi_user`` — the flag step 3 turned on for persons and step 5 reuses for
profiles; a results boundary is a boundary between PEOPLE, so the same flag. With it off every
function here answers "unchanged": ``file_roots`` returns ``None`` (unscoped), ``resolve_profile``
returns the requested name, ``memory_dirs_for`` returns every bucket as before. A single-user
installation therefore keeps the exact behaviour it had.

**Fail closed.** A person whose subject cannot be established, or whose registry is broken or
absent, gets an EMPTY root list and an empty stamp set — every path and every card is refused.
Only the host owner (``gateway.owner_identity.is_host_owner``) keeps the host-wide view, because
a broken registry must not lock the operator out of their own dashboard.
"""
from __future__ import annotations

import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

#: Refusal detail for a path outside the caller's root. Deliberately the same text as a path the
#: managed-files policy refuses: a distinct message would confirm that the file exists.
OUTSIDE_ROOT = "Path is outside the caller's allowed roots"
#: The miss a foreign card gets — byte-identical to an absent task id (no id oracle).
TASK_NOT_FOUND = "Task not found"
#: A card owned by somebody else: naming it would confirm the id exists, so a write on a foreign
#: card answers 403 with the same generic text the read boundary uses for other people's data.
TASK_NOT_YOURS = "Task belongs to another person"
#: The tenant prefix that puts a card in the PERSONAL contour. A card with
#: ``tenant="personal:<subject>"`` is private to that person; every other card is team material.
PERSONAL_TENANT_PREFIX = "personal:"


def _multi_user() -> bool:
    """``acl.multi_user`` — the person-level switch (steps 3 and 5 use the same one).

    The flag sits in ``config.yaml``, and throughout this tree the way to read it is
    :func:`hermes_acl_identity.multi_user_enabled` — but that goes through ``load_config()``,
    which ENSURES the home skeleton. This module is asked on every file listing, so a read would
    materialise ``~/.hermes`` as a side effect of browsing a directory; a tree with no home has
    no ``config.yaml`` and therefore no flag, so the answer is settled before the loader runs.
    """
    try:
        from hermes_constants import get_hermes_home

        if not get_hermes_home().is_dir():
            return False
    except Exception:
        logger.debug("acl results: hermes home unresolvable", exc_info=True)
        return False
    try:
        from hermes_acl_identity import multi_user_enabled

        return bool(multi_user_enabled())
    except Exception:  # an import failure must never ENABLE the boundary's absence
        logger.debug("acl results: multi_user flag unreadable", exc_info=True)
        return False


def request_subject(request: Any) -> str:
    """The VERIFIED subject a request acts as (``""`` when it carries none).

    Only what the auth gate attached: a native grant, a dashboard login, or an identity header
    when ``acl.identity_trust`` declares a proxy trusted. An unsigned header is not an identity.
    """
    if request is None:
        return ""
    try:
        from hermes_acl_scope import request_identity_subject

        return str(request_identity_subject(request) or "").strip()
    except Exception:
        logger.debug("acl results: subject resolution failed", exc_info=True)
        return ""


def _is_owner(subject: Any) -> bool:
    if not subject:
        return False
    try:
        from gateway.owner_identity import is_host_owner

        return bool(is_host_owner(subject))
    except Exception:
        logger.debug("acl results: owner identity unavailable", exc_info=True)
        return False


def scoped(request: Any) -> bool:
    """Whether THIS request is subject-scoped (multi-user on and the caller is not the owner)."""
    return scoped_subject(request_subject(request))


def scoped_subject(subject: Any) -> bool:
    """Whether *subject* is subject-scoped — the same rule as :func:`scoped`, without a request.

    A gateway delivery seam (MEDIA attachment, background reply) has no FastAPI request: the
    sender is the turn's actor (``HERMES_SESSION_ACTOR_ID``). It needs the identical answer, so
    the switch and the owner bypass live here once instead of being re-derived per call site.
    """
    if not _multi_user():
        return False
    return not _is_owner(str(subject or "").strip())


def _registry():
    """The bindings registry, or ``None`` when absent. Raises on a corrupt one (fail closed)."""
    import hermes_acl_bindings as hb

    return hb.load_cached()


def _registry_errors() -> tuple[type[BaseException], ...]:
    try:
        from hermes_acl_bindings import BindingsError

        return (BindingsError,)
    except ImportError:  # pragma: no cover - the registry ships with this tree
        return (_NeverRaised,)


class _NeverRaised(Exception):
    """Placeholder so the ``except`` clause stays valid in a build without the registry."""


def stamps(subject: Any) -> set[str]:
    """Every label the person owning *subject* may appear as on a stored row.

    The owner stamps the session layer already compares (:func:`hermes_acl_scope.owner_stamps`)
    plus the person's own orchestrator profile names: a card created by the CLI stores the profile
    name as ``created_by``, while the dashboard stores the signed-in subject.
    """
    text = str(subject or "").strip()
    if not text:
        return set()
    out: set[str] = set()
    try:
        from hermes_acl_scope import owner_stamps

        out.update(owner_stamps(text))
    except _registry_errors() as exc:
        logger.warning("acl results: bindings registry unusable — no stamp (%s)", exc)
        return set()
    try:
        registry = _registry()
    except _registry_errors() as exc:
        logger.warning("acl results: bindings registry unusable — no profile stamps (%s)", exc)
        return set()
    if registry is not None:
        out.update(registry.personal_profiles_of(text))
    out.discard("")
    return out


def profile_names(subject: Any) -> tuple[str, ...]:
    """The person's own orchestrator profiles (shared ones excluded: they are nobody's own)."""
    text = str(subject or "").strip()
    if not text:
        return ()
    try:
        registry = _registry()
    except _registry_errors() as exc:
        logger.warning("acl results: bindings registry unusable — no own profile (%s)", exc)
        return ()
    if registry is None:
        return ()
    return tuple(registry.personal_profiles_of(text))


def resolve_profile(request: Any, requested: Optional[str]) -> Optional[str]:
    """The profile a results request actually acts on; ``None`` keeps "the current profile".

    * scoping OFF, or the host owner — *requested* unchanged (legacy behaviour, byte for byte);
    * a person naming a profile — that name, after step 5's visibility check (a foreign name
      answers 404, so the roster is not an enumeration oracle);
    * a person naming NONE (the dashboard's own default) — their own orchestrator root. Without
      this the route fell back to the process's LAUNCH profile, which on a shared host belongs to
      somebody else; that is the leak this step closes for skills, memory and file previews.
    """
    if not scoped(request):
        return requested
    name = str(requested or "").strip()
    owns = profile_names(request_subject(request))
    if not name or name.lower() == "current":
        if owns:
            return owns[0]
        _refuse_profile("")
        return None  # pragma: no cover - _refuse_profile always raises
    if name not in owns:
        from hermes_acl_profiles import require_visible

        require_visible(request, name)  # 404 for a foreign name, 200 for a shared one
    return name


def _refuse_profile(profile: Any) -> None:
    from fastapi import HTTPException

    logger.info("acl results: refusing profile %r — the caller owns none", str(profile))
    raise HTTPException(status_code=404, detail=f"Profile '{profile}' does not exist.")


# ────────────────────────────────────────────────────────────────────────────
# Filesystem roots
# ────────────────────────────────────────────────────────────────────────────


def _real(path: Any) -> Optional[Path]:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _profile_home(name: str) -> Optional[Path]:
    try:
        from hermes_cli.profiles import get_profile_dir

        return _real(get_profile_dir(name))
    except Exception:
        logger.debug("acl results: profile dir unresolvable for %r", name, exc_info=True)
        return None


def _profile_workspace(name: str) -> Optional[Path]:
    """A profile's own working tree: ``~/workspaces/<profile>`` when it exists.

    The convention every profile's ``terminal.cwd`` follows (``03-software-engineer``,
    ``01-<login>``); the root config's cwd is just one profile's instance of it. Resolved against
    the OS home and returned only when the directory is real — a name that resolves nowhere
    contributes nothing.
    """
    if not name or "/" in name or name.startswith("."):
        return None
    try:
        base = Path(os.path.expanduser("~")) / "workspaces" / name
    except Exception:  # health: allow BLE001 -- an unresolvable home grants nothing
        return None
    if base.is_symlink():
        # A symlinked ``~/workspaces/<profile>`` would silently turn the caller's root into whatever
        # it points at (a neighbour profile, ``/etc``): the convention is a real directory or nothing.
        return None
    return _real(base) if base.is_dir() else None


def _exchange_dirs() -> list[Path]:
    """The team exchange directory: deliverables handed to whoever asked for them.

    A personal profile's working directory belongs to its owner, so a file an agent produced for
    another person there answers 403 to that person — correct, and useless as a delivery. Files in
    ``{HERMES_HOME}/shared`` are readable by every VERIFIED person: the producer writes there, the
    requester downloads there. An unidentified caller never reaches this list (the caller gates it
    on :func:`_verified`), and a missing directory contributes nothing.
    """
    try:
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "shared"
    except Exception:  # health: allow BLE001 -- a stripped build without the constant
        return []
    return [path] if path.is_dir() else []


def _team_card_dirs(subject: Any) -> list[Path]:
    """``kanban/workspaces/<id>`` and ``kanban/attachments/<id>`` of the cards TEAM may read.

    What the team produced belongs to the team: a card's folder and its attachments are reachable
    by any verified person, which is what makes a producer's deliverable openable by whoever needs
    it. The one exclusion is a PERSONAL-contour card (:func:`personal_contour_of`): it is the
    person's own business, so its folders stay theirs alone. An unreadable board yields no card
    directories — a catalogue that cannot be read must not widen the root.
    """
    if not _verified(subject):
        return []
    rows = _owned_task_rows()
    if not rows:
        return []
    try:
        from hermes_cli import kanban_db as kb

        bases = (_real(kb.workspaces_root()), _real(kb.attachments_root()))
    except Exception:  # health: allow BLE001 -- a board whose roots cannot be named grants nothing
        logger.debug("acl results: kanban roots unavailable for card roots", exc_info=True)
        return []
    marks = stamps(subject)
    out: list[Path] = []
    for row in rows:
        task_id = row["id"]
        private_to = _personal_contour_of_row(row)
        if private_to and not (row["stamps"] & marks or private_to == str(subject).strip()):
            continue  # somebody else's personal card
        for base in bases:
            if base is None:
                continue
            candidate = base / task_id
            if candidate.is_dir():
                resolved = _real(candidate)
                if resolved is not None and resolved not in out:
                    out.append(resolved)
    return out


#: ``(path, mtime_ns, size) -> terminal.cwd`` for the shared work-dir lookup. ``file_roots`` runs
#: on EVERY file listing, and parsing thirteen 89-key config files there cost ~700 ms a call; the
#: stamp re-reads only when a config actually changed. Bounded by the number of shared profiles.
_work_dir_cache: dict[tuple[str, int, int], Optional[Path]] = {}


def _shared_work_dirs() -> list[Path]:
    """The working directories of the SHARED profiles — where the team's own work lands.

    A script written by the software-engineering profile lives in that profile's
    ``terminal.cwd`` (``/home/hermes/workspaces/03-software-engineer`` by default), not inside
    any card folder. Since that profile is shared, what it produced is team material and every
    verified person must be able to open it — otherwise "the team's code is shared" stops at the
    card attachment and the producer's own working tree stays unreachable.

    Only registry-declared SHARED profiles contribute (:meth:`BindingsRegistry.shared_profiles`),
    never a personal one: ``01-<login>`` keeps its root to its owner. A profile whose home or
    config cannot be read contributes nothing — an unreadable registry or config must not widen
    the boundary. Same-directory and sub-directory results are de-duplicated by the caller.
    """
    try:
        registry = _registry()
    except _registry_errors():
        return []
    if registry is None:
        return []
    try:
        shared = registry.shared_profiles()
    except _registry_errors() as exc:
        logger.warning("acl results: bindings registry unusable — no shared work dirs (%s)", exc)
        return []
    out: list[Path] = []
    for name in shared:
        home = _profile_home(name)
        if home is None:
            continue
        cwd = _profile_work_dir(home)
        if cwd is not None and cwd not in out:
            out.append(cwd)
    return out


def _profile_work_dir(home: Path) -> Optional[Path]:
    """A profile's ``terminal.cwd`` as an absolute directory, or ``None``.

    Read EXACTLY as written (``read_user_config_raw``): the value is a plain path, and the
    defaults/expansion of ``load_config()`` would both materialise a home and turn a relative
    ``"."`` into the process's cwd — the launch profile's own tree, which is not this profile's.

    Memoised on ``(path, mtime_ns, size)``: parsing thirteen full configs on every file listing is
    ~700 ms, while a config that has not changed cannot have a different ``cwd``. The first call
    in a process pays that once (~770 ms), every later one ~3 ms; a ``touch`` re-reads one file.
    """
    config_path = Path(home) / "config.yaml"
    try:
        stat = config_path.stat()
        key = (str(config_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if key in _work_dir_cache:
        return _work_dir_cache[key]
    try:
        from hermes_cli.config import read_user_config_raw

        data = read_user_config_raw(config_path)
    except Exception:  # health: allow BLE001 -- an unreadable config grants nothing
        logger.debug("acl results: shared profile config unreadable under %s", home, exc_info=True)
        _work_dir_cache[key] = None
        return None
    terminal = data.get("terminal") if isinstance(data, dict) else None
    raw = terminal.get("cwd") if isinstance(terminal, dict) else None
    text = str(raw or "").strip()
    resolved: Optional[Path] = None
    if text and Path(text).is_absolute():
        candidate = _real(text)
        if candidate is not None and candidate.is_dir():
            resolved = candidate
    _work_dir_cache[key] = resolved
    if len(_work_dir_cache) > 256:  # a long-lived process seeing many temp homes (tests, probes)
        _work_dir_cache.pop(next(iter(_work_dir_cache)))
    return resolved


def _owned_task_rows() -> list[dict]:
    """``[{"id", "stamps", "tenant"}, ...]`` for the whole board; ``[]`` if unreadable.

    Deliberately board-wide and unfiltered: the visibility test needs the person's full stamp set
    and the card's personal-contour marker, both Python-side policy, so the query stays a plain
    read and the filter runs in the callers. An unreadable board (missing, corrupt, wrong schema)
    is no cards.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
    except Exception:  # health: allow BLE001 -- no board, no card roots: never widen the boundary
        logger.debug("acl results: kanban board unavailable for card roots", exc_info=True)
        return []
    try:
        raw = conn.execute("SELECT id, created_by, assignee, tenant FROM tasks").fetchall()
    except Exception:  # health: allow BLE001 -- an unreadable table is no cards, not every card
        logger.debug("acl results: tasks unreadable for card roots", exc_info=True)
        return []
    finally:
        with suppress(Exception):
            conn.close()
    rows: list[dict] = []
    for row in raw:
        keys = row.keys()
        task_id = row["id"] if "id" in keys else None
        if not task_id:
            continue
        rows.append({"id": str(task_id),
                     "stamps": {row["created_by"], row["assignee"]},
                     "tenant": row["tenant"] if "tenant" in keys else None})
    return rows


def _personal_contour_of_row(row: dict) -> str:
    """The subject a board ROW is private to, from its ``tenant`` marker (else ``""``)."""
    tenant = str(row.get("tenant") or "")
    if not tenant.startswith(PERSONAL_TENANT_PREFIX):
        return ""
    return tenant[len(PERSONAL_TENANT_PREFIX):].strip()


def file_roots(request: Any) -> Optional[tuple[Path, ...]]:
    """The directories this request may read and write; ``None`` = unscoped (legacy answer).

    An EMPTY tuple is the fail-closed answer (subject not established, registry broken, or the
    person owns no profile): every path is then outside the root.
    """
    if not scoped(request):
        return None
    return subject_roots(request_subject(request))


def subject_roots(subject: Any) -> tuple[Path, ...]:
    """The directories *subject* may read and write — the root set of :func:`file_roots`.

    Split out so a caller that already knows WHO is asking (a gateway turn's actor) does not have
    to fabricate an HTTP request: the roots themselves are computed exactly as before.
    """
    # A signed-in dashboard subject is a composite (``auth:v1:[provider, issuer, user_id]``), which
    # the bindings registry does not key profiles by: resolving it to the person's stamps first is
    # what makes the roots non-empty for a cookie login. A bare stamp still answers as before.
    names: list[str] = []
    for mark in sorted(stamps(subject)):
        for name in profile_names(mark):
            if name not in names:
                names.append(name)
    roots: list[Path] = []
    for name in names:
        home = _profile_home(name)
        if home is not None and home not in roots:
            roots.append(home)
        # A profile's own working tree (``~/workspaces/<profile>``) is where its sessions write and
        # what its owner asks to download; the home alone is not enough — files produced before the
        # cwd was pinned live there, and refusing them back to their AUTHOR is a dead end.
        work = _profile_workspace(name)
        if work is not None and work not in roots:
            roots.append(work)
    for path in _team_card_dirs(subject):
        if path not in roots:
            roots.append(path)
    if _verified(subject):
        for path in _exchange_dirs():
            if path not in roots:
                roots.append(path)
        for path in _shared_work_dirs():
            if path not in roots:
                roots.append(path)
    return tuple(roots)


def path_allowed(request: Any, target: Any) -> bool:
    """Whether *target* (already resolved) lies inside the caller's root (or is the root)."""
    roots = file_roots(request)
    if roots is None:
        return True
    return _within(roots, target)


def path_allowed_for_subject(subject: Any, target: Any) -> bool:
    """Whether *target* lies inside *subject*'s root — the :func:`path_allowed` rule, no request.

    ``None``-root semantics are preserved for the caller's sake: an UNSCOPED subject (multi-user
    off, or the host owner) is allowed anything, exactly as :func:`path_allowed` answers for their
    request. A scoped subject with no established identity gets an empty root set and is refused.
    """
    if not scoped_subject(subject):
        return True
    return _within(subject_roots(subject), target)


def _within(roots, target: Any) -> bool:
    """Containment of the resolved *target* in *roots* (the root itself included)."""
    resolved = _real(target)
    if resolved is None:
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


# ────────────────────────────────────────────────────────────────────────────
# Writing into the team exchange — one subfolder per person (L2-c)
# ────────────────────────────────────────────────────────────────────────────
#
# ``{HERMES_HOME}/shared`` has no OS-level owner: one Unix account, one group, ``g+w``. So the
# ``overwrite`` flag in an upload request decided whether a colleague's prepared deliverable
# survived, and nothing asked WHO wrote the file. That is the incident this section closes:
#
# * a scoped person's write lands in ``shared/<subject-slug>/`` — never in the exchange root and
#   never in somebody else's subfolder. The redirect makes the layout self-explaining and removes
#   the authorship question for anything written after this change;
# * a root-level path is not refused but REDIRECTED (``shared/team.docx`` →
#   ``shared/person_alice/team.docx``): the caller's intent ("hand this to the team") is honoured
#   and a file the team already put in the root is left untouched instead of silently replaced;
# * a named foreign subfolder is refused — writing into a neighbour's folder is not a handover;
# * the authorship journal (``shared/.authorship.jsonl``, last record per resolved path wins) is
#   the backstop for files that predate the layout: overwriting one created by ANOTHER subject is
#   a 409, while rewriting your own is exactly what ``overwrite`` is for.
#
# Unscoped (``acl.multi_user`` off, or the host owner) means "unchanged": no redirect, no
# journal check, the byte-for-byte behaviour before this change.

#: The journal that records who created a path under the exchange.
EXCHANGE_AUTHORSHIP_FILE = ".authorship.jsonl"


def exchange_dirs() -> tuple[Path, ...]:
    """The resolved exchange directories (``{HERMES_HOME}/shared``), empty when absent."""
    return tuple(path for path in (_real(p) for p in _exchange_dirs()) if path is not None)


def subject_slug(subject: Any) -> str:
    """The filesystem-safe folder name for *subject*, or ``""`` when nothing usable is left."""
    try:
        from tools.memory_tool import _subject_dirname

        return _subject_dirname(str(subject or ""))
    except Exception:  # health: allow BLE001 -- a stripped build keeps the previous behaviour
        logger.debug("acl results: subject folder name unavailable", exc_info=True)
        return ""


def _exchange_of(target: Path) -> Optional[Path]:
    """The exchange directory *target* lives in (including the root itself), else ``None``."""
    for directory in exchange_dirs():
        if target == directory or directory in target.parents:
            return directory
    return None


def _refuse_exchange_write(detail: str) -> None:
    from fastapi import HTTPException

    logger.info("acl results: refusing a write into another person's exchange folder")
    raise HTTPException(status_code=403, detail=detail)


def exchange_write_target(request: Any, target: Any) -> Path:
    """Where a write to *target* actually lands: the caller's own exchange subfolder when scoped.

    Unscoped calls (multi-user off, the host owner, an internal caller) return *target* unchanged.
    A scoped caller writing outside the exchange also gets *target* back — containment there is
    :func:`require_path`'s job, not this one. Inside the exchange the first path component decides:
    the caller's own slug (or nothing yet — a root-level name) is redirected into that slug, and
    a different person's slug is refused.
    """
    resolved = _real(target)
    if resolved is None:
        return Path(target)
    if not scoped(request):
        return resolved
    exchange = _exchange_of(resolved)
    if exchange is None:
        return resolved
    slug = subject_slug(verified_subject(request))
    if not slug:
        # A scoped caller with no established identity: fail closed, like every other root.
        _refuse_exchange_write(OUTSIDE_ROOT)
    relative = resolved.relative_to(exchange)
    if relative.parts and relative.parts[0] == slug:
        return resolved
    if len(relative.parts) <= 1:
        # A name at the exchange root: hand it to the caller's own folder instead of the root.
        return exchange / slug / resolved.name
    _refuse_exchange_write(
        "The team exchange is writable only inside your own folder "
        f"({exchange.name}/{slug}/)")


def _exchange_journal_path() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:  # health: allow BLE001 -- no home, no journal, no backstop
        logger.debug("acl results: hermes home unresolvable for the exchange journal", exc_info=True)
        return None
    return home / "shared" / EXCHANGE_AUTHORSHIP_FILE


def exchange_author(path: Any) -> str:
    """The subject the journal records as the creator of *path* (``""`` when unrecorded)."""
    resolved = _real(path)
    journal = _exchange_journal_path()
    if resolved is None or journal is None or not journal.is_file():
        return ""
    author = ""
    try:
        with open(journal, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    import json

                    row = json.loads(line)
                except ValueError:
                    continue  # a torn line is not evidence of authorship
                if not isinstance(row, dict) or str(row.get("path") or "") != str(resolved):
                    continue
                author = str(row.get("subject") or "")  # last record for a path wins
    except OSError:
        logger.warning("acl results: exchange authorship journal unreadable", exc_info=True)
        return ""
    return author


def require_exchange_authorship(request: Any, target: Any) -> None:
    """Refuse replacing a file ANOTHER subject created in the exchange. No-op when unscoped.

    The subfolder layout already keeps people apart; this is the backstop for a file that sits
    where the layout did not put it (one written before this change, or by an unscoped writer).
    An unrecorded file is not refused: "cannot tell" must not turn into "not yours" and lock a
    person out of their own folder.
    """
    if not scoped(request):
        return
    resolved = _real(target)
    if resolved is None or _exchange_of(resolved) is None or not resolved.is_file():
        return
    subject = verified_subject(request)
    if not subject:
        _refuse_exchange_write(OUTSIDE_ROOT)
    author = exchange_author(resolved)
    if not author or author == subject:
        return
    logger.info("acl results: refusing to overwrite another subject's exchange file")
    from fastapi import HTTPException

    raise HTTPException(
        status_code=409,
        detail="File already exists and was created by another user; write it under your own name")


def require_exchange_removal(request: Any, target: Any) -> None:
    """Refuse DELETING an exchange file that is not this subject's own. No-op when unscoped.

    Destroying a colleague's deliverable is the same harm as overwriting it, and the delete route
    only asked whether the path was inside the caller's roots. A file in another person's
    subfolder, or one the journal attributes to another subject, is refused; everything outside
    the exchange is untouched by this rule.
    """
    if not scoped(request):
        return
    resolved = _real(target)
    exchange = _exchange_of(resolved) if resolved is not None else None
    if resolved is None or exchange is None:
        return
    subject = verified_subject(request)
    slug = subject_slug(subject)
    if not subject or not slug:
        _refuse_exchange_write(OUTSIDE_ROOT)
    relative = resolved.relative_to(exchange)
    foreign_folder = bool(relative.parts) and relative.parts[0] != slug
    author = exchange_author(resolved)
    if foreign_folder or (author and author != subject):
        logger.info("acl results: refusing to delete another subject's exchange file")
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail="Only the user who created this exchange file may delete it")


def record_exchange_authorship(request: Any, target: Any) -> None:
    """Record the caller as the creator of *target* when it lives in the exchange. No-op otherwise.

    Append-only JSONL keyed by the resolved path; the last line for a path wins, so a rewrite by
    the same person is idempotent and a hand-built journal copy cannot be "corrected" in place.
    """
    if not scoped(request):
        return
    resolved = _real(target)
    if resolved is None or _exchange_of(resolved) is None:
        return
    subject = verified_subject(request)
    if not subject:
        return
    journal = _exchange_journal_path()
    if journal is None:
        return
    try:
        import json
        from datetime import UTC, datetime

        journal.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "path": str(resolved),
            "subject": subject,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError):
        # The journal is a backstop, not the boundary (the subfolder layout is): a failed write
        # must not fail the upload that already succeeded.
        logger.warning("acl results: could not record exchange authorship", exc_info=True)



def require_path(request: Any, target: Any) -> None:
    """Refuse a path outside the caller's root. Callers pass the RESOLVED path.

    403 (not 404): the caller named the path explicitly and a 404 would be indistinguishable from
    a genuine miss only if existence were unknown — the policy decides on containment, so the
    honest answer is "not yours". Nothing in the body names what lives there.
    """
    if path_allowed(request, target):
        return
    from fastapi import HTTPException

    logger.info("acl results: refusing a path outside the caller's root")
    raise HTTPException(status_code=403, detail=OUTSIDE_ROOT)


# ────────────────────────────────────────────────────────────────────────────
# Memory buckets
# ────────────────────────────────────────────────────────────────────────────


def memory_dirs_for(request: Any, all_dirs: Sequence[Path]) -> list[Path]:
    """The memory buckets this request may see out of *all_dirs* (``memories`` + ``u/<subject>``).

    Unscoped: every bucket, exactly as before (the owner's "delete everything" panel). Scoped: the
    caller's OWN ``u/<subject>`` bucket only. The legacy base is deliberately NOT kept: it holds
    the notes written before per-subject memory existed, i.e. the host owner's, and a shared
    profile's panel must not count — still less erase — a neighbour's notes.
    """
    if not scoped(request):
        return list(all_dirs)
    subject = request_subject(request)
    if not subject:
        return []
    try:
        from tools.memory_tool import _subject_dirname

        bucket = _subject_dirname(subject)
    except Exception:
        logger.debug("acl results: memory subject name unavailable", exc_info=True)
        return []
    if not bucket:
        return []
    return [Path(d) for d in all_dirs if Path(d).name == bucket and Path(d).parent.name == "u"]


# ────────────────────────────────────────────────────────────────────────────
# Card materials — TEAM by default, ownership only gates CHANGES
# ────────────────────────────────────────────────────────────────────────────
#
# The rule from the owner: what the team produced belongs to the team. A card, its comments, its
# log and its attachments are the team's working material — a file a document producer made on a
# card must be downloadable by the person who ordered it, by the reviewer, by anyone. Only two
# things stay private: correspondence (sessions, gated in hermes_acl_scope) and the personal
# profile (hermes_acl_profiles). So the READ boundary here is "is this a real caller at all",
# and ownership only decides who may CHANGE somebody's card.


def personal_contour_of(task: Any) -> str:
    """The subject a card is PRIVATE to, or ``""`` when it is team material.

    Opt-in and explicit: a card enters the personal contour only by carrying a
    ``tenant="personal:<subject>"`` marker. Nothing sets it today, so every existing card stays
    team material; the marker exists so a future personal to-do list has a gate instead of a
    comment saying "do not show this".
    """
    tenant = str(getattr(task, "tenant", "") or "").strip()
    if not tenant.startswith(PERSONAL_TENANT_PREFIX):
        return ""
    return tenant[len(PERSONAL_TENANT_PREFIX):].strip()


def _verified(subject: str) -> bool:
    """Whether *subject* names somebody: a login or a loginless native grant — not "unbound".

    :func:`gateway.session_identity.authenticated_subject` namespaces every dashboard identity as
    ``auth:v1:[provider, issuer, user]`` and fills the user with ``unbound`` when the session
    carried none. That string is non-empty, so an "is there a subject at all" test would treat an
    anonymous tab as a colleague; this asks what the user slot actually says.
    """
    text = str(subject or "").strip()
    if not text:
        return False
    try:
        from gateway.session_identity import parse_authenticated_subject

        parsed = parse_authenticated_subject(text)
    except Exception:  # health: allow BLE001 -- an unparsable form is judged by its text alone
        parsed = None
    if parsed is not None:
        return str(parsed[2] or "").strip() not in {"", "unbound"}
    return True


def verified_subject(request: Any) -> str:
    """The request's subject when it names a real person, else ``""`` (anonymous tab, no grant)."""
    subject = request_subject(request)
    return str(subject).strip() if _verified(subject) else ""


def task_visible(request: Any, task: Any) -> bool:
    """Whether the request's person may READ *task* and its materials.

    Team material (the default): readable by any verified caller — owner, or any subject that
    resolves. Unscoped (multi_user off / host owner): everything, exactly as before. Personal
    contour: only the person the card is marked for.
    """
    if not scoped(request):
        return True
    subject = request_subject(request)
    if not _verified(subject):
        return False
    private_to = personal_contour_of(task)
    if not private_to:
        return True
    if private_to == str(subject).strip():
        return True
    return bool(stamps(subject) & {private_to})


def task_writable(request: Any, task: Any) -> bool:
    """Whether the request's person may CHANGE *task*: its owner (creator or assignee) only.

    A team card is still one person's work order. Reading is shared; moving its status, uploading
    into it, deleting it are the holder's decision, so the ownership stamps are compared exactly
    as the read boundary used to.
    """
    if not scoped(request):
        return True
    subject = request_subject(request)
    marks = stamps(subject)
    if not marks:
        return False
    row = {getattr(task, "created_by", None), getattr(task, "assignee", None)}
    return bool(row & marks)


def card_verdict(subject: Any, task: Any) -> str:
    """How *subject* relates to *task*, for the delivery path to name out loud.

    ``"unscoped"`` — multi-user is off, so no relation is enforceable (legacy behaviour);
    ``"owner"`` — the host owner, who reaches every card;
    ``"member"`` — the person's stamps match the card's ``created_by``/``assignee``;
    ``"foreign"`` — nobody this card knows (an unknown subject lands here too: an establishable
    person is the only thing that can be a member, so "cannot tell" is not "belongs").

    This is :func:`task_visible` asked with a SUBJECT instead of an HTTP request: the gateway
    delivery path holds a subscription (platform + chat), not a request object, and must reach
    the same answer without inventing a second policy.
    """
    if not _multi_user():
        return "unscoped"
    text = str(subject or "").strip()
    if _is_owner(text):
        return "owner"
    marks = stamps(text)
    if not marks:
        return "foreign"
    row = {getattr(task, "created_by", None), getattr(task, "assignee", None)}
    return "member" if row & marks else "foreign"


def require_task(request: Any, task: Any, task_id: Any = "") -> None:
    """Refuse an unreadable card with the SAME 404 an absent id gets (no id oracle).

    Reads, not writes: a card and its attachments are team material, so this refuses only an
    anonymous call or a personal-contour card. Write routes call :func:`require_task_write`.
    """
    if task is not None and task_visible(request, task):
        return
    from fastapi import HTTPException

    logger.info("acl results: refusing card %r for this caller", str(task_id))
    raise HTTPException(status_code=404, detail=TASK_NOT_FOUND)


def require_task_write(request: Any, task: Any, task_id: Any = "") -> None:
    """Refuse a CHANGE to a card the caller does not hold (403, not 404: it is readable).

    A 404 here would be a lie — the caller can read the card a line earlier — and the id is
    already known to them, so nothing is leaked by naming the refusal.
    """
    if task is not None and task_writable(request, task):
        return
    from fastapi import HTTPException

    logger.info("acl results: refusing write to card %r for this caller", str(task_id))
    raise HTTPException(status_code=403, detail=TASK_NOT_YOURS)


def delivery_targets(subject: Any) -> tuple[str, ...]:
    """Every address that reaches the person owning *subject*, for a notify subscription.

    The notifier's own route: it matches a subscription's ``user_id`` against the owning
    person's stamps, so handing back the full stamp set is what makes a created-from-the-UI card
    reach the person who ordered it instead of only the orchestrator profile that hosted the turn.
    """
    marks = stamps(subject)
    out: list[str] = []
    for mark in (str(subject or "").strip(), *sorted(marks)):
        if mark and mark not in out:
            out.append(mark)
    return tuple(out)

