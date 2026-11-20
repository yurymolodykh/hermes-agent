"""The bindings registry: which profiles belong to which PERSON, for every ACL comparison.

Hermes is single-user: every signed-in surface of the host is "the owner", and
``gateway.owner_identity.same_owner`` answers "is this stored owner stamp you?" by comparing the
stamps of ONE identity. Step 3 of the multi-user mission gives each person a stable subject
(``person/<login>``) — and the moment it does, the owner's existing rows stop matching: they are
stamped ``uid:1001``, ``cron-owner``, a bare Telegram id, and step 3's caller is
``person/yury``. Measured on this install: ``same_owner('uid:1001', 'person/alice')`` is False, so
an identity that is not taught about bindings strands every session the owner already has.

This module is that missing layer, and only that layer. It answers two questions and nothing else:

* **who is this subject** — :meth:`Bindings.person_of` / :func:`same_person`, the single place a
  comparison of two subjects resolves to "same person" or "two people" for steps 3-6;
* **which profiles are whose** — :meth:`Bindings.profiles_of`, :meth:`Bindings.is_shared` (a
  shared record grants every person, the ``anyone`` principal of the directory registry), and
  :meth:`Bindings.profile_owner`.

Storage: ``<hermes root>/acl/profile-bindings.json`` — a data file, never in git, beside the
directory registry ``acl/registry.db`` of task ``t_be33a8fc``. Line-delimited formats were rejected
here for the same reason the users file uses them and the directory registry does not: this file is
one DOCUMENT (a person and the profiles they hold), not an append-only stream of independent
records, so a single JSON object validates as a whole and a half-written one is impossible.

Three rules are enforced on every load, in this order:

* **fail-closed on corruption.** A file that is present but does not parse, or parses into a shape
  that violates an invariant (a subject claimed by two people, a profile both personal and shared,
  an invalid slug, an unknown schema), raises :class:`BindingsCorrupt`. Callers turn that into a
  DENIAL (``hermes_acl_scope`` returns an empty scope, which the store renders as ``1 = 0``) and
  never into "no filter": a broken registry must stop the rights check, not open it. This is the
  accepted single point of failure the security review names (Д5) — see :class:`BindingStore` for
  the backup that makes recovery a file copy.
* **ADDITIVE, never authoritative.** A store that does not exist means "no bindings yet" and
  changes nothing: the legacy stamp comparison keeps answering exactly as it does today, so an
  installation that never created this file behaves as before. Only a PRESENT-and-broken file
  refuses.
* **0600.** The registry names who may see whose history; group/world bits are dropped at write and
  reported at load.

Nothing here decides whether ACL is ON. That is ``acl.enabled`` (``hermes_acl_scope.acl_enabled``)
and step 3 owns the first enablement: while it is false every reader of this module is a no-op, so
step 2 can create and bind the registry without moving a single right.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: Bumped only when the on-disk shape changes incompatibly; an unknown value is corrupt.
SCHEMA = 1

#: The registry holds an access model, not credentials — but it says who owns what, so 0600 as well.
FILE_MODE = 0o600

#: ``acl.bindings_file`` overrides this; the default lives beside the directory registry.
FILENAME = "profile-bindings.json"
ACL_DIRNAME = "acl"
BACKUP_DIRNAME = "backups"
BACKUP_PREFIX = "profile-bindings-"
BACKUP_LATEST = "profile-bindings-latest.json"
#: Rotated copies kept besides ``-latest``. A day of edits, not a history of the access model.
BACKUP_KEEP = 20

#: ``person/<login>`` for a human, ``shared/<name>`` for a record whose profiles everyone may use.
_PERSON_RE = re.compile(r"^person/[a-z0-9][a-z0-9._-]{0,63}$")
_SHARED_RE = re.compile(r"^shared/[a-z0-9][a-z0-9._-]{0,63}$")

#: ``hermes_constants.PROFILE_ID_RE`` is the canonical profile-name rule; imported lazily so this
#: module stays importable from a bare script (identity comparisons must not need hermes_constants).
def _profile_re():
    from hermes_constants import PROFILE_ID_RE
    return PROFILE_ID_RE


class BindingsError(Exception):
    """Base for every registry refusal. Callers must not treat one as "no bindings"."""


class BindingsCorrupt(BindingsError):
    """The file exists but is not a valid registry. EVERY rights check must deny, not skip."""


class BindingsUnavailable(BindingsError):
    """The file could not be read (I/O, permissions) although it was expected to exist."""


class BindingsConflict(BindingsError):
    """A write would break an invariant (a subject or profile claimed twice)."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def bindings_dir() -> Path:
    """``<hermes root>/acl`` — data, not the current profile (one registry serves every profile)."""
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / ACL_DIRNAME


def bindings_path() -> Path:
    """The registry file: ``acl.bindings_file`` when configured, else ``<root>/acl/profile-bindings.json``."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        configured = str(cfg_get(load_config_readonly(), "acl", "bindings_file", default="") or "").strip()
    except Exception:  # a broken config must not make the path unanswerable
        logger.debug("bindings: acl.bindings_file unreadable; using the default path", exc_info=True)
        configured = ""
    if configured:
        return Path(os.path.expanduser(os.path.expandvars(configured)))
    return bindings_dir() / FILENAME


def backups_dir(path: Optional[Path] = None) -> Path:
    """Where rotated copies of *path* live: ``<parent>/backups``."""
    return (path or bindings_path()).parent / BACKUP_DIRNAME


# ────────────────────────────────────────────────────────────────────────────
# The model
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PersonRecord:
    """One record: a person and the profiles they hold, or a shared (public) profile set.

    ``shared`` is the "общий профиль" marker: every person may use the profiles of a shared record
    (the directory registry's ``anyone`` grant, ``t_be33a8fc`` §1.2). A ``person/...`` record is
    never shared and a ``shared/...`` record always is — the two mean different things and the
    loader refuses a record that claims both.
    """

    slug: str
    display_name: str
    profiles: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    shared: bool = False
    owner: str = ""
    source: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "slug": self.slug,
            "display_name": self.display_name,
            "shared": self.shared,
            "profiles": list(self.profiles),
            "subjects": list(self.subjects),
            "owner": self.owner or self.slug,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return data

    @classmethod
    def from_json(cls, raw: Any, *, where: str) -> PersonRecord:
        if not isinstance(raw, dict):
            raise BindingsCorrupt(f"{where}: record is not an object")
        slug = raw.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise BindingsCorrupt(f"{where}: missing slug")
        shared = raw.get("shared", False)
        if not isinstance(shared, bool):
            raise BindingsCorrupt(f"{where}: 'shared' must be true/false")
        if _PERSON_RE.match(slug) and shared:
            raise BindingsCorrupt(f"{where}: person record {slug!r} cannot be shared")
        if _SHARED_RE.match(slug) and not shared:
            raise BindingsCorrupt(f"{where}: shared record {slug!r} must be marked shared")
        if not (_PERSON_RE.match(slug) or _SHARED_RE.match(slug)):
            raise BindingsCorrupt(f"{where}: slug {slug!r} is neither 'person/<login>' nor 'shared/<name>'")
        profiles = _str_tuple(raw.get("profiles", ()), where=f"{where}: profiles")
        subjects = _str_tuple(raw.get("subjects", ()), where=f"{where}: subjects")
        pattern = _profile_re()
        for profile in profiles:
            if not pattern.match(profile):
                raise BindingsCorrupt(f"{where}: invalid profile name {profile!r}")
        return cls(
            slug=slug,
            display_name=str(raw.get("display_name") or slug),
            profiles=profiles,
            subjects=subjects,
            shared=shared,
            owner=str(raw.get("owner") or slug),
            source=str(raw.get("source") or ""),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


def _str_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    """A JSON list of non-empty strings, deduped in order; anything else is corrupt."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise BindingsCorrupt(f"{where}: expected a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise BindingsCorrupt(f"{where}: {item!r} is not a bare non-empty string")
        if item not in out:
            out.append(item)
    return tuple(out)


@dataclass(frozen=True)
class Bindings:
    """A validated registry. Frozen: every change produces a new instance and an atomic rewrite."""

    persons: Mapping[str, PersonRecord] = field(default_factory=dict)
    written_at: str = ""
    written_by: str = ""
    path: Optional[Path] = None
    schema: int = SCHEMA

    # ── lookups ─────────────────────────────────────────────────────────────

    def record(self, slug: str) -> Optional[PersonRecord]:
        return self.persons.get(str(slug or "").strip())

    def person_of(self, subject: Any) -> Optional[str]:
        """The record slug *subject* belongs to: its own slug, or a record naming it. Else None.

        Exact match only — a prefix or a case fold would let ``person/yury2`` claim
        ``person/yury``'s profiles, which is precisely the confusion this file prevents.
        """
        text = str(subject or "").strip()
        if not text:
            return None
        if text in self.persons:
            return text
        for record in self.persons.values():
            if text in record.subjects:
                return record.slug
        return None

    def subjects_of(self, subject: Any) -> tuple[str, ...]:
        """Every stamp the person owning *subject* may hold: record subjects + its own slug."""
        slug = self.person_of(subject)
        if slug is None:
            return ()
        record = self.persons[slug]
        return tuple(dict.fromkeys((slug, *record.subjects)))

    def profiles_of(self, subject: Any) -> tuple[str, ...]:
        """Profiles the person owning *subject* holds, whether personal or shared."""
        slug = self.person_of(subject)
        if slug is None:
            return ()
        return self.persons[slug].profiles

    def personal_profiles_of(self, subject: Any) -> tuple[str, ...]:
        """Only the profiles of *subject*'s own record when it is a person (never shared ones)."""
        slug = self.person_of(subject)
        if slug is None:
            return ()
        record = self.persons[slug]
        return () if record.shared else record.profiles

    def is_shared(self, profile: str) -> bool:
        """Whether *profile* is a shared (public) profile: any person may use it."""
        text = str(profile or "").strip()
        if not text:
            return False
        return any(text in record.profiles for record in self.persons.values() if record.shared)

    def shared_profiles(self) -> tuple[str, ...]:
        out: list[str] = []
        for record in self.persons.values():
            if record.shared:
                for profile in record.profiles:
                    if profile not in out:
                        out.append(profile)
        return tuple(out)

    def profile_owner(self, profile: str) -> Optional[str]:
        """The record slug owning *profile* (personal records win; shared ones answer too)."""
        text = str(profile or "").strip()
        if not text:
            return None
        for record in self.persons.values():
            if not record.shared and text in record.profiles:
                return record.slug
        for record in self.persons.values():
            if record.shared and text in record.profiles:
                return record.slug
        return None

    def allows_profile(self, subject: Any, profile: str) -> bool:
        """Whether *subject* may use *profile*: its own person's profile, or a shared one."""
        if not self.persons:
            return False
        if self.is_shared(profile):
            return self.person_of(subject) is not None
        slug = self.person_of(subject)
        owner = self.profile_owner(profile)
        return bool(slug and owner and slug == owner)

    # ── serialisation ───────────────────────────────────────────────────────

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "written_at": self.written_at,
            "written_by": self.written_by,
            "persons": [self.persons[slug].to_json() for slug in sorted(self.persons)],
        }

    def render(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_json(cls, raw: Any, *, path: Optional[Path] = None) -> Bindings:
        if not isinstance(raw, dict):
            raise BindingsCorrupt("registry is not a JSON object")
        schema = raw.get("schema", SCHEMA)
        if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA:
            raise BindingsCorrupt(f"unsupported schema {schema!r} (this build understands {SCHEMA})")
        records = raw.get("persons", [])
        if not isinstance(records, list):
            raise BindingsCorrupt("'persons' must be a list")
        persons: dict[str, PersonRecord] = {}
        for index, entry in enumerate(records):
            record = PersonRecord.from_json(entry, where=f"persons[{index}]")
            if record.slug in persons:
                raise BindingsCorrupt(f"persons[{index}]: duplicate record {record.slug!r}")
            persons[record.slug] = record
        return cls(persons=persons, written_at=str(raw.get("written_at") or ""),
                   written_by=str(raw.get("written_by") or ""), path=path, schema=schema)

    def validate(self) -> Bindings:
        """The cross-record invariants. Called on load AND before every write."""
        subject_owner: dict[str, str] = {}
        profile_owner: dict[str, str] = {}
        for record in self.persons.values():
            for subject in record.subjects:
                previous = subject_owner.get(subject)
                if previous is not None and previous != record.slug:
                    raise BindingsCorrupt(
                        f"subject {subject!r} is claimed by both {previous!r} and {record.slug!r}")
                subject_owner[subject] = record.slug
            for profile in record.profiles:
                previous = profile_owner.get(profile)
                if previous is not None and previous != record.slug:
                    raise BindingsCorrupt(
                        f"profile {profile!r} belongs to both {previous!r} and {record.slug!r}")
                profile_owner[profile] = record.slug
        # A person's own slug may not also be somebody's raw subject stamp.
        for record in self.persons.values():
            if record.slug in subject_owner and subject_owner[record.slug] != record.slug:
                raise BindingsCorrupt(f"record slug {record.slug!r} is claimed as a subject by "
                                      f"{subject_owner[record.slug]!r}")
        return self

    # ── edits (each returns a NEW Bindings) ─────────────────────────────────

    def _copy(self, *, written_by: Optional[str] = None) -> Bindings:
        return replace(
            self, persons=dict(self.persons), written_at=_now(),
            written_by=self.written_by if written_by is None else written_by,
        )

    def with_record(self, record: PersonRecord, *, written_by: str = "") -> Bindings:
        """Insert or replace one record, validated. Used by bind/unbind/set_shared."""
        persons = dict(self.persons)
        previous = persons.get(record.slug)
        stamp = record.updated_at or _now()
        persons[record.slug] = replace(
            record,
            created_at=record.created_at or (previous.created_at if previous else stamp),
            updated_at=stamp,
        )
        updated = replace(
            self, persons=persons, written_at=_now(),
            written_by=written_by or self.written_by or record.owner or record.slug,
        )
        return updated.validate()


def same_person(first: Any, second: Any, *, bindings: Optional[Bindings] = None) -> bool:
    """Whether two subjects name the same person according to the registry.

    The single comparison step 3-6 call: no owner rule, no legacy stamp rules, no host-owner
    widening — only "both resolve to one record". Unknown subjects are never equal (fail closed),
    and a registry error propagates so the caller can deny rather than guess.
    """
    a = str(first or "").strip()
    b = str(second or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    registry = bindings if bindings is not None else load_cached()
    if registry is None:
        return False
    slug_a = registry.person_of(a)
    slug_b = registry.person_of(b)
    return bool(slug_a and slug_a == slug_b)


# ────────────────────────────────────────────────────────────────────────────
# Store: read, validate, write atomically, back up
# ────────────────────────────────────────────────────────────────────────────


class BindingStore:
    """The file behind :class:`Bindings`.

    * :meth:`read` is mtime-cached and raises :class:`BindingsCorrupt` for a present-but-broken
      file and :class:`BindingsUnavailable` for an unreadable one. An ABSENT file is ``None``.
    * :meth:`write` validates first, copies the previous version into ``backups/`` (a change to the
      access model is the one thing that must be recoverable: Л5/Д5 calls this registry the single
      point of failure), then replaces the file atomically — a reader sees the old document or the
      new one, never a truncated one.
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path is not None else bindings_path()
        self._cache: Optional[Bindings] = None
        self._cache_stamp: Optional[tuple[int, int]] = None

    # ── reading ─────────────────────────────────────────────────────────────

    def exists(self) -> bool:
        try:
            return self.path.is_file()
        except OSError:
            return False

    def _stamp(self) -> Optional[tuple[int, int]]:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BindingsUnavailable(f"{self.path}: {exc}") from exc
        return (int(info.st_mtime_ns), int(info.st_size))

    def read(self, *, use_cache: bool = True) -> Optional[Bindings]:
        """The registry, ``None`` when the file does not exist. Raises on a broken/unreadable one."""
        stamp = self._stamp()
        if stamp is None:
            self._cache, self._cache_stamp = None, None
            return None
        if use_cache and self._cache is not None and self._cache_stamp == stamp:
            return self._cache
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BindingsUnavailable(f"{self.path}: {exc}") from exc
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise BindingsCorrupt(f"{self.path}: invalid JSON ({exc})") from exc
        bindings = Bindings.from_json(raw, path=self.path).validate()
        self._cache, self._cache_stamp = bindings, stamp
        self._permission_warning()
        return bindings

    def _permission_warning(self) -> None:
        """A group/world-readable registry names who owns what — report it (the mode is not a refusal
        here, unlike the users file: the content is an access model, not credential material)."""
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except OSError:
            return
        if mode & 0o077:
            logger.warning("%s is readable beyond its owner (mode %o); the bindings registry should be %o",
                           self.path, mode, FILE_MODE)

    def load(self) -> Bindings:
        """The registry, empty when absent. Raises for a broken one (callers must deny)."""
        found = self.read()
        if found is None:
            return Bindings(path=self.path)
        return found

    def invalidate(self) -> None:
        self._cache, self._cache_stamp = None, None

    # ── writing ─────────────────────────────────────────────────────────────

    def backup(self, bindings: Optional[Bindings] = None) -> Optional[Path]:
        """Copy the CURRENT file into ``backups/`` before it is replaced; return the copy's path.

        Two names per change: a timestamped one (rotation keeps :data:`BACKUP_KEEP`) and
        ``profile-bindings-latest.json``, which is the one an operator restores from. ``None`` when
        there was no previous file to copy.
        """
        if not self.exists():
            return None
        target_dir = backups_dir(self.path)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise BindingsUnavailable(f"{self.path}: cannot back up ({exc})") from exc
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        stamped = target_dir / f"{BACKUP_PREFIX}{stamp}.json"
        latest = target_dir / BACKUP_LATEST
        from utils import atomic_write_bytes
        for destination in (stamped, latest):
            atomic_write_bytes(destination, payload, mode=FILE_MODE)
        self._prune_backups(target_dir)
        return stamped

    @staticmethod
    def _prune_backups(target_dir: Path) -> None:
        try:
            stamps = sorted(p for p in target_dir.glob(f"{BACKUP_PREFIX}*.json")
                            if p.name != BACKUP_LATEST)
        except OSError:
            return
        for stale in stamps[:-BACKUP_KEEP]:
            try:
                stale.unlink()
            except OSError:
                logger.debug("could not prune backup %s", stale, exc_info=True)

    def write(self, bindings: Bindings, *, backup: bool = True, written_by: str = "") -> Bindings:
        """Validate, back up the previous version, then replace the file atomically."""
        prepared = replace(bindings, written_at=bindings.written_at or _now(),
                           written_by=written_by or bindings.written_by,
                           path=self.path).validate()
        if backup:
            self.backup(prepared)
        from utils import atomic_write_text
        atomic_write_text(self.path, prepared.render(), mode=FILE_MODE, fsync_dir=True)
        self.invalidate()
        return prepared

    # ── operations ──────────────────────────────────────────────────────────

    def _current(self) -> Bindings:
        return self.load()

    def bind(self, slug: str, *, profiles: Iterable[str] = (), subjects: Iterable[str] = (),
             display_name: str = "", owner: str = "", source: str = "admin",
             written_by: str = "", replace_subjects: bool = True) -> Bindings:
        """Create or update a record. Re-binding MOVES a subject: it is dropped from every other
        record first, so one stamp never names two people (the ``validate`` invariant)."""
        slug = str(slug or "").strip()
        shared = bool(_SHARED_RE.match(slug))
        if not (_PERSON_RE.match(slug) or _SHARED_RE.match(slug)):
            raise BindingsConflict(f"{slug!r} is neither 'person/<login>' nor 'shared/<name>'")
        current = self._current()
        new_subjects = _str_tuple(list(subjects), where=f"{slug}: subjects")
        wanted = set(new_subjects)
        persons: dict[str, PersonRecord] = {}
        for existing in current.persons.values():
            if existing.slug == slug:
                continue
            kept = tuple(s for s in existing.subjects if s not in wanted)
            persons[existing.slug] = replace(existing, subjects=kept)
        previous = current.persons.get(slug)
        inherited_subjects = () if replace_subjects or previous is None else previous.subjects
        merged_subjects = tuple(dict.fromkeys((*inherited_subjects, *new_subjects)))
        stamp = _now()
        record = PersonRecord(
            slug=slug,
            display_name=display_name or (previous.display_name if previous else slug),
            profiles=tuple(dict.fromkeys((*(previous.profiles if previous else ()), *profiles))),
            subjects=merged_subjects,
            shared=shared,
            owner=owner or (previous.owner if previous else slug),
            source=source or (previous.source if previous else "admin"),
            created_at=previous.created_at if previous and previous.created_at else stamp,
            updated_at=stamp,
        )
        persons[slug] = record
        return self.write(replace(current, persons=persons), written_by=written_by or owner or slug)

    def unbind(self, slug: str, *, written_by: str = "") -> Bindings:
        """Remove a record entirely (the cascade a deleted account needs)."""
        current = self._current()
        if slug not in current.persons:
            return current
        persons = {k: v for k, v in current.persons.items() if k != slug}
        return self.write(replace(current, persons=persons), written_by=written_by or slug)

    def set_shared(self, profiles: Iterable[str], *, slug: str = "shared/service",
                   display_name: str = "Shared profiles", rewritten_by: str = "",
                   current: Optional[Bindings] = None) -> Bindings:
        """Replace the shared (public) profile set — the ``anyone`` grant, one record.

        ``current`` lets a caller that just wrote the document continue from the version it wrote,
        so one operation never re-reads a file it is about to overwrite.
        """
        registry = current if current is not None else self._current()
        wanted = _str_tuple(list(profiles), where=f"{slug}: profiles")
        pattern = _profile_re()
        for profile in wanted:
            if not pattern.match(profile):
                raise BindingsConflict(f"invalid profile name {profile!r}")
        record = PersonRecord(
            slug=slug, display_name=display_name, profiles=wanted, subjects=(), shared=True,
            owner=rewritten_by or "admin", source="admin",
            created_at=registry.persons[slug].created_at if slug in registry.persons else "",
            updated_at=_now(),
        )
        return self.write(registry.with_record(record, written_by=rewritten_by or "admin"))

    # ── bootstrap / migration ───────────────────────────────────────────────

    def bootstrap_owner(self, *, slug: str, profiles: Iterable[str], subjects: Iterable[str],
                        display_name: str = "", shared_profiles: Iterable[str] = (),
                        source: str = "bootstrap-owner", written_by: str = "") -> Bindings:
        """The first binding: the host owner's subject(s) → their own profiles.

        Idempotent in the strict sense: re-running with arguments that describe the CURRENT
        document does not rewrite it (no backup churn, no ``updated_at`` drift), so a migration can
        be re-run as a repair. A wider subject set — the normal migration case — extends the same
        person instead of creating a second one.

        Existing rows keep matching because the owner's legacy stamps (``uid:<n>``,
        ``cron-owner``, the bare Telegram id) are recorded as subjects of the same person.
        """
        current = self._current()
        wanted_profiles = tuple(dict.fromkeys(_str_tuple(list(profiles), where="profiles")))
        wanted_subjects = tuple(dict.fromkeys(_str_tuple(list(subjects), where="subjects")))
        wanted_shared = _str_tuple(list(shared_profiles), where="shared_profiles")
        if slug in current.persons and not current.persons[slug].subjects and not wanted_subjects:
            raise BindingsConflict(f"{slug}: refusing to write an owner record with no subjects")
        previous = current.persons.get(slug)
        if previous is not None and not previous.shared:
            same_record = (
                set(wanted_profiles) <= set(previous.profiles)
                and set(wanted_subjects) <= set(previous.subjects)
                and (not display_name or display_name == previous.display_name)
            )
            same_shared = set(wanted_shared) <= set(current.shared_profiles())
            if same_record and same_shared:
                logger.debug("bindings: %s already describes this document; nothing to write", slug)
                return current
        bound = self.bind(
            slug, profiles=wanted_profiles, subjects=wanted_subjects, display_name=display_name,
            owner=slug, source=source, written_by=written_by or slug,
        )
        if wanted_shared:
            bound = self.set_shared(wanted_shared, rewritten_by=written_by or slug, current=bound)
        return bound


# ────────────────────────────────────────────────────────────────────────────
# Process-wide accessor for the hot paths (same_owner, scope resolution)
# ────────────────────────────────────────────────────────────────────────────

_stores: dict[str, BindingStore] = {}


def store(path: Optional[str | Path] = None) -> BindingStore:
    """The process-wide store for *path* (default registry), so the mtime cache is shared."""
    key = str(Path(path) if path is not None else bindings_path())
    cached = _stores.get(key)
    if cached is None:
        cached = _stores[key] = BindingStore(key)
    return cached


def reset_cache() -> None:
    """Drop every cached registry (tests, and a caller that rewrote the file behind our back)."""
    for cached in _stores.values():
        cached.invalidate()


def registry_active() -> bool:
    """Whether bindings participate at all: only under ``acl.enabled`` (step 3 owns the switch)."""
    try:
        from hermes_acl_scope import acl_enabled
        return bool(acl_enabled())
    except Exception:  # an import failure must never ENABLE the registry
        logger.debug("bindings: acl switch unreadable", exc_info=True)
        return False


def load_cached() -> Optional[Bindings]:
    """The registry for the hot paths: ``None`` when absent OR inactive, raises when broken.

    Raises only while ``acl.enabled`` — with ACL off a broken file changes no behaviour, so it is
    logged once and ignored rather than turning today's single-user install into a denial.
    """
    registry_store = store()
    try:
        return registry_store.read()
    except BindingsError as exc:
        if registry_active():
            raise
        logger.debug("bindings: registry present but unusable while acl is off (%s)", exc)
        return None


def person_stamps(subject: Any) -> tuple[str, ...]:
    """Every stamp of *subject*'s person, for owner-scope expansion. ``()`` when unknown/inactive.

    Propagates :class:`BindingsError` while ACL is on, so the scope resolver can deny: a broken
    registry must stop the rights check, not silently fall back to a narrower (or wider) answer.
    """
    registry = load_cached()
    if registry is None:
        return ()
    return registry.subjects_of(subject)


def prime(*, quiet: bool = False) -> Optional[Bindings]:
    """Gateway boot: read and validate the registry once, loudly, before any request is served.

    Never raises: a broken registry is REPORTED (and every later check denies, so users are not
    silently let through) rather than preventing the process from starting — the gateway still has
    to serve the owner's own unscoped surfaces, and an operator needs a running process to fix the
    file. Returns the registry, or ``None`` when absent.
    """
    try:
        registry = store().read(use_cache=False)
    except BindingsError as exc:
        logger.error("ACL bindings registry unusable — every owner-scoped check will DENY until it "
                     "is repaired (restore %s): %s", backups_dir() / BACKUP_LATEST, exc)
        return None
    if registry is None:
        if not quiet:
            logger.info("ACL bindings registry not present yet (%s): owner stamps stay as they are",
                        bindings_path())
        return None
    if not quiet:
        logger.info("ACL bindings registry loaded: %d record(s), %d shared profile(s) — %s",
                    len(registry.persons), len(registry.shared_profiles()), bindings_path())
    return registry


def describe(registry: Optional[Bindings]) -> str:
    """One-line audit form (who owns what), never a session transcript."""
    if registry is None:
        return "bindings: absent"
    parts = [f"{r.slug}={list(r.profiles)}" for r in sorted(registry.persons.values(), key=lambda r: r.slug)]
    return "bindings: " + "; ".join(parts)


def copy_registry(source: Path, destination: Path) -> Path:
    """Restore helper: copy a backup over the registry, atomically and at 0600."""
    from utils import atomic_write_bytes
    atomic_write_bytes(destination, Path(source).read_bytes(), mode=FILE_MODE, fsync_dir=True)
    reset_cache()
    return destination


def backup_path_latest(path: Optional[Path] = None) -> Path:
    """The stable restore point an operator copies back after a bad edit."""
    return backups_dir(path) / BACKUP_LATEST
