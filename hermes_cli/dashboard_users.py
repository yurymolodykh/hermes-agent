"""The dashboard's users file: many logins, one additional source of truth.

``dashboard.basic_auth.users_file`` names a file of user records. It sits BESIDE the single
``username`` / ``password_hash`` pair, never instead of it: an installation with no users file
behaves exactly as before, and a file that is missing from disk changes nothing either.

Format — line-delimited JSON (one object per line), or a YAML/JSON list. Fields per record::

    login          required, unique (case-insensitive); what the person types at login
    display_name   shown in the UI; defaults to ``login``
    password_hash  required; ``scrypt$n$r$p$salt$dk`` is ours, other formats are read-only
    role           ``owner`` | ``user`` (default ``user``)
    aliases        list of extra logins that resolve to this record (old logins after a merge)
    telegram_id    platform id, empty when the person has no binding
    disabled       bool; a disabled person never authenticates
    gen            integer generation, bumped by every ``passwd``/``disable``/``enable``
    created_at     free-form ISO timestamp
    email          optional; a second merge key during import (see ``merge_import``)

Fail-closed is the rule for this file, and it applies to *all* of it: a record that does not
parse or does not validate voids the WHOLE file (nobody authenticates from it) and the error
names the line and the field — a half-loaded user table is worse than none, because the missing
half looks like "that person's password is wrong".

Two integrity rules are enforced on every load, in this order, and both are loud:

* permissions — a file that any account other than its owner can read is REFUSED
  (:class:`UsersFilePermissionError`). Credential material does not get a warning-only check.
* owner — a file owned by another uid is reported as a problem (the operator must fix it), but
  only the mode is a hard refusal: "owned by hermes" is an installation fact this module cannot
  assert on a host where the agent runs as somebody else.

Nothing here imports the dashboard-auth plugin: the plugin and ``hermes auth users`` both use
this module, so there is exactly one reader, one writer and one set of validation rules.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from utils import atomic_write_text

#: Only the owner may hold password material.
USERS_FILE_MODE = 0o600

#: Fields a record may carry. Anything else is preserved verbatim on rewrite but never read.
KNOWN_FIELDS = (
    "login", "display_name", "password_hash", "role", "aliases", "telegram_id",
    "disabled", "gen", "created_at", "email",
)

ROLES = ("owner", "user")

#: Hash prefixes we can VERIFY. ``scrypt`` is the only one we WRITE.
HASH_SCRYPT = "scrypt"
HASH_BCRYPT = "bcrypt"
HASH_ARGON2 = "argon2"
HASH_UNKNOWN = "unknown"

_BCRYPT_PREFIXES = ("$2b$", "$2a$", "$2y$")
_ARGON2_PREFIX = "$argon2"

_LOGIN_RE = re.compile(r"^[^\s|/\\]+$")


class UsersFileError(Exception):
    """Base for every users-file refusal. A caller must not continue past one of these."""


class UsersFilePermissionError(UsersFileError):
    """The file is readable by group or others: refused, never merely warned about."""


class UsersFileNotFound(UsersFileError):
    """The configured file does not exist."""


@dataclass(frozen=True)
class UserRecord:
    """One person. Frozen: a change produces a new record and an atomic rewrite."""

    login: str
    password_hash: str
    display_name: str = ""
    role: str = "user"
    aliases: tuple[str, ...] = ()
    telegram_id: str = ""
    disabled: bool = False
    gen: int = 1
    created_at: str = ""
    email: str = ""

    @property
    def display(self) -> str:
        return self.display_name or self.login

    def all_logins(self) -> tuple[str, ...]:
        return (self.login, *self.aliases)

    def to_json(self) -> dict[str, Any]:
        """The on-disk shape, keys in a stable order so diffs stay readable."""
        data: dict[str, Any] = {
            "login": self.login,
            "display_name": self.display,
            "password_hash": self.password_hash,
            "role": self.role,
            "aliases": list(self.aliases),
            "telegram_id": self.telegram_id,
            "disabled": self.disabled,
            "gen": self.gen,
            "created_at": self.created_at,
        }
        if self.email:
            data["email"] = self.email
        return data

    def to_line(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, separators=(",", ":"))


@dataclass
class UsersProblem:
    """One reason the file cannot be used, naming where it was found."""

    line: int
    field: str
    message: str

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line > 0 else "file"
        return f"{where}, field {self.field!r}: {self.message}"


@dataclass
class UsersLoadResult:
    """What a load produced. ``users`` is empty whenever ``problems`` is non-empty (fail-closed)."""

    path: Optional[Path]
    users: list[UserRecord] = field(default_factory=list)
    problems: list[UsersProblem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    exists: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems

    def by_login(self, login: str) -> Optional[UserRecord]:
        """The record answering to *login* or one of its aliases; None when the file is unusable."""
        if not self.ok:
            return None
        return lookup(self.users, login)

    def summary_line(self) -> str:
        if not self.exists:
            return "users file: not configured or absent"
        if self.problems:
            return f"users file: REFUSED — {len(self.problems)} problem(s); nobody may sign in from it"
        return f"users file: {len(self.users)} record(s)"


# ---- hash formats ---------------------------------------------------------

def hash_kind(encoded: str) -> str:
    """Which scheme a stored hash claims to be. Unrecognised text is ``unknown``, never a guess."""
    text = (encoded or "").strip()
    if text.startswith("scrypt$"):
        return HASH_SCRYPT
    if text.startswith(_BCRYPT_PREFIXES):
        return HASH_BCRYPT
    if text.startswith(_ARGON2_PREFIX):
        return HASH_ARGON2
    return HASH_UNKNOWN


# Interactive-login scrypt parameters (~16 MiB, a few ms); n must be a power of two.
# These are the SAME values the dashboard-auth basic plugin has always used, so hashes written
# by either the plugin or this module verify under both.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$<salt_b64>$<dk_b64>`` — the only format this project writes."""
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN, maxmem=0)
    return (f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
            f"${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}")


def verify_scrypt(password: str, encoded: str) -> bool:
    """Constant-time scrypt verify. False on any malformed hash string."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                dklen=len(expected), maxmem=0)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


class UnsupportedHashFormat(Exception):
    """A stored hash is in a scheme this environment cannot verify (module not installed)."""


def _bcrypt_backend() -> Any:
    try:
        import bcrypt  # optional dependency, imported on demand
    except ImportError as exc:
        raise UnsupportedHashFormat(
            "bcrypt is not installed in this Python environment, so a stored $2b$ hash cannot "
            "be verified. Install it (pip install bcrypt) or re-hash the password with "
            "`hermes auth users passwd`.") from exc
    return bcrypt


def _argon2_backend() -> Any:
    try:
        from argon2 import PasswordHasher  # optional dependency, imported on demand
        from argon2.exceptions import VerificationError, VerifyMismatchError
    except ImportError as exc:
        raise UnsupportedHashFormat(
            "argon2-cffi is not installed in this Python environment, so a stored $argon2 hash "
            "cannot be verified. Install it (pip install argon2-cffi) or re-hash the password "
            "with `hermes auth users passwd`.") from exc
    return SimpleNamespace(PasswordHasher=PasswordHasher,
                           errors=(VerificationError, VerifyMismatchError))


def verify_password(password: str, encoded: str) -> bool:
    """Verify against a stored hash in ANY supported scheme; False when it simply does not match.

    scrypt is ours. ``$2b$``/``$2a$``/``$2y$`` (bcrypt) and ``$argon2*`` (argon2) are READ-ONLY
    migrations: they are accepted so an imported password keeps working, and the caller rewrites
    them in scrypt on success (:func:`needs_rehash`).

    A scheme whose module is missing raises :class:`UnsupportedHashFormat` instead of returning
    ``False``: "we cannot check this" must not be reported as "wrong password".
    """
    kind = hash_kind(encoded)
    if kind == HASH_SCRYPT:
        return verify_scrypt(password, encoded)
    if kind == HASH_BCRYPT:
        bcrypt = _bcrypt_backend()
        try:
            return bool(bcrypt.checkpw(password.encode("utf-8"), encoded.encode("utf-8")))
        except (ValueError, TypeError):
            return False
    if kind == HASH_ARGON2:
        backend = _argon2_backend()
        try:
            return bool(backend.PasswordHasher().verify(encoded, password))
        except backend.errors:
            return False
        except Exception:  # malformed hash text: a mismatch, not a crash
            return False
    return False


def needs_rehash(encoded: str) -> bool:
    """True when a stored hash should be rewritten in our scrypt form after a successful login."""
    return hash_kind(encoded) != HASH_SCRYPT


def dummy_scrypt_hash() -> str:
    """A throwaway hash used to keep the no-such-user path as slow as the real one."""
    return hash_password("dummy-password-for-constant-time-verify")


def _normalize_login(login: str) -> str:
    return (login or "").strip().lower()


def lookup(users: Iterable[UserRecord], login: str) -> Optional[UserRecord]:
    """The record for *login* (exact on the canonical login, then on an alias).

    Case-insensitive and exact-only: a near-miss login is NOT a match, because "close enough"
    is how two different people end up sharing an account.
    """
    wanted = _normalize_login(login)
    if not wanted:
        return None
    for record in users:
        if _normalize_login(record.login) == wanted:
            return record
    for record in users:
        if any(_normalize_login(alias) == wanted for alias in record.aliases):
            return record
    return None


# ---- permissions ----------------------------------------------------------

def check_permissions(path: Path) -> list[str]:
    """Loud refusals/warnings for *path*. Raises :class:`UsersFilePermissionError` on loose bits.

    Group/other bits are a hard refusal on POSIX. On Windows ``os.stat`` reports synthetic
    ``0o666``/``0o777`` modes for every file, so the check is skipped there and the ACL is the
    operator's (the migration runs on Windows; refusing every file there would make the feature
    unusable rather than safe).
    """
    warnings: list[str] = []
    if os.name == "nt":
        return warnings
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise UsersFilePermissionError(
            f"users file {path} has mode {mode:04o}: it is readable by group or others. "
            f"Password hashes must stay private — run `chmod 600 {path}` "
            "and restart. Refusing to load any user from this file until then."
        )
    me = os.getuid()
    if st.st_uid != me:
        warnings.append(
            f"users file {path} is owned by uid {st.st_uid}, not by this process (uid {me}); "
            "fix the ownership so the file stays private."
        )
    return warnings


# ---- parsing --------------------------------------------------------------

def _parse_json_line(text: str) -> Any:
    return json.loads(text)


def _iter_raw_records(text: str) -> list[tuple[int, Any]]:
    """(line number, object) pairs from a line-delimited JSON file or a YAML/JSON list.

    Line numbers are what an operator can act on, so line-delimited JSON is tried first: if every
    non-empty line parses as a JSON value, that IS the format. Only otherwise is the text treated
    as one YAML/JSON document (where the reported number is the item's index, not a line).
    """
    stripped = text.strip()
    if not stripped:
        return []

    line_records: list[tuple[int, Any]] = []
    line_delimited = True
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            line_records.append((lineno, json.loads(line)))
        except ValueError:
            line_delimited = False
            break
    if line_delimited:
        return line_records

    from utils import fast_safe_load

    data = fast_safe_load(text)
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("users")
    if not isinstance(data, list):
        raise ValueError("expected a list of user records")
    return [(idx, item) for idx, item in enumerate(data, start=1)]


def _validate(raw: Any, lineno: int) -> tuple[Optional[UserRecord], list[UsersProblem]]:
    """Validate one parsed object into a record, naming the field that failed."""
    problems: list[UsersProblem] = []

    def bad(fieldname: str, message: str) -> None:
        problems.append(UsersProblem(lineno, fieldname, message))

    if not isinstance(raw, dict):
        bad("<record>", f"expected a JSON object, got {type(raw).__name__}")
        return None, problems

    login = str(raw.get("login") or "").strip()
    if not login:
        bad("login", "is empty; a record without a login can never be signed in to")
    elif not _LOGIN_RE.match(login):
        bad("login", "contains whitespace, '|' or a path separator")

    password_hash = str(raw.get("password_hash") or "").strip()
    if not password_hash:
        bad("password_hash", "is empty; a record without a hash can never be signed in to")

    role = str(raw.get("role") or "user").strip().lower()
    if role not in ROLES:
        bad("role", f"must be one of {', '.join(ROLES)}, got {role!r}")

    aliases_raw = raw.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases_raw = [aliases_raw]
    if not isinstance(aliases_raw, list):
        bad("aliases", "must be a list of logins")
        aliases_raw = []
    aliases: list[str] = []
    for alias in aliases_raw:
        text = str(alias or "").strip()
        if not text:
            bad("aliases", "contains an empty alias")
            continue
        aliases.append(text)

    disabled_raw = raw.get("disabled", False)
    if not isinstance(disabled_raw, bool):
        bad("disabled", f"must be true or false, got {disabled_raw!r}")
        disabled_raw = False

    gen_raw = raw.get("gen", 1)
    try:
        gen = int(gen_raw)
    except (TypeError, ValueError):
        bad("gen", f"must be an integer, got {gen_raw!r}")
        gen = 1
    else:
        if gen < 0:
            bad("gen", f"must not be negative, got {gen}")

    if problems:
        return None, problems
    return (
        UserRecord(
            login=login,
            password_hash=password_hash,
            display_name=str(raw.get("display_name") or "").strip(),
            role=role,
            aliases=tuple(aliases),
            telegram_id=str(raw.get("telegram_id") or "").strip(),
            disabled=disabled_raw,
            gen=gen,
            created_at=str(raw.get("created_at") or "").strip(),
            email=str(raw.get("email") or "").strip(),
        ),
        [],
    )


def parse_users_text(text: str) -> tuple[list[UserRecord], list[UsersProblem], list[str]]:
    """Parse file text into records. Any problem voids the whole file (fail-closed)."""
    problems: list[UsersProblem] = []
    warnings: list[str] = []
    try:
        raw_records = _iter_raw_records(text)
    except Exception as exc:  # malformed YAML/JSON: name the file, not a stack trace
        return [], [UsersProblem(0, "<file>", f"cannot be parsed: {exc}")], warnings

    records: list[UserRecord] = []
    for lineno, raw in raw_records:
        record, record_problems = _validate(raw, lineno)
        problems.extend(record_problems)
        if record is not None:
            records.append(record)

    if not problems:
        # Duplicate logins/aliases are a data error too: two people must not share a name.
        seen: dict[str, str] = {}
        for record in records:
            for name in record.all_logins():
                key = _normalize_login(name)
                if key in seen:
                    problems.append(UsersProblem(
                        0, "login/aliases",
                        f"{name!r} is claimed by both {seen[key]!r} and {record.login!r}"))
                else:
                    seen[key] = record.login

    if problems:
        return [], problems, warnings

    for record in records:
        kind = hash_kind(record.password_hash)
        if kind == HASH_UNKNOWN:
            warnings.append(
                f"login {record.login!r}: unrecognised password_hash format "
                f"(starts with {record.password_hash[:4]!r}); this person cannot sign in "
                "until the hash is replaced or rewritten in scrypt$ form")
        elif kind != HASH_SCRYPT:
            warnings.append(
                f"login {record.login!r}: {kind} password hash — verified on login and "
                "re-hashed to scrypt on first success")
    return records, [], warnings


# ---- loading / writing ----------------------------------------------------

def resolve_users_file_path(value: Any) -> Optional[Path]:
    """Absolute path for the configured value; ``None`` when unset.

    A relative path resolves under HERMES_HOME so a config copied between hosts keeps pointing at
    the profile's own file instead of the current working directory.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / path
    return path


def load_users_file(path: Optional[Path]) -> UsersLoadResult:
    """Read and validate *path*.

    Raises :class:`UsersFilePermissionError` when the file is group/world-readable — that is a
    refusal, not a warning, and callers must not fall back to "pretend it is not there".
    A missing file is a normal state (single-pair installs) and yields an empty result.
    """
    result = UsersLoadResult(path=path)
    if path is None:
        return result
    if not path.exists():
        return result
    result.exists = True
    result.warnings.extend(check_permissions(path))
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        result.problems.append(UsersProblem(0, "<file>", f"cannot be read: {exc}"))
        return result
    records, problems, warnings = parse_users_text(text)
    result.problems.extend(problems)
    result.warnings.extend(warnings)
    if not problems:
        result.users = records
    return result


class UserStore:
    """Cached view of the users file; reloads when it changes on disk.

    The provider holds one of these so ``hermes auth users passwd`` (a different process) takes
    effect without a restart, and a re-hash written during a login is visible immediately.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        self._signature: Optional[tuple[int, int]] = None
        self._result = UsersLoadResult(path=path)

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def _signature_now(self) -> Optional[tuple[int, int]]:
        if self._path is None or not self._path.exists():
            return None
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self, *, force: bool = False) -> UsersLoadResult:
        signature = self._signature_now()
        if force or signature != self._signature:
            self._result = load_users_file(self._path)
            self._signature = signature
        return self._result

    def lookup(self, login: str) -> Optional[UserRecord]:
        return self.load().by_login(login)

    def get(self, login: str) -> Optional[UserRecord]:
        """Exact lookup on the canonical login only (used by the management commands)."""
        normalized = _normalize_login(login)
        for record in self.load().users:
            if _normalize_login(record.login) == normalized:
                return record
        return None

    # ---- writes ----------------------------------------------------------

    def _read_lines(self) -> list[str]:
        if self._path is None or not self._path.exists():
            return []
        text = self._path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                json.loads(line)
            except ValueError:
                raise UsersFileError(
                    f"users file {self._path} is not line-delimited JSON (its first record does "
                    "not parse as one object per line); management commands write that form. "
                    "Convert the file once to one JSON object per line.") from None
        return lines

    def write_records(self, records: list[UserRecord]) -> None:
        """Replace the file with *records*, atomically and with permissions preserved."""
        if self._path is None:
            raise UsersFileError("no users file is configured (dashboard.basic_auth.users_file)")
        body = "".join(record.to_line() + "\n" for record in records)
        self._write_text(body)

    def _write_text(self, body: str) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path, body, mode=USERS_FILE_MODE, preserve_mode=True, fsync_dir=True)
        try:
            os.chmod(self._path, USERS_FILE_MODE)
        except OSError:  # pragma: no cover - exotic filesystems (FAT, some network mounts)
            pass
        self._signature = None
        self.load(force=True)

    def replace_record(self, record: UserRecord) -> bool:
        """Swap the line for *record.login* in place, keeping every other line byte-identical.

        Re-reads first, so a concurrent edit to another record is not clobbered. Returns False
        when the login is no longer in the file.
        """
        lines = self._read_lines()
        normalized = _normalize_login(record.login)
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if isinstance(raw, dict) and _normalize_login(str(raw.get("login") or "")) == normalized:
                lines[idx] = record.to_line()
                self._write_text("".join(existing + "\n" for existing in lines))
                return True
        return False

    def upsert_record(self, record: UserRecord) -> None:
        """Add *record* or replace the existing line with the same login."""
        if not self.replace_record(record):
            lines = self._read_lines()
            lines.append(record.to_line())
            self._write_text("".join(existing + "\n" for existing in lines))

    def next_gen(self, record: UserRecord) -> UserRecord:
        return replace(record, gen=max(0, int(record.gen)) + 1)


# ---- import / merge -------------------------------------------------------

@dataclass
class ImportReport:
    """The import summary: counts plus the logins behind each count. Never hash material."""

    added: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # (login, reason)
    unknown_hash: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"added: {len(self.added)}",
            f"merged: {len(self.merged)}",
            f"skipped: {len(self.skipped)}",
            f"conflicts: {len(self.conflicts)}",
            f"unknown hash format: {len(self.unknown_hash)}",
        ]
        if self.merged:
            lines.append("merged logins: " + ", ".join(self.merged))
        if self.skipped:
            lines.append("skipped logins: " + ", ".join(self.skipped))
        if self.conflicts:
            lines.append("conflict logins: " + ", ".join(f"{login} ({why})" for login, why in self.conflicts))
        if self.unknown_hash:
            lines.append("unknown hash format: " + ", ".join(self.unknown_hash))
        return "\n".join(lines)


def _looks_like_header(parts: list[str]) -> bool:
    """True when *parts* names the export's own columns, e.g. ``login|display_name|password_hash``."""
    first = parts[0].strip().lower()
    rest = [part.strip().lower().replace(" ", "_") for part in parts[1:]]
    if first != "login":
        return False
    known = {"display_name", "password_hash", "telegram_id", "role", "created_at", "email"}
    return bool(rest) and rest[0] in known


def parse_export_text(text: str) -> tuple[list[dict[str, str]], list[UsersProblem]]:
    """Parse a Windows-host export: ``login|display_name|password_hash|telegram_id|role|created_at``.

    A header line is optional and detected by its first column. A seventh column (e-mail) and an
    eighth (aliases, comma-separated) are accepted so the merge keeps more identity than the bare
    six do.
    """
    columns = ["login", "display_name", "password_hash", "telegram_id", "role", "created_at",
               "email", "aliases"]
    rows: list[dict[str, str]] = []
    problems: list[UsersProblem] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if not rows and len(parts) > 1 and _looks_like_header(parts):
            # A header row: recognised by its column NAMES (and only as the first data row), so a
            # person whose login happens to be the word "login" is not silently dropped.
            continue
        if len(parts) < 3:
            problems.append(UsersProblem(lineno, "line", "expected at least login|display_name|password_hash"))
            continue
        row = dict(zip(columns, parts))
        if len(parts) > len(columns):
            row["extra"] = "|".join(parts[len(columns):])
        rows.append(row)
    return rows, problems


def _merge_keys(record: UserRecord) -> list[tuple[str, str]]:
    """Identity keys for merge, strongest first: telegram id, e-mail, then the login itself.

    Similar-looking logins are never a key — that is how ``yury`` and ``yuri`` become one person
    on paper only.
    """
    keys: list[tuple[str, str]] = []
    if record.telegram_id:
        keys.append(("telegram_id", record.telegram_id.strip()))
    for address in (record.email, record.login if "@" in record.login else ""):
        if address and "@" in address:
            keys.append(("email", address.strip().lower()))
    if record.login:
        keys.append(("login", _normalize_login(record.login)))
    return keys


def merge_import(
    existing: list[UserRecord],
    incoming: list[dict[str, str]],
    *,
    overwrite: bool = False,
) -> tuple[list[UserRecord], ImportReport]:
    """Merge *incoming* rows into *existing* records; returns the new list and a report.

    Rules:
    * a row matching an existing record by telegram id or e-mail is MERGED — its old login joins
      ``aliases`` so nobody is lost; the existing hash is kept unless ``overwrite`` is set;
    * a row whose login matches but whose telegram id / e-mail disagree with the existing record
      is a CONFLICT (left untouched, reported), because silently picking a side reassigns a person;
    * anything else is ADDED.
    """
    report = ImportReport()
    records = list(existing)

    for row in incoming:
        login = (row.get("login") or "").strip()
        if not login:
            report.conflicts.append(("<empty>", "login column is empty"))
            continue
        hash_value = (row.get("password_hash") or "").strip()
        kind = hash_kind(hash_value)
        if kind == HASH_UNKNOWN:
            report.unknown_hash.append(login)

        candidate = UserRecord(
            login=login,
            password_hash=hash_value,
            display_name=(row.get("display_name") or "").strip(),
            role=(row.get("role") or "user").strip().lower() or "user",
            aliases=tuple(
                part.strip() for part in (row.get("aliases") or "").split(",") if part.strip()),
            telegram_id=(row.get("telegram_id") or "").strip(),
            created_at=(row.get("created_at") or "").strip(),
            email=(row.get("email") or "").strip(),
        )

        match_idx: Optional[int] = None
        matched_by = ""
        for idx, record in enumerate(records):
            existing_keys = {key: value for key, value in _merge_keys(record)}
            for key, value in _merge_keys(candidate):
                # Only identity-defining keys merge; a shared login is handled separately below.
                if key == "login" or key not in existing_keys:
                    continue
                if existing_keys[key] == value:
                    match_idx, matched_by = idx, key
                    break
            if match_idx is not None:
                break

        if match_idx is None:
            login_idx = next(
                (i for i, record in enumerate(records)
                 if _normalize_login(record.login) == _normalize_login(login)), None)
            if login_idx is not None:
                existing_record = records[login_idx]
                same_identity = (
                    (not candidate.telegram_id and not candidate.email)
                    or (candidate.telegram_id and candidate.telegram_id == existing_record.telegram_id)
                    or (candidate.email and _normalize_login(candidate.email) == _normalize_login(existing_record.email))
                )
                if not same_identity:
                    report.conflicts.append((login, "same login, different telegram id or e-mail"))
                    continue
                # Same person, re-exported: keep their hash unless overwrite was explicit.
                if not overwrite and existing_record.password_hash:
                    report.skipped.append(login)
                    continue
                match_idx, matched_by = login_idx, "login"

        if match_idx is None:
            records.append(candidate)
            report.added.append(login)
            continue

        current = records[match_idx]
        # Nobody is lost: the incoming login survives as an alias when it differs from the kept
        # canonical one, and so does every login the existing record already answered to.
        alias_pool = [*current.aliases, *candidate.aliases]
        for name in (candidate.login, *current.all_logins()):
            if _normalize_login(name) != _normalize_login(current.login):
                alias_pool.append(name)
        aliases = tuple(dict.fromkeys(alias_pool))
        merged = replace(
            current,
            # Only take the incoming display name when it actually carries one: filling an empty
            # field from the same string is not a merge, and it would make a no-op look like work.
            display_name=current.display_name or candidate.display_name,
            password_hash=(candidate.password_hash if overwrite else current.password_hash) or candidate.password_hash,
            role=candidate.role or current.role,
            telegram_id=current.telegram_id or candidate.telegram_id,
            email=current.email or candidate.email,
            created_at=current.created_at or candidate.created_at,
            aliases=aliases,
        )
        if merged == current:
            # Matched, but the export carried nothing new (no new alias, no field to fill in,
            # hash kept): that is "skipped", not "merged" — the summary must not claim work.
            report.skipped.append(login)
            continue
        records[match_idx] = merged
        report.merged.append(f"{candidate.login}→{current.login} ({matched_by})")

    return records, report


def render_list(records: list[UserRecord]) -> str:
    """A table for ``hermes auth users list`` — login, role, status, aliases, telegram. No hashes."""
    if not records:
        return "no users"
    rows = [("login", "role", "status", "aliases", "telegram")]
    for record in records:
        rows.append((
            record.login,
            record.role,
            "disabled" if record.disabled else "active",
            ", ".join(record.aliases) if record.aliases else "-",
            record.telegram_id or "-",
        ))
    widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)).rstrip()
        for row in rows)
