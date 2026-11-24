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
  the registry, plus the directories of the CARDS they own (``kanban/workspaces/<id>``,
  ``kanban/attachments/<id>``). Everything else on the host is outside the root and refused by
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
    if not _multi_user():
        return False
    return not _is_owner(request_subject(request))


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


def _own_card_dirs(subject: Any) -> list[Path]:
    """``kanban/workspaces/<id>`` and ``kanban/attachments/<id>`` of the cards *subject* owns.

    Read from the board itself (the same database the dashboard serves): a task is the person's
    when its ``created_by`` or ``assignee`` carries one of their stamps. A read failure yields no
    card directories — a catalogue that cannot be read must not widen the root.
    """
    marks = stamps(subject)
    if not marks:
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
    out: list[Path] = []
    for task_id, stamps_on_row in rows:
        if not task_id or not (stamps_on_row & marks):
            continue
        for base in bases:
            if base is None:
                continue
            candidate = base / task_id
            if candidate.is_dir():
                resolved = _real(candidate)
                if resolved is not None and resolved not in out:
                    out.append(resolved)
    return out


def _owned_task_rows() -> list[tuple[str, set[Any]]]:
    """``[(task_id, {created_by, assignee}), ...]`` for the whole board; ``[]`` if unreadable.

    Deliberately board-wide and unfiltered: the ownership test needs the person's full stamp set,
    which is a Python-side policy, so the query stays a plain read and the filter runs in
    :func:`_own_card_dirs`. An unreadable board (missing, corrupt, wrong schema) is no cards.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(db_path=kb.kanban_db_path())
    except Exception:  # health: allow BLE001 -- no board, no card roots: never widen the boundary
        logger.debug("acl results: kanban board unavailable for card roots", exc_info=True)
        return []
    try:
        raw = conn.execute("SELECT id, created_by, assignee FROM tasks").fetchall()
    except Exception:  # health: allow BLE001 -- an unreadable table is no cards, not every card
        logger.debug("acl results: tasks unreadable for card roots", exc_info=True)
        return []
    finally:
        with suppress(Exception):
            conn.close()
    rows: list[tuple[str, set[Any]]] = []
    for row in raw:
        task_id = row["id"] if "id" in row.keys() else None
        if not task_id:
            continue
        rows.append((str(task_id), {row["created_by"], row["assignee"]}))
    return rows


def file_roots(request: Any) -> Optional[tuple[Path, ...]]:
    """The directories this request may read and write; ``None`` = unscoped (legacy answer).

    An EMPTY tuple is the fail-closed answer (subject not established, registry broken, or the
    person owns no profile): every path is then outside the root.
    """
    if not scoped(request):
        return None
    subject = request_subject(request)
    roots: list[Path] = []
    for name in profile_names(subject):
        home = _profile_home(name)
        if home is not None and home not in roots:
            roots.append(home)
    for path in _own_card_dirs(subject):
        if path not in roots:
            roots.append(path)
    return tuple(roots)


def path_allowed(request: Any, target: Any) -> bool:
    """Whether *target* (already resolved) lies inside the caller's root (or is the root)."""
    roots = file_roots(request)
    if roots is None:
        return True
    resolved = _real(target)
    if resolved is None:
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


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


def task_visible(request: Any, task: Any) -> bool:
    """Whether the request's person may READ *task* and its materials.

    Team material (the default): readable by any verified caller — owner, or any subject that
    resolves. Unscoped (multi_user off / host owner): everything, exactly as before. Personal
    contour: only the person the card is marked for.
    """
    if not scoped(request):
        return True
    subject = request_subject(request)
    if not str(subject or "").strip():
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

