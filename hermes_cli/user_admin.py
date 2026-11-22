"""``hermes user add`` — one command that provisions a whole user, or nothing.

A person in this install is four artefacts that must agree: a profile ``01-<login>``
(with the personal SOUL clone and its questionnaire), a person record in the ACL
bindings registry, a dashboard users-file record (the password they sign in with),
and a ``gateway.profile_routes`` entry so their Telegram messages reach their
profile through the owner's single bot. Creating them one command at a time is how
installs end up with a profile nobody can sign into, or a route pointing at a
profile that was never created.

This module owns the all-or-nothing order: every completed step registers its own
undo, and any later failure unwinds them in reverse before returning a non-zero
code. A refusal (duplicate profile, duplicate binding, duplicate login) is decided
BEFORE the first write and exits ``2``.

Formats are taken from the code that owns them, never re-invented here:
``hermes_cli.profiles`` (profile create/delete), ``hermes_acl_bindings``
(``person/<login>`` records), ``hermes_cli.dashboard_users`` (line-JSON users file,
``scrypt$`` hashes), ``gateway.profile_routing`` (route fields, ``bot_profile: null``
= the default profile's bot).
"""

from __future__ import annotations

import json
import secrets
import stat
import string
import sys
from pathlib import Path
from typing import Any, Callable, Optional

PERSONAL_ROOT_PREFIX = "01-"
CLONE_SOURCE = "01-orchestrator"
ROUTE_PLATFORM = "telegram"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

PASSWORD_LENGTH = 24
PASSWORD_PREFIX_CHARS = 7
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_"

USERS_FILE_RELATIVE = "users.jsonl"
CREDENTIALS_DIRNAME = "credentials"


class Refused(Exception):
    """A precondition failed: nothing was written, exit code is 2."""


class Failed(Exception):
    """A step failed after writes started: everything is unwound, exit code is 1."""


# ────────────────────────────────────────────────────────────────────────────
# Small shared helpers
# ────────────────────────────────────────────────────────────────────────────


def _out(message: str = "", *, stream=None) -> None:
    print(message, file=stream or sys.stdout)


def _err(message: str) -> None:
    print(f"hermes user: {message}", file=sys.stderr)


def personal_profile_name(login: str) -> str:
    """``01-<login>`` for a login that may name a person, else ``""``.

    ``hermes_acl_soul.normalize_login`` is the one rule (``[A-Za-z0-9._-]``, no leading
    dot, <= 64 chars) and ``hermes_acl_identity.person_slug`` the one subject rule, so a
    login accepted here always has both a profile name and a registry subject.
    """
    from hermes_acl_identity import person_slug
    from hermes_acl_soul import normalize_login

    slug = normalize_login(login)
    if not slug or not person_slug(slug):
        return ""
    return f"{PERSONAL_ROOT_PREFIX}{slug}"


def telegram_subject(telegram_id: str) -> str:
    """The registry stamp for a Telegram user id (``tg:<id>``; same form as the tests)."""
    value = str(telegram_id or "").strip()
    return f"tg:{value}" if value else ""


def user_record_name(login: str) -> str:
    return str(login or "").strip()


# ────────────────────────────────────────────────────────────────────────────
# Users file (the password they sign in with)
# ────────────────────────────────────────────────────────────────────────────


def users_file_path() -> Optional[Path]:
    """The configured users file, or ``None`` when the install names none."""
    from hermes_cli import dashboard_users as du
    from hermes_cli.config import cfg_get, load_config

    value = cfg_get(load_config(), "dashboard", "basic_auth", "users_file", default="")
    return du.resolve_users_file_path(value)


def ensure_users_file_configured() -> tuple[Path, Callable[[], None]]:
    """The users file, configuring ``dashboard.basic_auth.users_file`` if it was unset.

    Returns the path and an undo that clears the key it just set (a no-op when the
    install already named a file). Without a configured path ``UserStore`` refuses to
    write at all, so a user created here would have no password to sign in with.
    """
    existing = users_file_path()
    if existing is not None:
        return existing, (lambda: None)
    from hermes_cli.config import get_config_path
    from hermes_cli.config_backend import write_config_keys

    write_config_keys(get_config_path(), {"dashboard.basic_auth.users_file": USERS_FILE_RELATIVE})

    def undo() -> None:
        write_config_keys(get_config_path(), {"dashboard.basic_auth.users_file": None})

    resolved = users_file_path()
    if resolved is None:  # pragma: no cover - the write above always resolves
        raise Failed("could not configure dashboard.basic_auth.users_file")
    return resolved, undo


# ────────────────────────────────────────────────────────────────────────────
# Routes (which profile a Telegram sender's messages reach)
# ────────────────────────────────────────────────────────────────────────────


def _routes_doc() -> tuple[Path, Any, list[dict]]:
    from hermes_cli.config import get_config_path
    from hermes_cli.config_backend import read_config_doc

    path = get_config_path()
    try:
        document = read_config_doc(path)
    except FileNotFoundError:
        document = {}
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise Refused(f"{path} is not a YAML mapping; refusing to edit it")
    gateway = document.get("gateway")
    routes = gateway.get("profile_routes") if isinstance(gateway, dict) else None
    return path, document, list(routes or [])


def write_route_undoable(login: str, profile: str, telegram_id: str) -> Callable[[], None]:
    """Append one ``gateway.profile_routes`` entry; return the undo that removes it.

    ``bot_profile`` is null on purpose: every person is served by the owner's single
    Telegram bot (one reader per token — a second one is ``telegram_polling_conflict``).
    """
    from hermes_cli.config_backend import write_config_document

    path, document, routes = _routes_doc()
    before = json.loads(json.dumps(document, default=str))

    route = {
        "name": f"user-{login}",
        "platform": ROUTE_PLATFORM,
        "profile": profile,
        "user_id": str(telegram_id),
        "bot_profile": None,
        "enabled": True,
    }
    routes.append(route)
    gateway = document.setdefault("gateway", {})
    if not isinstance(gateway, dict):  # pragma: no cover - refused in _routes_doc
        raise Refused(f"{path}: gateway is not a mapping")
    gateway["profile_routes"] = routes
    write_config_document(path, document)

    def undo() -> None:
        write_config_document(path, before)

    return undo


def _route_for_profile(login: str) -> Optional[dict]:
    from gateway.profile_routing import parse_profile_routes

    _path, _document, routes = _routes_doc()
    wanted = f"{PERSONAL_ROOT_PREFIX}{login}"
    for route in parse_profile_routes(routes):
        if route.profile == wanted:
            return {
                "name": route.name,
                "platform": route.platform,
                "profile": route.profile,
                "user_id": route.user_id,
                "bot_profile": route.bot_profile,
                "enabled": route.enabled,
            }
    return None


# ────────────────────────────────────────────────────────────────────────────
# Password delivery
# ────────────────────────────────────────────────────────────────────────────


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def write_password_file(login: str, password: str) -> tuple[Path, Callable[[], None]]:
    """Write the full password to ``<HERMES_HOME>/credentials/<login>.password`` (0600).

    Only the first ``PASSWORD_PREFIX_CHARS`` characters ever reach stdout: the owner
    needs them to recognise the record, not to read the secret out of a terminal log.
    """
    from hermes_constants import get_hermes_home

    directory = get_hermes_home() / CREDENTIALS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    path = directory / f"{login}.password"
    fd = path.open("w", encoding="utf-8")
    try:
        fd.write(password + "\n")
    finally:
        fd.close()
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    if stat.S_IMODE(path.stat().st_mode) != 0o600:  # pragma: no cover - defence in depth
        raise Failed(f"{path} is not owner-only after writing it")

    def undo() -> None:
        try:
            path.unlink()
        except OSError:
            pass

    return path, undo


# ────────────────────────────────────────────────────────────────────────────
# The command
# ────────────────────────────────────────────────────────────────────────────


def _preflight(login: str, telegram_id: str) -> str:
    """Everything that can refuse, before the first write. Returns the profile name."""
    from hermes_cli import dashboard_users as du
    from hermes_cli.profiles import get_profile_dir, profile_exists

    profile = personal_profile_name(login)
    if not profile:
        raise Refused(
            f"{login!r} cannot name a person: use letters, digits, '.', '-' or '_' "
            "(no leading dot, at most 64 characters)")
    if profile_exists(profile) or get_profile_dir(profile).exists():
        raise Refused(f"profile {profile} already exists")

    # Bindings: the slug and the Telegram stamp must both be free.
    from hermes_acl_bindings import store

    registry = store().read()
    subject = telegram_subject(telegram_id)
    if registry is not None:
        slug = f"person/{profile[len(PERSONAL_ROOT_PREFIX):]}"
        if registry.record(slug) is not None:
            raise Refused(f"binding record {slug} already exists")
        if registry.person_of(slug) is not None:
            raise Refused(f"{slug} is already claimed by {registry.person_of(slug)}")
        if subject and registry.person_of(subject) is not None:
            raise Refused(f"Telegram id {telegram_id} is already bound to "
                          f"{registry.person_of(subject)}")
        if registry.profile_owner(profile) is not None:
            raise Refused(f"profile {profile} is already listed in {registry.profile_owner(profile)}")

    # Users file: no record, and no route already aiming at this profile.
    path = users_file_path()
    if path is not None:
        result = du.load_users_file(path)
        if not result.ok:
            raise Refused(f"users file {path} is unusable: "
                          + "; ".join(str(problem) for problem in result.problems))
        if du.lookup(result.users, login) is not None:
            raise Refused(f"login {login!r} already exists in {path}")
    if _route_for_profile(login) is not None:
        raise Refused(f"gateway.profile_routes already routes Telegram traffic to {profile}")
    return profile


def add_user(
    login: str,
    *,
    telegram_id: str = "",
    display_name: str = "",
    role: str = "user",
    password: Optional[str] = None,
    no_skills: bool = False,
    say: Callable[[str], None] = _out,
) -> dict[str, Any]:
    """Provision one user completely. Raises :class:`Refused` or :class:`Failed`."""
    from hermes_cli.profiles import delete_profile

    login = user_record_name(login)
    telegram_id = str(telegram_id or "").strip()
    display = (display_name or "").strip() or login

    profile = _preflight(login, telegram_id)  # Refused → nothing written yet

    undo: list[tuple[str, Callable[[], Any]]] = []
    created_profile = False
    try:
        # 1. Profile ``01-<login>`` — cloned config/SOUL/skills from the orchestrator when this
        # install has one, never its messaging channels (a copied bot token makes two gateways
        # fight over one bot): ``create_profile`` strips them unless ``clone_channels``.
        from hermes_cli.profiles import create_profile, get_profile_dir, seed_profile_skills

        source = None
        try:
            from hermes_cli.profiles import profile_exists

            if profile_exists(CLONE_SOURCE):
                source = CLONE_SOURCE
        except ValueError:  # pragma: no cover - reserved/invalid name can't be a source
            source = None
        profile_dir = create_profile(
            profile, clone_from=source, clone_config=source is not None, no_alias=True,
            # ``--no-skills`` is mutually exclusive with cloning: a clone copies the source's
            # skills, so the flag only applies to a from-scratch profile.
            no_skills=no_skills and source is None,
        )
        created_profile = True
        undo.append((f"profile {profile}", lambda: delete_profile(profile, yes=True)))

        # 2. SOUL and its questionnaire — the profile's whole point over a bare directory.
        from hermes_acl_soul import QUESTIONNAIRE_FILENAME, seed_questionnaire

        soul_path = get_profile_dir(profile) / "SOUL.md"
        if not soul_path.is_file() or not soul_path.read_text(encoding="utf-8-sig").strip():
            raise Failed(f"{soul_path} is missing or empty after creating {profile}")
        try:
            seed_questionnaire(get_profile_dir(profile), only_personal=True)
        except OSError as exc:  # best effort: the SOUL is what the person reads first
            _err(f"warning: could not write {QUESTIONNAIRE_FILENAME} ({exc})")
        questionnaire = get_profile_dir(profile) / QUESTIONNAIRE_FILENAME
        if not questionnaire.is_file():
            _err(f"warning: {QUESTIONNAIRE_FILENAME} was not seeded into {profile}")

        # 3. Bindings: one person, their profile, their Telegram stamp.
        from hermes_acl_bindings import store as bindings_store

        slug = f"person/{profile[len(PERSONAL_ROOT_PREFIX):]}"
        subjects = [telegram_subject(telegram_id)] if telegram_id else []
        bindings_store().bind(
            slug, profiles=[profile], subjects=subjects, display_name=display,
            source="hermes user add",
        )
        undo.append((f"binding {slug}",
                     lambda: bindings_store().unbind(slug, written_by="hermes user add")))

        # 4. Password + users-file record (the credential they sign in with).
        from datetime import UTC, datetime

        from hermes_cli import dashboard_users as du

        users_path, undo_users_config = ensure_users_file_configured()
        undo.append(("dashboard.basic_auth.users_file", undo_users_config))
        secret = password if password is not None else generate_password()
        password_path, undo_password = write_password_file(login, secret)
        undo.append((f"password file {password_path.name}", undo_password))

        users_store = du.UserStore(users_path)
        record = du.UserRecord(
            login=login,
            password_hash=du.hash_password(secret),
            display_name=display,
            role=role,
            telegram_id=telegram_id,
            gen=1,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        users_store.upsert_record(record)
        undo.append((f"users-file record {login}",
                     lambda: users_store.remove_record(login)))

        # 5. Route: their Telegram messages reach their profile through the owner's bot.
        if telegram_id:
            undo.append(("gateway.profile_routes entry",
                         write_route_undoable(login, profile, telegram_id)))
        else:
            _err("warning: no --telegram-id given, so no gateway.profile_routes entry was "
                 "written; the user can sign in on the dashboard but their Telegram messages "
                 "will not be routed")

        # Fresh profiles own no skills until seeded; clones carry the source's.
        if source is None and not no_skills:
            try:
                seed_profile_skills(get_profile_dir(profile))
            except Exception as exc:  # pragma: no cover - skills are not the credential
                _err(f"warning: skill seeding failed ({exc}); run `{profile} update` later")

    except BaseException as exc:
        if created_profile:
            for step, action in reversed(undo):
                try:
                    action()
                except Exception as undo_exc:
                    _err(f"rollback: {step} could not be undone ({undo_exc})")
        if isinstance(exc, (Refused, Failed)):
            raise
        raise Failed(str(exc)) from exc

    return {
        "login": login,
        "profile": profile,
        "profile_dir": str(profile_dir),
        "slug": slug,
        "subjects": subjects,
        "telegram_id": telegram_id,
        "users_file": str(users_path),
        "password_file": str(password_path),
        "password_prefix": secret[:PASSWORD_PREFIX_CHARS],
        "route": _route_for_profile(login),
    }


# ────────────────────────────────────────────────────────────────────────────
# list / remove
# ────────────────────────────────────────────────────────────────────────────


def list_users() -> list[dict[str, Any]]:
    """Every personal root with what the four registries say about it (never a hash)."""
    from hermes_acl_bindings import store as bindings_store
    from hermes_acl_soul import root_login
    from hermes_cli import dashboard_users as du
    from hermes_cli.profiles import list_profiles

    registry = None
    try:
        registry = bindings_store().read()
    except Exception as exc:  # a broken registry must be visible, not crash the listing
        _err(f"warning: bindings registry unreadable ({exc})")

    path = users_file_path()
    records = []
    if path is not None and path.exists():
        result = du.load_users_file(path)
        records = result.users if result.ok else []

    rows: list[dict[str, Any]] = []
    for entry in list_profiles():
        name = getattr(entry, "name", None) or (entry.get("name") if isinstance(entry, dict) else str(entry))
        login = root_login(name)
        if not login:
            continue
        slug = f"person/{login}"
        record = registry.record(slug) if registry is not None else None
        user = du.lookup(records, login)
        rows.append({
            "login": login,
            "profile": name,
            "bound": record is not None,
            "profiles_in_registry": list(record.profiles) if record else [],
            "subjects": list(record.subjects) if record else [],
            "has_password": user is not None,
            "role": getattr(user, "role", ""),
            "telegram_id": getattr(user, "telegram_id", "") or "",
            "route": _route_for_profile(login),
        })
    rows.sort(key=lambda row: row["login"])
    return rows


def remove_user(login: str, *, say: Callable[[str], None] = _out) -> dict[str, Any]:
    """Remove one user completely: route, binding, users-file record, then the profile."""
    from hermes_acl_bindings import store as bindings_store
    from hermes_cli import dashboard_users as du
    from hermes_cli.profiles import delete_profile, profile_exists

    profile = personal_profile_name(login)
    if not profile:
        raise Refused(f"{login!r} cannot name a person")
    if not profile_exists(profile):
        raise Refused(f"profile {profile} does not exist")

    removed: list[str] = []

    path, document, routes = _routes_doc()
    kept = [route for route in routes
            if not (isinstance(route, dict) and route.get("profile") == profile)]
    if len(kept) != len(routes):
        from hermes_cli.config_backend import write_config_document

        gateway = document.get("gateway")
        if isinstance(gateway, dict):
            gateway["profile_routes"] = kept
            write_config_document(path, document)
        removed.append(f"gateway.profile_routes: {len(routes) - len(kept)} entry")

    slug = f"person/{profile[len(PERSONAL_ROOT_PREFIX):]}"
    registry = bindings_store().read()
    if registry is not None and registry.record(slug) is not None:
        bindings_store().unbind(slug, written_by="hermes user remove")
        removed.append(f"binding {slug}")

    users_path = users_file_path()
    if users_path is not None and users_path.exists():
        store = du.UserStore(users_path)
        if store.remove_record(login):
            removed.append(f"users-file record {login}")

    from hermes_constants import get_hermes_home

    password_path = get_hermes_home() / CREDENTIALS_DIRNAME / f"{login}.password"
    if password_path.exists():
        password_path.unlink()
        removed.append(f"password file {password_path.name}")

    delete_profile(profile, yes=True)
    removed.append(f"profile {profile}")
    return {"login": login, "profile": profile, "removed": removed}


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


def build_user_parser(subparsers) -> None:
    """Attach ``hermes user`` (``add`` / ``list`` / ``remove``)."""
    user = subparsers.add_parser(
        "user",
        help="Provision a whole user: profile, SOUL, binding, password, Telegram route",
        description="Create or remove a person in this install. ``add`` is all-or-nothing: "
                    "if any step fails, everything it created is removed and the exit code "
                    "is non-zero.",
    )
    user.set_defaults(func=_user_no_verb)
    sub = user.add_subparsers(dest="user_action", metavar="<action>")

    p_add = sub.add_parser(
        "add", help="Create a user in one command (profile 01-<login>, SOUL, binding, password, route)",
        description="Creates the profile, its personal SOUL clone and questionnaire, the ACL "
                    "binding, the users-file record (with a generated password) and the "
                    "gateway.profile_routes entry. A duplicate profile, binding or login is "
                    "refused with exit code 2 before anything is written.")
    p_add.add_argument("login", help="Login, used as the profile 01-<login> and the sign-in name")
    p_add.add_argument("--telegram-id", default="",
                       help="Telegram user id of the person (enables their route)")
    p_add.add_argument("--display-name", default="", help="Displayed name (default: the login)")
    p_add.add_argument("--role", choices=("user", "owner"), default="user",
                       help="Users-file role (default: user)")
    p_add.add_argument("--password-stdin", action="store_true",
                       help="Read the password from stdin instead of generating one")
    p_add.add_argument("--no-skills", action="store_true",
                       help="Do not seed bundled skills into the new profile")
    p_add.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    p_add.set_defaults(func=cmd_user_add)

    p_list = sub.add_parser("list", help="List users and what each registry knows about them")
    p_list.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    p_list.set_defaults(func=cmd_user_list)

    p_remove = sub.add_parser("remove", help="Remove a user (route, binding, record, profile)")
    p_remove.add_argument("login")
    p_remove.add_argument("--yes", action="store_true",
                          help="Do not ask for confirmation (for scripts)")
    p_remove.set_defaults(func=cmd_user_remove)


def _user_no_verb(_args) -> int:
    _err("choose add, list or remove")
    return EXIT_REFUSED


def cmd_user_add(args) -> int:
    password = None
    if getattr(args, "password_stdin", False):
        line = sys.stdin.readline()
        if not line:
            _err("--password-stdin was given but stdin was empty")
            return EXIT_FAILED
        password = line.rstrip("\n")
    try:
        result = add_user(
            args.login,
            telegram_id=args.telegram_id,
            display_name=args.display_name,
            role=args.role,
            password=password,
            no_skills=getattr(args, "no_skills", False),
        )
    except Refused as exc:
        _err(f"refused: {exc}")
        return EXIT_REFUSED
    except Failed as exc:
        _err(f"failed (nothing was created): {exc}")
        return EXIT_FAILED

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK
    _out(f"✓ user {result['login']} created")
    _out(f"  profile:        {result['profile']}  ({result['profile_dir']})")
    _out(f"  binding:        {result['slug']}"
         + (f"  subjects={', '.join(result['subjects'])}" if result["subjects"] else ""))
    _out(f"  users file:     {result['users_file']}")
    _out(f"  password file:  {result['password_file']}  (mode 0600)")
    _out(f"  password:       {result['password_prefix']}…  (first {PASSWORD_PREFIX_CHARS} "
         "characters; the full password is in the file above)")
    route = result["route"]
    if route:
        _out(f"  telegram route: platform={route['platform']} user_id={route['user_id']} "
             f"bot_profile={'null (owner bot)' if route['bot_profile'] is None else route['bot_profile']}")
    return EXIT_OK


def cmd_user_list(args) -> int:
    rows = list_users()
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not rows:
        _out("no users (no 01-<login> profiles)")
        return EXIT_OK
    _out(f"{'login':<16} {'profile':<20} {'bound':<6} {'password':<9} {'route'}")
    for row in rows:
        route = "yes" if row["route"] else "no"
        _out(f"{row['login']:<16} {row['profile']:<20} "
             f"{'yes' if row['bound'] else 'no':<6} "
             f"{'yes' if row['has_password'] else 'no':<9} {route}")
    return EXIT_OK


def cmd_user_remove(args) -> int:
    if not getattr(args, "yes", False):
        import getpass

        try:
            confirm = input(f"Type '{args.login}' to confirm removal: ").strip()
        except (EOFError, KeyboardInterrupt):
            confirm = ""
        if confirm != args.login:
            _out("cancelled")
            return EXIT_REFUSED
    try:
        result = remove_user(args.login)
    except Refused as exc:
        _err(f"refused: {exc}")
        return EXIT_REFUSED
    except Exception as exc:
        _err(f"failed: {exc}")
        return EXIT_FAILED
    _out(f"✓ removed {result['login']}: " + ", ".join(result["removed"]))
    return EXIT_OK
