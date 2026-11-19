"""``hermes auth users`` — the users file from the command line.

Six verbs over ``dashboard.basic_auth.users_file``:

* ``list``    — login, role, status, whether aliases/telegram exist. Never a hash, never a password.
* ``add``     — create a record; the password arrives on stdin or from a masked prompt, never argv
                (an argv password is visible in ``ps`` to every account on the host).
* ``passwd``  — replace a password; raises ``gen`` by one (the generation is what step 3 uses to
                drop devices holding an older credential generation).
* ``disable`` / ``enable`` — flip ``disabled``; both raise ``gen`` as well.
* ``import``  — read a Windows-host export and merge by telegram id / e-mail, keeping old logins
                as aliases and never overwriting an existing hash without ``--overwrite``.

The store (``hermes_cli.dashboard_users``) owns parsing, validation and the atomic rewrite; this
module is only the CLI surface. Nothing here prints hash material.
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path
from typing import Optional

from hermes_cli import dashboard_users as du


def _users_no_verb(args) -> int:
    """`hermes auth users` with no verb: say what the verbs are."""
    print("hermes auth users: choose list, add, passwd, disable, enable or import",
          file=sys.stderr)
    return 2


def build_auth_users_parser(subparsers) -> None:
    """Attach the ``users`` subtree under ``hermes auth`` (``subparsers`` is auth's)."""
    users = subparsers.add_parser(
        "users", help="Manage dashboard users (the users file)",
        description="Manage the dashboard users file (dashboard.basic_auth.users_file): list, "
                    "add, change passwords, disable/enable, and import a Windows-host export. "
                    "Hashes and passwords are never printed.")
    sub = users.add_subparsers(dest="users_action", metavar="<action>")
    # A bare `hermes auth users` must not inherit the parent's `func=cmd_auth` (that would
    # recurse), and main() always calls `args.func(args)`, so it gets its own handler.
    users.set_defaults(func=_users_no_verb)

    p_list = sub.add_parser("list", help="List users (login, role, status, aliases, telegram)")
    p_list.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    p_list.set_defaults(func=cmd_users_list)

    p_add = sub.add_parser("add", help="Add a user")
    p_add.add_argument("login", help="Login (what the person types)")
    p_add.add_argument("--display-name", default="", help="Displayed name (default: the login)")
    p_add.add_argument("--role", choices=du.ROLES, default="user", help="owner or user (default: user)")
    p_add.add_argument("--alias", action="append", default=[], dest="aliases",
                       help="Extra login that resolves to this record (repeatable)")
    p_add.add_argument("--telegram-id", default="", help="Bound Telegram user id")
    p_add.add_argument("--email", default="", help="E-mail (also a merge key during import)")
    p_add.add_argument("--password-stdin", action="store_true",
                       help="Read the password from stdin (first line); never pass it as an argument")
    p_add.set_defaults(func=cmd_users_add)

    p_passwd = sub.add_parser("passwd", help="Change a password (raises gen)")
    p_passwd.add_argument("login", help="Login of the record to change")
    p_passwd.add_argument("--password-stdin", action="store_true",
                          help="Read the password from stdin (first line)")
    p_passwd.set_defaults(func=cmd_users_passwd)

    p_disable = sub.add_parser("disable", help="Disable a user (raises gen)")
    p_disable.add_argument("login")
    p_disable.set_defaults(func=cmd_users_disable)

    p_enable = sub.add_parser("enable", help="Re-enable a user (raises gen)")
    p_enable.add_argument("login")
    p_enable.set_defaults(func=cmd_users_enable)

    p_import = sub.add_parser(
        "import", help="Import a Windows-host export (login|display_name|password_hash|...)",
        description="Add records from a pipe-separated export. Existing records are matched by "
                    "telegram id and e-mail (never by login similarity); a matched record gains "
                    "the incoming login as an alias. Existing hashes are kept unless --overwrite.")
    p_import.add_argument("file", help="Export file (use '-' for stdin)")
    p_import.add_argument("--overwrite", action="store_true",
                          help="Replace an existing record's password hash with the imported one")
    p_import.add_argument("--dry-run", action="store_true", help="Report without writing the file")
    p_import.set_defaults(func=cmd_users_import)


# ---- plumbing -------------------------------------------------------------

def _fail(message: str) -> int:
    print(f"hermes auth users: {message}", file=sys.stderr)
    return 1


def _store() -> Optional[du.UserStore]:
    """The configured store, or None with the reason already printed."""
    path = _configured_users_file()
    if path is None:
        _fail("no users file configured. Set dashboard.basic_auth.users_file in config.yaml.")
        return None
    return du.UserStore(path)


def _configured_users_file() -> Optional[Path]:
    try:
        from hermes_cli.config import cfg_get, load_config

        value = cfg_get(load_config(), "dashboard", "basic_auth", "users_file", default="")
    except Exception as exc:
        _fail(f"cannot read config: {exc}")
        return None
    return du.resolve_users_file_path(value)


def _load_checked(store: du.UserStore) -> Optional[list[du.UserRecord]]:
    """Records from *store*, or None after printing why the file cannot be used."""
    try:
        result = store.load(force=True)
    except du.UsersFilePermissionError as exc:
        _fail(str(exc))
        return None
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not result.ok:
        for problem in result.problems:
            print(f"error: {problem}", file=sys.stderr)
        _fail("the users file is unusable; fix it before managing users")
        return None
    return result.users


def _read_password(*, confirm: bool) -> Optional[str]:
    """Password from stdin (``--password-stdin``) or a masked interactive prompt."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            _fail("--password-stdin was given but stdin was empty")
            return None
        return line.rstrip("\n")
    password = getpass.getpass("Password: ")
    if confirm:
        again = getpass.getpass("Repeat password: ")
        if password != again:
            _fail("passwords do not match")
            return None
    if not password:
        _fail("empty password")
        return None
    return password


def _password_for(args) -> Optional[str]:
    # Deliberately NO --password flag: an argv password is readable in `ps` by every account.
    return _read_password(confirm=not getattr(args, "password_stdin", False))


# ---- commands -------------------------------------------------------------

def cmd_users_list(args) -> int:
    path = _configured_users_file()
    if path is None:
        return _fail("no users file configured. Set dashboard.basic_auth.users_file in config.yaml.")
    if not path.exists():
        print(f"users file {path} does not exist yet (no users)")
        return 0
    store = du.UserStore(path)
    records = _load_checked(store)
    if records is None:
        return 1
    if getattr(args, "json", False):
        # The on-disk shape without password_hash — never emit hash material.
        print(json.dumps([{k: v for k, v in r.to_json().items() if k != "password_hash"}
                          for r in records], ensure_ascii=False, indent=2))
        return 0
    print(f"users file: {path}")
    print(du.render_list(records))
    return 0


def cmd_users_add(args) -> int:
    store = _store()
    if store is None:
        return 1
    records = _load_checked(store)
    if records is None:
        return 1
    if du.lookup(records, args.login) is not None:
        return _fail(f"login {args.login!r} (or an alias) already exists")

    password = _password_for(args)
    if password is None:
        return 1

    from datetime import UTC, datetime

    record = du.UserRecord(
        login=args.login,
        password_hash=du.hash_password(password),
        display_name=args.display_name or args.login,
        role=args.role,
        aliases=tuple(args.aliases),
        telegram_id=args.telegram_id,
        email=args.email,
        gen=1,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    for name in record.all_logins():
        if du.lookup(records, name) is not None:
            return _fail(f"{name!r} resolves to an existing record; pick another login/alias")
    store.upsert_record(record)
    print(f"added {record.login} (role {record.role}, gen {record.gen})")
    return 0


def cmd_users_passwd(args) -> int:
    store = _store()
    if store is None:
        return 1
    records = _load_checked(store)
    if records is None:
        return 1
    record = store.get(args.login)
    if record is None:
        return _fail(f"no user with login {args.login!r}")
    password = _password_for(args)
    if password is None:
        return 1
    from dataclasses import replace as _replace

    updated = store.next_gen(_replace(record, password_hash=du.hash_password(password)))
    store.upsert_record(updated)
    print(f"password changed for {updated.login}; gen {record.gen} -> {updated.gen}")
    return 0


def _set_disabled(args, *, disabled: bool) -> int:
    store = _store()
    if store is None:
        return 1
    records = _load_checked(store)
    if records is None:
        return 1
    record = store.get(args.login)
    if record is None:
        return _fail(f"no user with login {args.login!r}")
    if record.disabled == disabled:
        print(f"{record.login} is already {'disabled' if disabled else 'enabled'}")
        return 0
    from dataclasses import replace as _replace

    updated = store.next_gen(_replace(record, disabled=disabled))
    store.upsert_record(updated)
    print(f"{updated.login} is now {'disabled' if disabled else 'enabled'}; gen {record.gen} -> {updated.gen}")
    return 0


def cmd_users_disable(args) -> int:
    return _set_disabled(args, disabled=True)


def cmd_users_enable(args) -> int:
    return _set_disabled(args, disabled=False)


def cmd_users_import(args) -> int:
    store = _store()
    if store is None:
        return 1
    records = _load_checked(store)
    if records is None:
        return 1

    if args.file == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            return _fail(f"cannot read {args.file}: {exc}")

    rows, problems = du.parse_export_text(text)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return _fail(f"{len(problems)} malformed line(s) in the export; nothing imported")

    merged, report = du.merge_import(records, rows, overwrite=args.overwrite)
    print(report.render())
    if args.dry_run:
        print("(dry run: the users file was not written)")
        return 0
    if merged != records:
        store.write_records(merged)
        print(f"users file updated: {len(merged)} record(s) at {store.path}")
    else:
        print("users file unchanged")
    return 0
