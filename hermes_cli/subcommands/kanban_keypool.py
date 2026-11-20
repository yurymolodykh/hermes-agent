"""``hermes kanban keypool …`` — per-card API-key pool control surface.

Owner-facing operations for the ``key-pool-per-card`` mechanism
(``agent/kanban_key_pool.py``). Everything an operator needs, and nothing a
card needs at runtime:

* ``status``  — is the pool in force, which keys are free/reserved/quarantined,
                and what is THIS process pinned to;
* ``off``     — one-command rollback to the pre-pool shared-key scheme;
* ``on``      — re-enable after a rollback;
* ``keys``    — list the configured key ids/labels/roles (**never** the secrets);
* ``sweep``   — reap reservations whose owner process is gone / TTL expired;
* ``clear``   — drop a quarantine record (after a key was fixed);
* ``check``   — the owner's verification recipe: show which task/pid holds
                which key, which is what makes "cards really went to different
                accounts" provable from the machine.

Secret handling: the key file holds plaintext API keys (the owner ships them
via chat and permits them in logs — decision #6). This module never prints
``api_key``; every view goes through ``pool_status()`` / ``load_config()`` and
reads ids/labels/roles only.
"""

from __future__ import annotations

import argparse
import json
import sys


def _pool():
    from agent import kanban_key_pool

    return kanban_key_pool


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))


def cmd_keypool(args: argparse.Namespace) -> int:
    action = getattr(args, "keypool_action", None) or "status"
    kp = _pool()
    as_json = bool(getattr(args, "json", False))

    try:
        if action == "status":
            return _cmd_status(kp, as_json)
        if action == "keys":
            return _cmd_keys(kp, as_json)
        if action == "off":
            return _cmd_off(kp, as_json)
        if action == "on":
            return _cmd_on(kp, as_json)
        if action == "sweep":
            return _cmd_sweep(kp, as_json)
        if action == "clear":
            return _cmd_clear(kp, args, as_json)
        if action == "check":
            return _cmd_check(kp, args, as_json)
        print(f"kanban keypool: unknown action {action!r}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"kanban keypool {action}: {exc}", file=sys.stderr)
        return 1


# ── status ─────────────────────────────────────────────────────────────────


def _cmd_status(kp, as_json: bool) -> int:
    status = kp.pool_status()
    if as_json:
        _print_json(status)
        return 0

    # The env override is checked BEFORE the "no key file" early return.
    # ``HERMES_KANBAN_KEYPOOL=off`` makes ``key_pool_path()`` fall back to the
    # default root (an ``off`` value is not a path), so when no key file
    # exists there the command used to print "NOT CONFIGURED — no keys issued
    # yet" for an operator who had just switched the pool off on purpose. The
    # two states are opposites (deliberately off vs never configured) and must
    # not share a sentence.
    if status.get("env_says_off"):
        print("key pool: DISABLED")
        print(f"  disabled by env {kp.ENV_KEYPOOL}={status.get('env_override')}")
        print(f"  key file:   {status['key_file']}")
        print("  (an explicit env override — unset it to let the key file decide)")
        return 0

    if not status["key_file_exists"]:
        print("key pool: NOT CONFIGURED — behaviour is the plain single-key scheme")
        print(f"  expected key file: {status['key_file']}")
        print("  (a missing key file is the 'no keys issued yet' state, not an error)")
        return 0

    state_label = "ENABLED" if status["enabled"] else "DISABLED"
    print(f"key pool: {state_label}")
    print(f"  key file:   {status['key_file']}")
    print(f"  state file: {status['state_file']}")
    print(f"  endpoint:   {status['base_url']}  (provider {status['provider']})")
    print(
        f"  background key: #{status['background_key_id']} "
        f"| card keys: {status['card_key_ids']} of {status['key_count']} total"
    )
    excluded = status.get("excluded_card_key_ids") or []
    if excluded:
        # Without this line an owner who marked a key excluded sees it silently
        # vanish from `card keys` and has to diff the file to find out why.
        labels = status.get("excluded_card_key_labels") or {}
        names = ", ".join(
            f"#{kid}" + (f" ({labels[str(kid)]})" if str(kid) in labels else "")
            for kid in excluded
        )
        print(f"  excluded from cards: {names} — held back on purpose, not free capacity")
    print(
        f"  card limit per key: {status.get('max_cards_per_key')} "
        f"(capacity: {len(status['card_key_ids']) * int(status.get('max_cards_per_key') or 1)} "
        f"card slots)"
    )
    if not status["enabled"]:
        if status["disabled_marker_exists"]:
            print(f"  rollback sentinel present: {status['disabled_marker']}")
        elif status.get("inactive_reason") == "config_disabled":
            print("  disabled via 'enabled: false' in the key file")
        elif status.get("inactive_reason") == "no_card_keys":
            print(
                "  NOT IN FORCE: the key file has fewer than two keys or no "
                "key with role 'card' — behaviour is the plain single-key scheme"
            )
        elif status.get("inactive_reason") == "no_base_url":
            print(
                "  NOT IN FORCE: the key file declares no base_url, so a pool "
                "key could be sent to any host — fill it in or the pool stays off"
            )
        elif status.get("inactive_reason") == "unusable_key_file":
            # Review N2: every schema-level rejection in ``load_config``
            # collapses to this one reason (invalid JSON, non-object root,
            # duplicate id, empty api_key, role outside {background, card},
            # malformed/mismatched background_key_id, a second background
            # key). Without its own branch the operator's typo in a freshly
            # issued key file printed exactly like a deliberate shutdown:
            # "key pool: DISABLED" and nothing else.
            print(
                "  NOT IN FORCE: the key file is unusable — the pool cannot "
                "distribute keys from it. Fix the schema and restart the "
                "workers that need it."
            )
            print(
                "  Common causes: invalid JSON, duplicate key id, empty "
                "api_key, a role outside {background, card}, or a "
                "background_key_id that is not the one background key."
            )
            print(
                "  The exact reason is the [key-pool] warning on stderr; a "
                "deliberate rollback (`hermes kanban keypool off`) reads "
                "differently and does not print this block."
            )

    print("\nkeys (occupied/limit):")
    if not status.get("keys"):
        print("  (no card keys)")
    for row in status.get("keys") or []:
        tasks = ", ".join(str(t) for t in row.get("task_ids") or []) or "—"
        print(
            f"  key #{row.get('key_id')} ({row.get('key_label')}): "
            f"{row.get('used')}/{row.get('limit')}  [{tasks}]"
        )

    print("\nreservations:")
    if not status["reservations"]:
        print("  (none)")
    for entry in status["reservations"]:
        flag = " STALE" if entry.get("stale") else ""
        print(
            f"  key #{entry.get('key_id')} ({entry.get('key_label')}) -> "
            f"task {entry.get('task_id')} run {entry.get('run_id')} "
            f"pid {entry.get('pid')} host {entry.get('host')} "
            f"until {entry.get('expires_at')}{flag}"
        )

    print("\nquarantined keys:")
    if not status["failed_keys"]:
        print("  (none)")
    for record in status["failed_keys"]:
        until = record.get("quarantine_until") or 0
        window = "until operator clears it" if not until else f"until {until}"
        print(
            f"  key #{record.get('key_id')} ({record.get('key_label')}) — "
            f"HTTP {record.get('status_code')} on task {record.get('task_id')} "
            f"({window})"
        )

    me = status.get("this_process")
    if me:
        print(f"\nthis process: {json.dumps(me, ensure_ascii=False)}")
    return 0


# ── keys ───────────────────────────────────────────────────────────────────


def _cmd_keys(kp, as_json: bool) -> int:
    cfg = kp.load_config()
    if cfg is None:
        print(
            f"key pool: no usable key file at {kp.key_pool_path()}",
            file=sys.stderr,
        )
        return 1
    rows = [
        {
            "id": kid,
            "label": cfg.label(kid),
            "role": entry.get("role"),
        }
        for kid, entry in sorted(cfg.keys.items())
    ]
    if as_json:
        _print_json({"base_url": cfg.base_url, "provider": cfg.provider,
                     "background_key_id": cfg.background_key_id, "keys": rows})
        return 0
    print(f"keys in {kp.key_pool_path()} (secrets never shown):")
    for row in rows:
        mark = "  (background)" if row["id"] == cfg.background_key_id else ""
        print(f"  #{row['id']:<3} {row['label']:<20} role={row['role']}{mark}")
    return 0


# ── on / off ───────────────────────────────────────────────────────────────


def _cmd_off(kp, as_json: bool) -> int:
    marker = kp.set_enabled(False)
    payload = {
        "enabled": False,
        "sentinel": str(marker),
        "scope": "newly started workers; a card already holding a key keeps it",
    }
    if as_json:
        _print_json(payload)
        return 0
    print("key pool: ROLLED BACK to the single shared key scheme")
    print(f"  sentinel: {marker}")
    print("  cards already running keep their reserved key until they finish —")
    print("  a reservation is never yanked out from under a live run.")
    print("  Applies to every worker started from now on. Re-enable with")
    print("  `hermes kanban keypool on`.")
    return 0


def _cmd_on(kp, as_json: bool) -> int:
    marker = kp.set_enabled(True)
    cfg = kp.load_config()
    if cfg is None:
        print(
            f"key pool: sentinel removed ({marker}) but the key file is missing "
            f"or unusable — behaviour stays single-key until it exists",
            file=sys.stderr,
        )
        return 1
    payload = {
        "enabled": True,
        "sentinel_removed": str(marker),
        "card_key_ids": cfg.card_key_ids,
        "background_key_id": cfg.background_key_id,
    }
    if as_json:
        _print_json(payload)
        return 0
    print("key pool: ENABLED again")
    print(
        f"  background key #{cfg.background_key_id} | card keys {cfg.card_key_ids}"
    )
    return 0


# ── sweep / clear ──────────────────────────────────────────────────────────


def _cmd_sweep(kp, as_json: bool) -> int:
    dropped = kp.sweep()
    if as_json:
        _print_json({"reaped": dropped, "count": len(dropped)})
        return 0
    if not dropped:
        print("key pool: nothing to reap — every reservation is alive")
        return 0
    print(f"key pool: reaped {len(dropped)} stale reservation(s)")
    for entry in dropped:
        print(
            f"  key #{entry.get('key_id')} task {entry.get('task_id')} "
            f"pid {entry.get('pid')} ({entry.get('released_reason')})"
        )
    return 0


def _cmd_clear(kp, args: argparse.Namespace, as_json: bool) -> int:
    raw = getattr(args, "key_id", None)
    key_id = None
    if raw is not None:
        try:
            key_id = int(raw)
        except (TypeError, ValueError):
            print("kanban keypool clear: --key must be an integer key id", file=sys.stderr)
            return 2
    cleared = kp.clear_quarantine(key_id)
    if as_json:
        _print_json({"cleared": cleared, "count": len(cleared)})
        return 0
    if not cleared:
        print("key pool: no quarantine record matched")
        return 0
    print(f"key pool: cleared {len(cleared)} quarantine record(s)")
    for record in cleared:
        print(
            f"  key #{record.get('key_id')} ({record.get('key_label')}) was "
            f"HTTP {record.get('status_code')} on task {record.get('task_id')}"
        )
    return 0


# ── check ──────────────────────────────────────────────────────────────────


def _cmd_check(kp, args: argparse.Namespace, as_json: bool) -> int:
    """Owner verification: prove the slot discipline holds right now.

    Prints, per reserved card, the key id and the task/pid that holds it, plus
    an explicit verdict line. The criterion changed with the slot model
    (``max_cards_per_key``): two cards sharing one key is now CORRECT as long
    as the key stays within its limit, so "distinct key ids" is no longer the
    test. What must hold instead:

    * no key carries more cards than ``max_cards_per_key`` (over-subscription);
    * a card never appears on two keys at once (the double-assignment the
      mission exists to prevent);
    * the background key is never handed to a card.

    With one card there is nothing to compare, so the verdict says so instead
    of pretending success.
    """
    status = kp.pool_status()
    reservations = [r for r in status["reservations"] if not r.get("stale")]
    key_ids = [int(r.get("key_id") or 0) for r in reservations]
    distinct = len(set(key_ids))
    limit = int(status.get("max_cards_per_key") or 1)
    background_id = status.get("background_key_id")
    leaks = [k for k in key_ids if background_id is not None and k == background_id]

    per_key: dict = {}
    for row in reservations:
        per_key.setdefault(int(row.get("key_id") or 0), []).append(row.get("task_id"))
    overfull = {
        kid: tasks for kid, tasks in per_key.items() if len(tasks) > limit
    }
    per_task: dict = {}
    for row in reservations:
        per_task.setdefault(str(row.get("task_id") or ""), set()).add(
            int(row.get("key_id") or 0)
        )
    duplicated = {t: sorted(k) for t, k in per_task.items() if len(k) > 1}

    ok = (
        bool(reservations)
        and not overfull
        and not duplicated
        and not leaks
    )
    if not reservations:
        verdict = "no_active_cards"
    elif leaks:
        verdict = "BACKGROUND_LEAK"
    elif duplicated:
        verdict = "DOUBLE_ASSIGNMENT"
    elif overfull:
        verdict = "OVER_LIMIT"
    else:
        verdict = "ok" if ok else "COLLISION"

    payload = {
        "enabled": status["enabled"],
        "card_count": len(reservations),
        "distinct_keys": distinct,
        "max_cards_per_key": limit,
        "background_key_id": background_id,
        "background_leaked_to_cards": leaks,
        "over_limit_keys": [
            {"key_id": kid, "limit": limit, "task_ids": tasks}
            for kid, tasks in sorted(overfull.items())
        ],
        "tasks_on_multiple_keys": duplicated,
        "verdict": verdict,
        "pairs": [
            {
                "key_id": r.get("key_id"),
                "key_label": r.get("key_label"),
                "task_id": r.get("task_id"),
                "run_id": r.get("run_id"),
                "pid": r.get("pid"),
            }
            for r in reservations
        ],
        "keys": status.get("keys") or [],
    }
    if as_json:
        _print_json(payload)
        return 0 if ok or not reservations else 1

    if not status["enabled"]:
        print("key pool: DISABLED — cards all share the single env key")
        return 1
    if not reservations:
        print("key pool: no card is running right now — nothing to compare")
        print("  start two cards, then re-run `hermes kanban keypool check`")
        return 0
    print(
        f"key pool: {len(reservations)} card(s) on {distinct} key(s), "
        f"limit {limit} card(s) per key"
    )
    for row in payload["pairs"]:
        print(
            f"  key #{row['key_id']} ({row['key_label']}) <- task {row['task_id']} "
            f"run {row['run_id']} pid {row['pid']}"
        )
    if ok:
        print(
            f"VERDICT: ok — {len(reservations)} cards on {distinct} key(s), no key "
            f"above {limit} card(s), background key #{background_id} not handed to "
            f"any card"
        )
        return 0
    print(
        f"VERDICT: {verdict} — {len(reservations)} card(s) on {distinct} key(s), "
        f"limit {limit} per key"
        + (f", over limit on {sorted(overfull)}" if overfull else "")
        + (f", on several keys: {sorted(duplicated)}" if duplicated else "")
        + (f", background leaked: {leaks}" if leaks else ""),
        file=sys.stderr,
    )
    return 1


# ── parser ─────────────────────────────────────────────────────────────────


def build_keypool_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach ``keypool`` under the ``kanban`` subparser tree."""
    parser = parent_subparsers.add_parser(
        "keypool",
        help="Per-card API-key pool: status, rollback, quarantine (mission key-pool-per-card)",
        description=(
            "Inspect and control the per-card API-key pool. Each Kanban card "
            "reserves one key for its whole run; background traffic (chat, cron, "
            "auxiliary) always uses the designated background key and cards can "
            "never receive it. With no key file configured, behaviour is the "
            "plain single-key scheme."
        ),
    )
    sub = parser.add_subparsers(dest="keypool_action")

    p_status = sub.add_parser(
        "status",
        aliases=["show"],
        help="Pool state: enabled?, free/reserved/quarantined keys, this process",
    )
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_keypool)

    p_keys = sub.add_parser("keys", help="List configured key ids/labels/roles (no secrets)")
    p_keys.add_argument("--json", action="store_true")
    p_keys.set_defaults(func=cmd_keypool)

    p_off = sub.add_parser(
        "off",
        help="ROLLBACK: return to the single shared key scheme (one command)",
    )
    p_off.add_argument("--json", action="store_true")
    p_off.set_defaults(func=cmd_keypool)

    p_on = sub.add_parser("on", help="Re-enable the per-card pool after a rollback")
    p_on.add_argument("--json", action="store_true")
    p_on.set_defaults(func=cmd_keypool)

    p_sweep = sub.add_parser(
        "sweep",
        help="Reap reservations whose owner process is gone or whose TTL expired",
    )
    p_sweep.add_argument("--json", action="store_true")
    p_sweep.set_defaults(func=cmd_keypool)

    p_clear = sub.add_parser(
        "clear",
        help="Clear a key quarantine record (after fixing/re-issuing the key)",
    )
    p_clear.add_argument("--key", dest="key_id", default=None,
                         help="Key id to clear; omit to clear every quarantine record")
    p_clear.add_argument("--json", action="store_true")
    p_clear.set_defaults(func=cmd_keypool)

    p_check = sub.add_parser(
        "check",
        help="Prove concurrent cards hold DISTINCT keys (owner acceptance check)",
    )
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_keypool)

    parser.set_defaults(keypool_action="status", func=cmd_keypool, json=False)
    return parser
