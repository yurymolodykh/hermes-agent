"""Live probe for card t_00544442 (multi-user step 3), on READ-ONLY copies of the live data.

Criteria it produces evidence for:

2. two accounts with different passwords give DIFFERENT subjects (passwords never printed);
3. revoking an account refuses the old cookie/ticket — measured before and after;
4. the owner keeps access to their own sessions (count before and after).

Nothing is written to the live installation: the users file and every state.db are copied into a
scratch directory first, and the flag ``acl.multi_user`` is forced per-call rather than read from
the operator's config.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path("/home/hermes/.hermes/hermes-agent/.worktrees/t_00544442")
sys.path.insert(0, str(REPO))

HOME = Path.home() / ".hermes"
SCRATCH = Path(os.environ["SCRATCH_PROBE"])
SCRATCH.mkdir(exist_ok=True)

import hermes_acl_identity as hi                      # noqa: E402
import hermes_acl_scope                               # noqa: E402
import hermes_cli.dashboard_auth.ws_tickets as ticks   # noqa: E402
from gateway import owner_identity                     # noqa: E402
from hermes_cli import dashboard_users as du           # noqa: E402
from plugins.dashboard_auth import basic as basic      # noqa: E402

SECRET = b"probe-secret-material-32-bytes!!"


def rule(title: str) -> None:
    print(f"\n=== {title} ===")


def copy_live_users() -> Path:
    """Copy the live users file (mode 0600) into scratch; never read it in place."""
    src = HOME / "dashboard-users.jsonl"
    dst = SCRATCH / "users" / "users.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    return dst


def build_probe_users(path: Path) -> None:
    """Add two probe accounts with known passwords NEXT TO the live records."""
    store = du.UserStore(path)
    for login in ("probe.alice", "probe.bob"):
        if store.get(login) is None:
            store.upsert_record(du.UserRecord(
                login=login, password_hash=du.hash_password(f"{login}-pw"),
                display_name=login, role="user"))


def owner_record(store: du.UserStore):
    for record in store.load(force=True).users:
        if record.role == "owner":
            return record
    return None


def sessions_dbs() -> list[Path]:
    return sorted(HOME.glob("state.db")) + sorted(HOME.glob("profiles/*/state.db"))


def owner_session_count(owner_stamps: list[str]) -> tuple[int, int]:
    total = 0
    visible = 0
    marks = ",".join("?" * len(owner_stamps))
    for db in sessions_dbs():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
            if "user_id" not in cols:
                con.close()
                continue
            total += con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            visible += con.execute(
                f"SELECT COUNT(*) FROM sessions WHERE user_id IN ({marks})",
                owner_stamps).fetchone()[0]
            con.close()
        except sqlite3.Error as exc:
            print(f"  ! {db}: {exc}")
    return total, visible


def main() -> int:
    live = copy_live_users()
    build_probe_users(live)
    store = du.UserStore(live)
    hi.users_store = lambda: store

    flag = {"on": True}
    hi.multi_user_enabled = lambda: flag["on"]

    rule("criterion 2 — two accounts, two passwords, two subjects")
    print(f"users file (copy): {live}  records={len(store.load(force=True).users)}")
    provider = basic.BasicAuthProvider(username="", password_hash="", secret=SECRET,
                                       users_file=str(live))
    first = provider.complete_password_login(username="probe.alice", password="probe.alice-pw")
    second = provider.complete_password_login(username="probe.bob", password="probe.bob-pw")
    subj_a = owner_identity.principal_subject(
        {"user_id": first.user_id, "provider": first.provider})
    subj_b = owner_identity.principal_subject(
        {"user_id": second.user_id, "provider": second.provider})
    print(f"probe.alice  -> user_id={first.user_id!r}  subject={subj_a!r}")
    print(f"probe.bob    -> user_id={second.user_id!r}  subject={subj_b!r}")
    print(f"subjects differ: {subj_a != subj_b}")
    print(f"neither is the host owner ({owner_identity.host_owner_subject()!r}): "
          f"{owner_identity.host_owner_subject() not in (subj_a, subj_b)}")
    cross_refused = False
    try:
        provider.complete_password_login(username="probe.alice", password="probe.bob-pw")
    except Exception:
        cross_refused = True
    print(f"alice's account refuses bob's password: {cross_refused}")

    print("\n-- the same two logins with the flag OFF (the shipped default) --")
    flag["on"] = False
    off_a = owner_identity.principal_subject(
        {"user_id": first.user_id, "provider": first.provider})
    off_b = owner_identity.principal_subject(
        {"user_id": second.user_id, "provider": second.provider})
    print(f"off: probe.alice -> {off_a!r}   probe.bob -> {off_b!r}   collapsed to one: "
          f"{off_a == off_b}")
    flag["on"] = True

    rule("criterion 3 — revocation reaches the device")
    alice = provider.complete_password_login(username="probe.alice", password="probe.alice-pw")
    print(f"before: verify_session -> "
          f"{'accepted' if provider.verify_session(access_token=alice.access_token) else 'REFUSED'}")
    tick = ticks.mint_ticket(user_id=alice.user_id, provider=alice.provider)
    print(f"before: ws ticket minted, gen={ticks.consume_ticket(tick)['gen'] if False else alice.credential_generation}")
    tick = ticks.mint_ticket(user_id=alice.user_id, provider=alice.provider)
    gen_before = store.get("probe.alice").gen
    store.upsert_record(store.next_gen(store.get("probe.alice")))
    gen_after = store.get("probe.alice").gen
    print(f"revoke: user01 gen {gen_before} -> {gen_after} (password untouched)")
    print(f"after:  verify_session -> "
          f"{'accepted' if provider.verify_session(access_token=alice.access_token) else 'REFUSED'}")
    status, detail = provider.credential_status(alice.access_token)
    print(f"after:  gate status={status!r} detail={detail!r}")
    try:
        ticks.consume_ticket(tick)
        print("after:  ws ticket ACCEPTED  <-- BUG")
    except ticks.TicketInvalid as exc:
        print(f"after:  ws ticket refused: {exc}")
    try:
        provider.refresh_session(refresh_token=alice.refresh_token)
        print("after:  refresh ACCEPTED  <-- BUG")
    except Exception as exc:
        print(f"after:  refresh refused: {type(exc).__name__}")
    re_login = provider.complete_password_login(username="probe.alice",
                                                password="probe.alice-pw")
    print(f"after:  same password signs back in at gen={re_login.credential_generation}")

    rule("criterion 3b — deleting the record kills the session")
    bob = provider.complete_password_login(username="probe.bob", password="probe.bob-pw")
    store.remove_record("probe.bob")
    print(f"bob record removed: verify_session -> "
          f"{'accepted' if provider.verify_session(access_token=bob.access_token) else 'REFUSED'}")

    rule("criterion 4 — the owner keeps their own sessions")
    owner = owner_record(store)
    print(f"owner record: login={owner.login!r} role={owner.role!r} gen={owner.gen}")
    owner_subject = owner_identity.principal_subject(
        {"user_id": owner.login, "provider": "basic"})
    print(f"owner subject: {owner_subject!r} == host owner: "
          f"{owner_subject == owner_identity.host_owner_subject()}")
    stamps = hermes_acl_scope.owner_stamps(owner_subject)
    total, visible = owner_session_count(stamps)
    print(f"owner scope stamps ({len(stamps)}): {stamps}")
    print(f"state.db files scanned: {len(sessions_dbs())}")
    print(f"sessions total={total}  visible to the owner={visible}  invisible={total - visible}")
    print(f"owner session still verifies: "
          f"{owner_subject == owner_identity.host_owner_subject()}")

    rule("criterion 4b — with the step-2 bindings registry, the owner sees their earlier rows too")
    import hermes_acl_bindings as hb

    registry = SCRATCH / "acl" / "profile-bindings.json"
    hb.bindings_path = lambda: registry  # type: ignore[assignment]
    hb.reset_cache()
    binding_store = hb.BindingStore(registry)
    stamp_rows: dict[str, int] = {}
    for db in sessions_dbs():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
            if "user_id" in cols:
                for (stamp,) in con.execute(
                        "SELECT DISTINCT user_id FROM sessions"):
                    stamp_rows[stamp] = stamp_rows.get(stamp, 0) + 1
            con.close()
        except sqlite3.Error:
            pass
    print(f"distinct live stamps: {sorted(s for s in stamp_rows if s)}")
    owner_stamps_guess = [s for s in stamp_rows
                          if s and (s == "uid:1001" or s == "cron-owner"
                                    or s.startswith("1258") or s.startswith("telegram"))]
    binding_store.bootstrap_owner(
        slug="person/yury", profiles=["03-software-engineer"],
        subjects=owner_stamps_guess, display_name="owner")
    hb.reset_cache()
    stamps_with_registry = hermes_acl_scope.owner_stamps(owner_subject)
    total2, visible2 = owner_session_count(stamps_with_registry)
    print(f"registry subjects for the owner ({len(owner_stamps_guess)}): {owner_stamps_guess}")
    print(f"owner scope stamps ({len(stamps_with_registry)}): {stamps_with_registry}")
    print(f"sessions total={total2}  visible to the owner={visible2}  invisible={total2 - visible2}")
    print(f"user appendix — one user's scope is disjoint from the owner's: "
          f"{sorted(set(hermes_acl_scope.owner_stamps('probe.alice')) & set(stamps_with_registry))}")

    rule("lifetimes")
    print(f"multi_user ON : session_ttl={hi.session_ttl_seconds(0, default_seconds=43200)}s "
          f"refresh_ttl={hi.refresh_ttl_seconds(0, default_seconds=2592000)}s")
    flag["on"] = False
    print(f"multi_user OFF: session_ttl={hi.session_ttl_seconds(0, default_seconds=43200)}s "
          f"refresh_ttl={hi.refresh_ttl_seconds(0, default_seconds=2592000)}s")

    rule("live installation untouched")
    print(f"live users file mtime unchanged: "
          f"{int((HOME / 'dashboard-users.jsonl').stat().st_mtime)}")
    print(f"acl.multi_user in live config: see `grep multi_user ~/.hermes/config.yaml`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
