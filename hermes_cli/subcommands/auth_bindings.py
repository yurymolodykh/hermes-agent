"""``hermes auth bindings`` — the person→profiles registry from the command line.

Five verbs over ``acl.bindings_file`` (``hermes_acl_bindings``):

* ``list``       — every record: person, profiles, shared marker, subjects, source. Never a secret:
                  the registry holds no credential material at all.
* ``show``       — one record plus what the registry answers about it (its stamps, its profiles).
* ``bind``       — create/update a person's record: ``--profile`` (repeatable) and ``--subject``
                  (repeatable: a stored ``sessions.user_id`` stamp, e.g. ``uid:1001``).
* ``unbind``     — remove a record (the cascade a deleted account needs).
* ``share``      — replace the shared (public) profile set: every person may use those.
* ``owner``      — the FIRST binding: the host owner's subject(s) → their personal profile(s), plus
                  the shared set. Idempotent, so it can be re-run as a repair.

Every write goes through :class:`hermes_acl_bindings.BindingStore`, so it is validated, backed up
and atomic. Nothing here enables multi-user access: ``acl.enabled`` stays as the operator set it and
step 3 owns the first enablement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import hermes_acl_bindings as hb


def _bindings_no_verb(args) -> int:
    """`hermes auth bindings` with no verb: say what the verbs are."""
    print("hermes auth bindings: choose list, show, bind, unbind, share or owner", file=sys.stderr)
    return 2


def build_auth_bindings_parser(subparsers) -> None:
    """Attach the ``bindings`` subtree under ``hermes auth`` (``subparsers`` is auth's)."""
    bindings = subparsers.add_parser(
        "bindings", help="Manage the person→profiles bindings registry",
        description="Manage the ACL bindings registry (acl.bindings_file): which profiles belong to "
                    "which person, and which are shared. The registry only takes effect while "
                    "acl.enabled is true; this command never changes that switch.")
    sub = bindings.add_subparsers(dest="bindings_action", metavar="<action>")
    bindings.set_defaults(func=_bindings_no_verb)

    p_list = sub.add_parser("list", help="List records (person, profiles, shared, subjects)")
    p_list.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    p_list.set_defaults(func=cmd_bindings_list)

    p_show = sub.add_parser("show", help="Show one record and what the registry answers for it")
    p_show.add_argument("slug", help="Record slug (person/<login> or shared/<name>)")
    p_show.set_defaults(func=cmd_bindings_show)

    p_bind = sub.add_parser("bind", help="Create or update a person's record")
    p_bind.add_argument("slug", help="Record slug: person/<login>")
    # NOT --profile: main._apply_profile_override consumes `--profile/-p` from argv to re-home the
    # process to another Hermes profile BEFORE argparse runs, so a `--profile` here would silently
    # switch profiles instead of naming a bound one.
    p_bind.add_argument("--holds", action="append", default=[], dest="profiles",
                        help="Profile this person holds (repeatable)")
    p_bind.add_argument("--subject", action="append", default=[], dest="subjects",
                        help="Stored owner stamp naming this person (repeatable), e.g. uid:1001")
    p_bind.add_argument("--display-name", default="", help="Displayed name (default: the slug)")
    p_bind.add_argument("--source", default="admin", help="Why the record exists (default: admin)")
    p_bind.set_defaults(func=cmd_bindings_bind)

    p_unbind = sub.add_parser("unbind", help="Remove a record entirely")
    p_unbind.add_argument("slug", help="Record slug to remove")
    p_unbind.set_defaults(func=cmd_bindings_unbind)

    p_share = sub.add_parser("share", help="Replace the shared (public) profile set")
    p_share.add_argument("profiles", nargs="*", help="Profiles every person may use")
    p_share.add_argument("--slug", default="shared/service", help="Shared record slug")
    p_share.set_defaults(func=cmd_bindings_share)

    p_owner = sub.add_parser(
        "owner", help="Write the first binding: the host owner's subject(s) → their profiles",
        description="Idempotent bootstrap: records the owner's legacy stamps (uid:<n>, cron-owner, "
                    "Telegram id) as ONE person so the sessions created before multi-user identity "
                    "stay reachable by their owner.")
    p_owner.add_argument("--slug", default="person/owner", help="Owner record slug")
    p_owner.add_argument("--holds", action="append", default=[], dest="profiles",
                         help="Profile the owner holds (repeatable; default: 01-orchestrator)")
    p_owner.add_argument("--subject", action="append", default=[], dest="subjects",
                         help="Owner stamp (repeatable; default: this host's uid:<n> and cron-owner)")
    p_owner.add_argument("--shared", action="append", default=[], dest="shared_profiles",
                         help="Profile to also mark shared (repeatable)")
    p_owner.add_argument("--display-name", default="", help="Displayed name")
    p_owner.add_argument("--dry-run", action="store_true", help="Show the record without writing")
    p_owner.set_defaults(func=cmd_bindings_owner)


def _store() -> hb.BindingStore:
    """The registry store. ``bindings_path()`` already degrades to the default path when
    ``acl.bindings_file`` is unreadable, so there is nothing here to catch."""
    return hb.store()


def _load(store: hb.BindingStore) -> Optional[hb.Bindings]:
    """The registry, or ``None`` after reporting why it cannot be read. A broken file is a REFUSAL:
    this CLI never overwrites it silently (that would destroy the previous access model)."""
    try:
        return store.load()
    except hb.BindingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"The registry is not touched. Restore a copy from "
              f"{hb.backup_path_latest(store.path)} or fix the file by hand.", file=sys.stderr)
        return None


def cmd_bindings_list(args) -> int:
    store = _store()
    registry = _load(store)
    if registry is None:
        return 1
    if args.json:
        print(json.dumps(registry.to_json(), indent=2, ensure_ascii=False))
        return 0
    print(f"registry: {store.path}")
    if not registry.persons:
        print("(no records yet — run `hermes auth bindings owner` to bind the host owner)")
        return 0
    header = ("slug", "shared", "profiles", "subjects", "source")
    rows = [
        (record.slug, "yes" if record.shared else "no", ", ".join(record.profiles) or "-",
         ", ".join(record.subjects) or "-", record.source or "-")
        for record in sorted(registry.persons.values(), key=lambda r: r.slug)
    ]
    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header))]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def cmd_bindings_show(args) -> int:
    store = _store()
    registry = _load(store)
    if registry is None:
        return 1
    record = registry.record(args.slug)
    if record is None:
        print(f"error: no record {args.slug!r} in {store.path}", file=sys.stderr)
        return 1
    print(json.dumps(record.to_json(), indent=2, ensure_ascii=False))
    print(f"person_of({args.slug!r}) -> {registry.person_of(args.slug)!r}")
    print(f"subjects_of         -> {list(registry.subjects_of(args.slug))}")
    print(f"profiles_of         -> {list(registry.profiles_of(args.slug))}")
    return 0


def cmd_bindings_bind(args) -> int:
    store = _store()
    if not args.profiles:
        print("error: --holds is required (which profiles does this person hold?)", file=sys.stderr)
        return 2
    try:
        registry = store.bind(
            args.slug, profiles=args.profiles, subjects=args.subjects,
            display_name=args.display_name, source=args.source,
        )
    except hb.BindingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    record = registry.record(args.slug)
    if record is None:  # bind() always inserts it; guard anyway so a None can never print a trace
        print(f"error: {args.slug!r} missing right after the write", file=sys.stderr)
        return 1
    print(f"bound {args.slug}: profiles={list(record.profiles)} subjects={list(record.subjects)}")
    print(f"registry: {store.path}  (backup: {hb.backup_path_latest(store.path)})")
    return 0


def cmd_bindings_unbind(args) -> int:
    store = _store()
    registry = _load(store)
    if registry is None:
        return 1
    if registry.record(args.slug) is None:
        print(f"record {args.slug!r} is not present; nothing to remove")
        return 0
    try:
        store.unbind(args.slug)
    except hb.BindingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"unbound {args.slug}")
    print(f"registry: {store.path}  (backup: {hb.backup_path_latest(store.path)})")
    return 0


def cmd_bindings_share(args) -> int:
    store = _store()
    try:
        registry = store.set_shared(args.profiles, slug=args.slug)
    except hb.BindingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    record = registry.record(args.slug)
    if record is None:
        print(f"error: {args.slug!r} missing right after the write", file=sys.stderr)
        return 1
    print(f"shared profiles ({args.slug}): {list(record.profiles)}")
    print(f"registry: {store.path}  (backup: {hb.backup_path_latest(store.path)})")
    return 0


def _default_owner_subjects() -> list[str]:
    """The stamps this install's existing rows actually carry: the OS identity and cron."""
    import os
    from hermes_state_keys import CRON_PRINCIPAL_ID
    if os.name == "nt":
        from gateway.session_worker_reservation import owner_worker_subject
        primary = owner_worker_subject()
    else:
        primary = f"uid:{os.getuid()}"  # windows-footgun: ok — POSIX branch
    return [primary, CRON_PRINCIPAL_ID]


def cmd_bindings_owner(args) -> int:
    store = _store()
    profiles = list(args.profiles) or ["01-orchestrator"]
    subjects = list(args.subjects) or _default_owner_subjects()
    if args.dry_run:
        print(json.dumps({"slug": args.slug, "profiles": profiles, "subjects": subjects,
                          "shared": list(args.shared_profiles)}, indent=2, ensure_ascii=False))
        print(f"would write: {store.path}")
        return 0
    try:
        registry = store.bootstrap_owner(
            slug=args.slug, profiles=profiles, subjects=subjects,
            display_name=args.display_name, shared_profiles=args.shared_profiles,
            written_by=args.slug,
        )
    except hb.BindingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    record = registry.record(args.slug)
    if record is None:
        print(f"error: {args.slug!r} missing right after the write", file=sys.stderr)
        return 1
    print(f"owner {args.slug}: profiles={list(record.profiles)} subjects={list(record.subjects)}")
    if registry.shared_profiles():
        print(f"shared: {list(registry.shared_profiles())}")
    print(f"registry: {store.path}  (backup: {hb.backup_path_latest(store.path)})")
    print("acl.enabled is unchanged — the registry only takes effect once step 3 turns it on.")
    return 0
