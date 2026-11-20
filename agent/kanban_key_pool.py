"""Per-card API-key pool: one reservation per Kanban card, key #1 for background.

Owner requirement (mission ``key-pool-per-card``, cards t_830ffcd0 / t_96a0e866):

* N keys belong to N *different* letovo accounts, all serving the same model.
* A Kanban card reserves **one** key for the whole time it runs and never
  moves off it, not even between requests or while idle.
* A key serves at most ``max_cards_per_key`` cards **at the same time**
  (default 2, configurable in the key file). A slot is released when the card
  ends; the key itself is not bound to those cards forever.
* Key #1 is the **background** key: interactive chat, cron, auxiliary and any
  other traffic that is not a card. Cards must never receive it, even when
  card keys are exhausted — that is enforced here in code, not by convention.
* No keys configured (or fewer than two card keys) → behave exactly as before:
  a single ``LETOVO_LLM_API_KEY`` for everyone. No crash, no behaviour change.
* A dead key (401/403/429) is *not* silently swapped mid-run. The card keeps
  its key, the key is marked bad for that task, and the run fails loudly. The
  next run of the same card may be handed a different key because that is a
  new run, not a mid-run hop.

Design notes
------------
The whole mechanism hangs off ``hermes_cli.runtime_provider.resolve_runtime_provider``
— the single chokepoint every runtime path (CLI chat, oneshot worker, gateway,
delegation) already goes through (see research/F1 §2.1). The wrapper overrides
``api_key`` there, which means the main client, auxiliary clients (they inherit
``agent.api_key`` through ``set_runtime_main``) and delegated children all get
the same reserved value without touching any of those call sites.

Reservation state lives in a JSON file next to the key file and is mutated
only under an exclusive cross-process lock (``msvcrt`` on Windows, ``fcntl``
elsewhere) so two workers starting in the same second cannot take one key.

Liveness is decided by three independent signals, in this order:

1. explicit release (``kanban_complete`` / ``kanban_block`` / process exit);
2. PID liveness — a reservation whose owner process is gone is stale;
3. TTL backstop for PID reuse and for a worker killed with a recycled PID.

Signal (2)/(3) are reaped lazily on the next reservation attempt, so no daemon
or background thread is required and the state survives a gateway restart.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "KeyPoolError",
    "KeyPoolConfig",
    "Reservation",
    "KeyBinding",
    "key_pool_path",
    "state_path",
    "disabled_marker_path",
    "is_enabled",
    "load_config",
    "reserve_for_card",
    "release_for_task",
    "release_for_pid",
    "background_key",
    "apply_card_key",
    "mark_current_key_failed",
    "current_binding",
    "active_card_binding",
    "is_card_process",
    "pool_status",
    "set_enabled",
    "clear_quarantine",
    "sweep",
    "reset_process_cache",
]

# ── tunables ────────────────────────────────────────────────────────────────
#: How long a reservation stays valid without a release, on top of the card's
#: own runtime budget. Kanban's default ``max_runtime_seconds`` is 3h; the
#: slack covers a slow teardown, not a long-running card (that would be a
#: dispatcher timeout, not a key problem).
_TTL_SLACK_SECONDS = 1800
_DEFAULT_TTL_SECONDS = 3 * 3600 + _TTL_SLACK_SECONDS
#: Quarantine window for a key that failed with 429 (rate limited, not dead).
#: 15 minutes matches the documented letovo/DeepSeek limit window; a 401/403
#: gets no expiry at all — a revoked key must not quietly come back.
_DEFAULT_QUARANTINE_SECONDS = 900
#: Waiting this long for the state lock is already pathological — every
#: critical section is a read + a small JSON write (single-digit ms).
_LOCK_TIMEOUT_SECONDS = 20.0
#: How many cards ONE key may serve **at the same time**. The owner chose the
#: slot variant: the slot is released when the card ends, the key is not bound
#: to those two cards forever. 1 reproduces the pre-slot exclusive behaviour
#: exactly (proven by running the legacy suite under that value).
_DEFAULT_MAX_CARDS_PER_KEY = 2
#: Environment overrides. ``HERMES_KANBAN_KEYPOOL`` accepts ``off`` (hard
#: rollback to the single-key scheme) or a path to an alternative key file
#: (tests, and the owner verifying a dry run).
ENV_KEYPOOL = "HERMES_KANBAN_KEYPOOL"
ENV_KEYPOOL_STATE = "HERMES_KANBAN_KEYPOOL_STATE"
#: Operator override for the per-key slot limit, for when the key file cannot
#: be edited (a card's worker already started) or when the owner wants the
#: exclusive scheme back without touching the file. The key file's
#: ``max_cards_per_key`` is the declared value; this env var wins over it.
ENV_MAX_CARDS_PER_KEY = "HERMES_KANBAN_KEYPOOL_MAX_CARDS_PER_KEY"

_ROLE_BACKGROUND = "background"
_ROLE_CARD = "card"
_VALID_ROLES = frozenset({_ROLE_BACKGROUND, _ROLE_CARD})
_STATUS_ACTIVE = "active"
_STATUS_BAD = "bad"

#: Per-process memo of this worker's binding so repeated resolutions inside
#: one process (main + aux + delegated children in-process) are O(1) and can
#: never disagree with each other.
_binding_lock = threading.Lock()
_binding: Optional["KeyBinding"] = None
#: Set only after a reservation SUCCEEDED (or after a failure that must stay
#: sticky). Never set before the attempt: a failed attempt must not turn the
#: next resolution into a silent fallback to the shared key.
_binding_attempted = False
#: The error of a failed attempt. Kept so every later resolution in the same
#: process fails the same way instead of quietly working off the shared key.
_binding_error: Optional["KeyPoolError"] = None

_file_lock_holder = threading.local()


class KeyPoolError(RuntimeError):
    """A key-pool operation failed in a way the caller must handle."""


# ── paths ───────────────────────────────────────────────────────────────────


def _hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def key_pool_path() -> Path:
    """Return the key file path (``$HERMES_KANBAN_KEYPOOL`` wins when set)."""
    override = (os.environ.get(ENV_KEYPOOL) or "").strip()
    if override and override.lower() not in {"off", "0", "false", "no"}:
        return Path(override)
    return _hermes_root() / "kanban-keypool.json"


def state_path() -> Optional[Path]:
    """Return the reservation-state path.

    Follows ``HERMES_KANBAN_KEYPOOL_STATE`` when set, otherwise sits next to
    the key file so a test/instance with its own key file also gets its own
    state instead of mutating the production one.
    """
    override = (os.environ.get(ENV_KEYPOOL_STATE) or "").strip()
    if override:
        return Path(override)
    key_file = key_pool_path()
    return key_file.parent / f"{key_file.stem}.state.json"


def disabled_marker_path() -> Path:
    return key_pool_path().parent / f"{key_pool_path().stem}.disabled"


def _env_says_off() -> bool:
    val = (os.environ.get(ENV_KEYPOOL) or "").strip().lower()
    return val in {"off", "0", "false", "no"}


def is_enabled() -> bool:
    """Cheap gate: is the per-card pool actually in force right now?

    Ordered by precedence: env ``off`` → ``<keypool>.disabled`` sentinel →
    ``enabled: false`` in the key file → a key file that cannot distribute keys
    (no card keys / fewer than two keys / no endpoint). Absent key file means
    "not configured yet", which is also disabled — that is the zero-configuration
    guarantee. ``pool_status()`` reports the precise reason under
    ``inactive_reason`` so a misconfigured file is visible, not silent.
    """
    if _env_says_off():
        return False
    path = key_pool_path()
    try:
        if disabled_marker_path().exists():
            return False
    except OSError:
        return False
    if not path.exists():
        return False
    cfg = load_config()
    return cfg is not None and cfg.usable


def set_enabled(enabled: bool) -> Path:
    """Owner rollback switch — one call, effective on the next process start.

    ``hermes kanban keypool off`` → writes the disabled sentinel.
    ``hermes kanban keypool on`` → removes it.

    A worker that is already running keeps the key it reserved (the whole
    point of a reservation), so the rollback applies to newly started work.
    """
    marker = disabled_marker_path()
    if enabled:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - filesystem dependent
            raise KeyPoolError(f"could not remove {marker}: {exc}") from exc
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {"disabled_at": int(time.time()), "by": "hermes kanban keypool off"}
            ),
            encoding="utf-8",
        )
    return marker


# ── config model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyPoolConfig:
    """Parsed, validated view of the key file."""

    base_url: str
    provider: str
    background_key_id: int
    enabled: bool
    keys: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    #: How many cards one key serves at the same time (the slot limit).
    #: ``1`` reproduces the old exclusive scheme; the default is 2.
    max_cards_per_key: int = _DEFAULT_MAX_CARDS_PER_KEY

    @property
    def card_key_ids(self) -> List[int]:
        return sorted(
            kid
            for kid, entry in self.keys.items()
            if entry.get("role") == _ROLE_CARD
            and not entry.get("excluded_from_cards")
        )

    @property
    def excluded_card_key_ids(self) -> List[int]:
        """Card-role keys the owner marked as NOT for cards.

        Kept separate from :attr:`card_key_ids` on purpose: the exclusion must
        not also remove them from the file, the status view has to be able to
        show *why* a card key is missing from distribution.
        """
        return sorted(
            kid
            for kid, entry in self.keys.items()
            if entry.get("role") == _ROLE_CARD
            and entry.get("excluded_from_cards")
        )

    @property
    def inactive_reason(self) -> Optional[str]:
        """Why this file does not put the pool in force, or ``None`` if it does.

        Two conditions are *structural* rather than a matter of taste, and each
        one silently breaks one of the owner's hard requirements if the pool is
        allowed to run without it:

        * **no card keys** (or fewer than two keys in total) — there is nothing
          to distribute, so the honest behaviour is the plain single-key scheme
          (mission requirement 5). Running instead would fail every card with
          "no free card key", which is not "behaviour identical to today".
        * **empty ``base_url``** — the endpoint guard in :func:`apply_card_key`
          compares the runtime URL against ``cfg.base_url``; with an empty value
          that comparison cannot be made, and a card key would be handed to
          whatever host the runtime happens to point at (another provider, a
          fallback endpoint). A pool key is a secret for ONE host, so a key
          file that does not name that host is unusable.
        """
        if not self.enabled:
            return "config_disabled"
        if len(self.keys) < 2 or not self.card_key_ids:
            return "no_card_keys"
        if not self.base_url.strip():
            return "no_base_url"
        return None

    @property
    def usable(self) -> bool:
        return self.inactive_reason is None

    def secret(self, key_id: int) -> str:
        return str(self.keys.get(int(key_id), {}).get("api_key") or "")

    def label(self, key_id: int) -> str:
        return str(self.keys.get(int(key_id), {}).get("label") or f"key-{key_id}")


def _normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def load_config() -> Optional[KeyPoolConfig]:
    """Parse the key file, or return ``None`` when absent/unusable.

    Never raises on a malformed file: a broken pool must degrade to the
    pre-existing single-key behaviour, not brick every worker. The reason is
    logged once per process.
    """
    path = key_pool_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn_once(f"key pool file {path} is unreadable ({exc}); ignoring it")
        return None
    if not isinstance(raw, dict):
        _warn_once(f"key pool file {path} is not a JSON object; ignoring it")
        return None

    enabled = bool(raw.get("enabled", True))
    base_url = str(raw.get("base_url") or "").strip()
    provider = str(raw.get("provider") or "letovo").strip()

    try:
        background_id = int(raw.get("background_key_id") or 1)
    except (TypeError, ValueError):
        _warn_once(f"key pool {path}: background_key_id is not an integer")
        return None

    max_cards_per_key = _parse_max_cards_per_key(raw.get("max_cards_per_key"), path)
    if max_cards_per_key is None:
        return None

    keys: Dict[int, Dict[str, Any]] = {}
    raw_keys = raw.get("keys")
    if not isinstance(raw_keys, list):
        _warn_once(f"key pool {path}: 'keys' must be a list")
        return None
    for index, item in enumerate(raw_keys):
        if not isinstance(item, dict):
            _warn_once(f"key pool {path}: keys[{index}] is not an object")
            return None
        try:
            kid = int(item.get("id"))
        except (TypeError, ValueError):
            _warn_once(f"key pool {path}: keys[{index}] has no integer id")
            return None
        if kid in keys:
            _warn_once(f"key pool {path}: duplicate key id {kid}")
            return None
        role = str(item.get("role") or "").strip().lower()
        if role not in _VALID_ROLES:
            _warn_once(
                f"key pool {path}: keys[{index}] role {role!r} must be "
                f"one of {sorted(_VALID_ROLES)}"
            )
            return None
        if not str(item.get("api_key") or "").strip():
            _warn_once(f"key pool {path}: key id {kid} has an empty api_key")
            return None
        keys[kid] = {
            "id": kid,
            "role": role,
            "label": str(item.get("label") or f"key-{kid}"),
            "api_key": str(item["api_key"]).strip(),
            # ``excluded_from_cards`` is the owner's reservation for a key that
            # belongs to auxiliary/background work (e.g. the compression key).
            # Before this flag was read, such a key was handed to cards by the
            # ordinary free-slot scan, so compression and a card could fight
            # over one account. It only blocks NEW hand-outs: a card already
            # holding the key keeps it (see ``reserve_for_card`` reuse branch).
            "excluded_from_cards": bool(item.get("excluded_from_cards")),
        }

    if background_id not in keys:
        _warn_once(
            f"key pool {path}: background_key_id {background_id} is not in keys"
        )
        return None
    if keys[background_id]["role"] != _ROLE_BACKGROUND:
        _warn_once(
            f"key pool {path}: key id {background_id} must have role "
            f"{_ROLE_BACKGROUND!r} to serve as the background key"
        )
        return None
    for kid, entry in keys.items():
        if kid != background_id and entry["role"] == _ROLE_BACKGROUND:
            _warn_once(
                f"key pool {path}: only one background key is allowed "
                f"(id {background_id}); id {kid} also declares it"
            )
            return None

    return KeyPoolConfig(
        base_url=base_url,
        provider=provider,
        background_key_id=background_id,
        enabled=enabled,
        keys=keys,
        max_cards_per_key=max_cards_per_key,
    )


def _parse_max_cards_per_key(raw_value: Any, path: Path) -> Optional[int]:
    """Resolve the per-key slot limit: env override → file → default.

    Returns ``None`` when a *declared* value is unusable, which makes the caller
    reject the whole key file (an ambiguous limit is worse than a broken pool:
    it would put more cards on one account than the owner allowed). An absent
    field is not an error — it means the default.
    """
    default = _DEFAULT_MAX_CARDS_PER_KEY
    file_value = raw_value
    if file_value is not None:
        if isinstance(file_value, bool) or not isinstance(file_value, int):
            try:
                file_value = int(str(file_value).strip())
            except (TypeError, ValueError):
                _warn_once(
                    f"key pool {path}: max_cards_per_key {raw_value!r} is not an integer"
                )
                return None
    env_value = (os.environ.get(ENV_MAX_CARDS_PER_KEY) or "").strip()
    if env_value:
        try:
            env_value_int = int(env_value)
        except ValueError:
            _warn_once(
                f"key pool: {ENV_MAX_CARDS_PER_KEY}={env_value!r} is not an integer; "
                f"using the file value"
            )
            env_value_int = None
        if env_value_int is not None:
            if env_value_int < 1:
                _warn_once(
                    f"key pool: {ENV_MAX_CARDS_PER_KEY}={env_value_int} < 1; "
                    f"forcing 1"
                )
                return 1
            return env_value_int
    if file_value is None:
        return default
    if file_value < 1:
        _warn_once(
            f"key pool {path}: max_cards_per_key {file_value} < 1; forcing 1"
        )
        return 1
    return file_value


_warned: set = set()


def _warn_once(message: str) -> None:
    if message in _warned:
        return
    _warned.add(message)
    logger.warning("key-pool: %s", message)
    # Also to stderr: worker logs are the only surface an operator reads for a
    # headless card, and a silently ignored pool is exactly the failure mode
    # that looks like "the feature did nothing".
    try:
        print(f"[key-pool] {message}", file=sys.stderr)
    except Exception:  # pragma: no cover - closed stderr
        pass


# ── cross-process lock ──────────────────────────────────────────────────────

try:  # POSIX
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

try:  # Windows
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    """Exclusive cross-process lock around one read-modify-write of state.

    Reentrant per thread, same contract as ``hermes_cli.auth._file_lock`` —
    a nested reservation attempt inside one process must not deadlock on a
    lock its own thread already holds.
    """
    if getattr(_file_lock_holder, "depth", 0) > 0:
        _file_lock_holder.depth += 1
        try:
            yield
        finally:
            _file_lock_holder.depth -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    if fcntl is None and msvcrt is None:  # pragma: no cover - exotic host
        _file_lock_holder.depth = 1
        try:
            yield
        finally:
            _file_lock_holder.depth = 0
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    mode = "r+" if msvcrt else "a+"
    with lock_path.open(mode, encoding="utf-8") as handle:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if fcntl:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError, PermissionError) as exc:
                if time.monotonic() >= deadline:
                    raise KeyPoolError(
                        f"timed out after {_LOCK_TIMEOUT_SECONDS:.0f}s waiting for "
                        f"the key-pool state lock at {lock_path}"
                    ) from exc
                time.sleep(0.02)
        _file_lock_holder.depth = 1
        try:
            yield
        finally:
            _file_lock_holder.depth = 0
            try:
                if fcntl:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):  # pragma: no cover
                pass


# ── state persistence ──────────────────────────────────────────────────────


def _read_state(path: Path) -> Dict[str, Any]:
    """Read the state file and fold it into the *slot* form.

    Two on-disk shapes are readable, and the reader never loses a reservation:

    * **legacy (v1)** — ``reservations[str(key_id)] = {task_id, pid, ...}``, one
      flat record per key, i.e. the key was exclusive. It is exactly one slot,
      so it is read as a one-element ``slots`` list.
    * **slots (v2)** — ``reservations[str(key_id)] = {key_id, key_label, slots:
      [{task_id, pid, ...}, ...]}``.

    Writing is deliberately backward compatible too (see :func:`_write_state`):
    a key holding one card is still written as the flat v1 record. A worker
    started by the previous build during a rolling restart therefore keeps
    reading the file, and a rollback to the exclusive scheme finds the state
    intact instead of every card losing its key at once.
    """
    empty = {"version": 2, "reservations": {}, "failed_keys": {}, "rr_cursor": None}
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A torn state file must not block new work: the keys themselves live
        # in a different file and every reservation is recoverable.
        return dict(empty)
    if not isinstance(raw, dict):
        return dict(empty)
    stored = raw.get("reservations")
    if not isinstance(stored, dict):
        stored = {}
    reservations: Dict[str, Any] = {}
    for key, entry in stored.items():
        folded = _fold_reservation(key, entry)
        if folded is not None:
            reservations[str(key)] = folded
    failed_keys = raw.get("failed_keys")
    if not isinstance(failed_keys, dict):
        failed_keys = {}
    # ``rr_cursor`` is the rotation cursor: the id of the last key handed out.
    # It lives here, in the same file as ``reservations`` and ``failed_keys``,
    # so a restarted process continues the same ring instead of starting over
    # at the lowest id (a second state file would drift out of sync with this
    # one on any partial write). Absent in a v1 file: ``None`` means "no turn
    # taken yet" and the ring starts at the lowest free id.
    cursor_raw = raw.get("rr_cursor")
    try:
        rr_cursor: Optional[int] = None if cursor_raw is None else int(cursor_raw)
    except (TypeError, ValueError):
        rr_cursor = None
    return {
        "version": 2,
        "reservations": reservations,
        "failed_keys": failed_keys,
        "rr_cursor": rr_cursor,
    }


def _fold_reservation(key: Any, entry: Any) -> Optional[Dict[str, Any]]:
    """Normalise one stored key record to ``{key_id, key_label, slots}``."""
    if not isinstance(entry, dict):
        return None
    try:
        key_id = int(entry.get("key_id") or key)
    except (TypeError, ValueError):
        return None
    label = str(entry.get("key_label") or f"key-{key_id}")
    raw_slots = entry.get("slots")
    if isinstance(raw_slots, list):
        slots = [dict(s) for s in raw_slots if isinstance(s, dict)]
    else:
        # Legacy flat record: every field except the key's own two belongs to
        # the single slot the key used to hold exclusively.
        slots = [
            {k: v for k, v in entry.items() if k not in ("key_id", "key_label")}
        ]
    return {"key_id": key_id, "key_label": label, "slots": slots}


def _key_slots(entry: Any) -> List[Dict[str, Any]]:
    """The live slots of one key record (empty for anything unusable)."""
    if not isinstance(entry, dict):
        return []
    slots = entry.get("slots")
    if not isinstance(slots, list):
        return []
    return [s for s in slots if isinstance(s, dict)]


def _serialize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Render the in-memory slot state for disk, keeping v1 readable.

    One slot → the flat v1 record (a previous build can still read it, and so
    can every test that inspects the file directly). Two or more slots → the
    v2 ``slots`` list. A key with no slots is dropped entirely instead of being
    written as a husk.
    """
    out_reservations: Dict[str, Any] = {}
    for key, entry in (state.get("reservations") or {}).items():
        if not isinstance(entry, dict):
            continue
        slots = _key_slots(entry)
        if not slots:
            continue
        key_id = entry.get("key_id")
        label = entry.get("key_label") or f"key-{key_id}"
        if len(slots) == 1:
            flat = dict(slots[0])
            flat["key_id"] = key_id
            flat["key_label"] = label
            out_reservations[str(key)] = flat
        else:
            out_reservations[str(key)] = {
                "key_id": key_id,
                "key_label": label,
                "slots": [dict(s) for s in slots],
            }
    return {
        "version": 2,
        "reservations": out_reservations,
        "failed_keys": dict(state.get("failed_keys") or {}),
        "rr_cursor": state.get("rr_cursor"),
    }


def _pick_key(
    free: List[int],
    occupied: Dict[int, List[Dict[str, Any]]],
    last_key_id: Optional[int],
) -> int:
    """Choose the next card key: least loaded first, round-robin to break ties.

    Two rules, in this order — the owner asked for both at once (card K4-Р,
    2026-11-08: «чтобы ключи постоянно ротировались, а не всегда
    использовались первые 4»):

    1. **Least loaded.** The key with the fewest occupied slots wins. That is
       what spreads two cards of a two-slot key onto two *different* keys
       while there are free keys, instead of filling #2's second slot first.
    2. **Round-robin.** Among keys with the same load, prefer the first one
       *after* ``last_key_id`` in ascending id order, wrapping at the end.
       That is what keeps successive cards moving (#2 → #3 → #4 …) instead of
       hammering the lowest id, and it brings the ring back to a key that has
       emptied out again.

    Leaving this as ``free[0]`` would reintroduce the reported defect: with
    2 slots and 9 keys every card piles onto #2 before #3 is ever touched.
    """
    least = min(len(occupied.get(kid) or []) for kid in free)
    candidates = [kid for kid in free if len(occupied.get(kid) or []) == least]
    if last_key_id is None:
        return candidates[0]
    # First candidate strictly after the cursor, wrapping around the ring.
    after = [kid for kid in candidates if kid > last_key_id]
    return after[0] if after else candidates[0]


def _write_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomic replace so a crash never leaves half a reservation behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_serialize_state(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:  # keys and state are both sensitive-ish; keep them owner-only
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs
        pass


# ── PID liveness ───────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness for another process.

    On Windows ``os.kill(pid, 0)`` raises for a live process too, so psutil is
    the reliable probe (already a hard dependency). It is imported lazily so
    the pure-stdlib test paths and the fast no-pool path never pay for it.
    """
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:  # pragma: no cover - psutil unavailable
        pass
    if os.name == "nt":  # pragma: no cover - Windows without psutil
        return True  # cannot prove it is dead → keep the reservation
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _reservation_is_stale(entry: Dict[str, Any], now: float) -> Optional[str]:
    """Return a short staleness reason, or ``None`` when still valid.

    Order matters, and it is the fix for the TTL defect found in review: **a
    live owner is never stale**. The lease (``expires_at``) exists to reclaim a
    key whose owner vanished without releasing — it is a backstop for a dead
    worker, not a budget that revokes a working card's key mid-run.

    Checking ``expires_at`` first made TTL cover PID reuse, but it also meant a
    genuinely busy card lost its reservation after ``max_runtime + 30min`` (and
    3.5h when the card had no budget at all) while it was still sending
    requests: the next card was handed the SAME key, so two live runs shared
    one account — the exact guarantee this mission exists to provide. So:

    * owner (or any attached sibling) alive → not stale, regardless of TTL.
      ``apply_card_key`` renews the lease on every resolution, so an expired
      lease on a live process is just "no renewal happened yet".
    * nobody alive → stale on PID grounds (``pid_gone``).
    * no usable pid recorded at all → the TTL is the only evidence left, so it
      decides (`ttl_expired`).
    """
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    try:
        expires_at = float(entry.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0

    if pid and _pid_alive(pid):
        return None
    # The reserving worker is gone, but a sibling process of the SAME card may
    # still be running on this key (an attached helper / in-flight subprocess).
    # The reservation is live while any of them is still alive.
    attached = entry.get("attached_pids") or []
    if isinstance(attached, list):
        for extra in attached:
            try:
                extra_pid = int(extra)
            except (TypeError, ValueError):
                continue
            if extra_pid and _pid_alive(extra_pid):
                return None
    if pid:
        return "pid_gone"
    if expires_at and now >= expires_at:
        return "ttl_expired"
    return None


def _sweep_locked(state: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """Drop stale slots in-place; return what was dropped.

    Staleness is a property of a SLOT, not of a key: one card on a key can die
    while the other keeps working, and reaping the key for the dead one would
    cancel a live card's reservation — the same defect as releasing the whole
    key on one card's completion (requirement 4).

    A slot marked ``bad`` is *kept* until its owner is gone — it is the audit
    record for "this task was served a key that the provider rejected", and it
    must keep that key away from the same task on an immediate retry. The key
    itself is quarantined separately in ``failed_keys``.
    """
    dropped: List[Dict[str, Any]] = []
    reservations: Dict[str, Any] = state["reservations"]
    for key in list(reservations.keys()):
        entry = reservations.get(key)
        if not isinstance(entry, dict):
            reservations.pop(key, None)
            continue
        slots = _key_slots(entry)
        if not slots:  # a husk: no card, no record
            reservations.pop(key, None)
            continue
        key_id = entry.get("key_id")
        survivors: List[Dict[str, Any]] = []
        for slot in slots:
            reason = _reservation_is_stale(slot, now)
            if reason:
                record = dict(slot)
                record["key_id"] = key_id
                record["key_label"] = entry.get("key_label")
                record["released_reason"] = reason
                record["released_at"] = int(now)
                dropped.append(record)
                continue
            survivors.append(slot)
        if survivors:
            entry["slots"] = survivors
        else:
            reservations.pop(key, None)
    return dropped


# ── reservation model ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reservation:
    key_id: int
    label: str
    task_id: str
    run_id: Optional[int]
    pid: int
    expires_at: int
    api_key: str = field(repr=False, default="")
    status: str = _STATUS_ACTIVE
    reused: bool = False

    def to_audit(self) -> Dict[str, Any]:
        """Secret-free view, safe for logs, Kanban metadata and reports."""
        return {
            "key_id": self.key_id,
            "label": self.label,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "pid": self.pid,
            "expires_at": self.expires_at,
            "status": self.status,
            "reused": self.reused,
        }


@dataclass(frozen=True)
class KeyBinding:
    """What this process is currently pinned to."""

    key_id: int
    label: str
    role: str
    api_key: str = field(repr=False, default="")
    task_id: str = ""
    run_id: Optional[int] = None
    pid: int = 0
    status: str = _STATUS_ACTIVE
    failure: Optional[Dict[str, Any]] = None

    @property
    def usable(self) -> bool:
        return self.status == _STATUS_ACTIVE and bool(self.api_key)

    def to_audit(self) -> Dict[str, Any]:
        return {
            "key_id": self.key_id,
            "label": self.label,
            "role": self.role,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "pid": self.pid,
            "status": self.status,
            "failure": self.failure,
        }


def _ttl_seconds() -> float:
    raw = (os.environ.get("HERMES_KANBAN_MAX_RUNTIME_SECONDS") or "").strip()
    try:
        budget = float(raw)
    except (TypeError, ValueError):
        budget = 0.0
    if budget <= 0:
        return float(_DEFAULT_TTL_SECONDS)
    return budget + _TTL_SLACK_SECONDS


def _quarantine_seconds() -> float:
    """Cooldown for a rate-limited (not dead) key; see ``mark_key_failed``."""
    raw = (os.environ.get("HERMES_KANBAN_KEYPOOL_QUARANTINE_SECONDS") or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(_DEFAULT_QUARANTINE_SECONDS)
    return value if value > 0 else float(_DEFAULT_QUARANTINE_SECONDS)


def _worker_identity() -> Tuple[str, Optional[int]]:
    """The raw dispatcher vars, WITHOUT deciding whether they mean anything.

    Callers must go through :func:`_card_identity`; this tuple on its own is
    exactly the assumption the N1 defect rested on.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    try:
        run_id: Optional[int] = int(raw_run) if raw_run else None
    except ValueError:
        run_id = None
    return task_id, run_id


#: Raised when a dispatcher-envelope worker's own identity cannot be decided.
#: Never degraded to "background": that would hand the card the shared
#: ``.env`` key, the one outcome the owner forbade.
_IDENTITY_PROBE_UNAVAILABLE = (
    "agent.kanban_key_pool cannot tell whether this dispatcher worker owns the "
    "card: agent.delegation_context exposes neither "
    "is_non_dispatcher_owned_context() nor its _NON_DISPATCHER_OWNED_CONTEXT "
    "state. Refusing to guess and refusing to hand the card the background key."
)


def _narrow_non_dispatcher_owned() -> bool:
    """True while an in-process cron run has borrowed this worker's envelope.

    This is the *narrow* half of :func:`agent.delegation_context.
    is_dispatcher_owned_worker_context`: it says only "a cron job fired in this
    process is not the card", and deliberately does NOT disqualify a
    ``delegate_task`` child (a child of a card *is* the card's own work and must
    keep the card's key — design ``F2``, «Одна карточка = один ключ»).

    Two trees have to be served, so the probe has two tiers:

    1. ``is_non_dispatcher_owned_context()`` — the named predicate on the pool's
       own branch;
    2. the ``_NON_DISPATCHER_OWNED_CONTEXT`` ``ContextVar`` it is a one-line
       reader of — present in the live tree
       (``release/t_b0291572-owui-origin-binding``), which has the state and the
       ``non_dispatcher_owned_context`` / ``enter_`` / ``exit_`` context
       manager but not the named accessor.

    A third tier is deliberately absent. Composing the answer from
    ``is_dispatcher_owned_worker_context()`` + ``is_delegated_child_context()``
    looks equivalent but inverts on a cron job that *also* delegates (it would
    call that process a card and hand it a card key). If neither tier resolves,
    :class:`KeyPoolError` is raised — silently answering "background" here is
    exactly the K7 defect: every card quietly ran on key #1.

    The function is only reached for a proven dispatcher envelope (see
    :func:`is_card_process`); a background process never calls it.
    """
    try:
        from agent import delegation_context as _dc
    except Exception as exc:  # pragma: no cover - broken install
        raise KeyPoolError(
            f"{_IDENTITY_PROBE_UNAVAILABLE} (import failed: "
            f"{type(exc).__name__}: {exc})"
        ) from exc

    named = getattr(_dc, "is_non_dispatcher_owned_context", None)
    if callable(named):
        return bool(named())

    state = getattr(_dc, "_NON_DISPATCHER_OWNED_CONTEXT", None)
    getter = getattr(state, "get", None)
    if callable(getter):
        return bool(getter())

    raise KeyPoolError(_IDENTITY_PROBE_UNAVAILABLE)


def is_card_process() -> bool:
    """Is THIS execution a dispatcher-owned card worker?

    ``HERMES_KANBAN_TASK`` being set is not evidence of that, and the
    repository says so three times:

    * ``agent/kanban_stop.py`` requires the full dispatcher envelope
      (``HERMES_KANBAN_RUN_ID`` / ``_DB`` / ``_WORKSPACE`` + ``HERMES_PROFILE``
      + ``HERMES_SESSION_SOURCE=kanban``) because a long-lived gateway process
      started inside an old worker context inherits the bare task id and would
      otherwise be treated as that worker (phantom-loop regression
      ``t_6972f101``, ``gateway/session_context.py``).
    * the context half comes from ``agent/delegation_context``: an in-process
      cron run (``cronjob(action="run")``, wrapped by ``cron/scheduler.py`` in
      ``enter_non_dispatcher_owned_context``) inherits the worker's whole env
      envelope but is NOT the worker. See :func:`_narrow_non_dispatcher_owned`.

    The context half is read through that *narrow* predicate, not through
    ``is_dispatcher_owned_worker_context()``: a ``delegate_task`` child of a
    card IS the card's own work. The design files it under «другой процесс той
    же карточки» (adopt the existing reservation, ``attached_pids``), whereas
    ``is_dispatcher_owned_worker_context`` — correctly, for ``kanban_stop`` —
    answers "the child does not own the card". Reading that coarser predicate
    here sent every delegated child to the background key and, worse, lifted
    the card's "never rotate mid-run" guard inside it.

    Both halves are required. The env fence alone misses the cron case; the
    context fence alone would accept a stale env in a chat process, since
    nothing marks a chat process as non-dispatcher-owned.

    Why this matters to the pool specifically — two concrete failures it
    prevents:

    * a chat/background process with a stale task id took a CARD key and held
      it for the whole TTL, which is owner requirement #3 inverted (background
      running on a card account);
    * a cron job inside a worker resolved to the worker's card key, and a 401
      from that unrelated job then quarantined the live card's key via
      ``mark_current_key_failed`` — a stranger burning the card's account.

    Failure directions are asymmetric on purpose:

    * no dispatcher envelope (or ``kanban_stop`` unimportable) → ``False``: the
      process is definitively *not* a worker, and the background key is the
      correct answer;
    * envelope present but the context half unusable → ``KeyPoolError``: the
      process IS a worker of some kind, and the pool must not guess. The old
      ``return False`` here made every card resolve to background key #1
      (defect K7, ``design/K7-live-compat-task.md``).
    """
    try:
        from agent.kanban_stop import is_dispatcher_worker_env
    except Exception:  # pragma: no cover - import plumbing only
        return False
    try:
        if not is_dispatcher_worker_env():
            return False
    except Exception:  # pragma: no cover - defensive
        return False

    # Envelope says "worker". The context half must now be decided for real;
    # its failures propagate as KeyPoolError instead of demoting the card.
    return not _narrow_non_dispatcher_owned()


def _card_identity() -> Tuple[str, Optional[int]]:
    """``(task_id, run_id)`` for a proven card, ``("", None)`` otherwise."""
    if not is_card_process():
        return "", None
    return _worker_identity()


def reserve_for_card(
    task_id: str,
    run_id: Optional[int] = None,
    *,
    pid: Optional[int] = None,
) -> Reservation:
    """Atomically reserve one card key for ``task_id``.

    Idempotent per ``(task_id, pid)``: a second call from the same process
    returns the reservation it already holds instead of taking a second key.
    That is what makes repeated ``resolve_runtime_provider`` calls inside one
    worker (main client, auxiliary clients, delegated children resolved
    out-of-process) agree on a single key.

    Raises ``KeyPoolError`` when the pool is enabled but no card key is free.
    The caller must surface that, never silently borrow the background key.
    """
    cfg = load_config()
    if cfg is None:
        raise KeyPoolError("key pool is not configured")
    reason = cfg.inactive_reason
    if reason is not None:
        # Reaching here means a caller reserved explicitly on a key file that
        # is not in force. Say why instead of half-working: in particular
        # ``no_card_keys`` must never be reported as "no free card key", which
        # would look like contention when the file simply has no card role.
        raise KeyPoolError(
            f"key pool is not in force ({reason}): the key file cannot "
            f"distribute card keys"
        )

    card_ids = cfg.card_key_ids
    if not card_ids:
        raise KeyPoolError("key pool has no card keys (role: 'card')")

    owner_pid = int(pid if pid is not None else os.getpid())
    path = state_path()
    if path is None:  # pragma: no cover - state_path always resolves
        raise KeyPoolError("key pool state path could not be resolved")
    now = time.time()
    ttl = _ttl_seconds()

    with _state_lock(path):
        state = _read_state(path)
        dropped = _sweep_locked(state, now)
        if dropped:
            logger.info(
                "key-pool: reaped %d stale reservation(s): %s",
                len(dropped),
                ", ".join(
                    f"key {d.get('key_id')} task {d.get('task_id')} "
                    f"({d.get('released_reason')})"
                    for d in dropped
                ),
            )
        reservations: Dict[str, Any] = state["reservations"]
        max_per_key = max(1, int(getattr(cfg, "max_cards_per_key", 1) or 1))

        # 1. Reuse, never take a second key. Two scopes:
        #
        #    (a) same process + same task → ours, refresh the lease;
        #    (b) same task + a DIFFERENT live pid → a child/helper process of
        #        the SAME card (a delegated subagent resolved out-of-process, a
        #        `hermes` subprocess, a retried worker whose predecessor is
        #        still winding down). The mission requirement is "one card =
        #        one key for the whole run", so the newcomer must attach to the
        #        card's existing reservation instead of consuming a second
        #        account. Without (b) an 8-card ceiling could burn 16 keys.
        #
        #    Both scopes now search SLOTS: the card may sit in the second slot
        #    of a shared key, and the search must still find it there instead
        #    of handing the same card a second slot on another account.
        #
        #    A stale slot (dead pid / expired lease) was already dropped by
        #    ``_sweep_locked`` above, so anything seen here belongs to live work.
        for key, entry in reservations.items():
            if not isinstance(entry, dict):
                continue
            kid = int(entry.get("key_id") or key)
            for slot in _key_slots(entry):
                if slot.get("task_id") != task_id:
                    continue
                entry_pid = int(slot.get("pid") or 0)
                if entry_pid == owner_pid:
                    slot["expires_at"] = int(now + ttl)  # refresh our own lease
                    _write_state(path, state)
                    return Reservation(
                        key_id=kid,
                        label=cfg.label(kid),
                        task_id=task_id,
                        run_id=slot.get("run_id"),
                        pid=owner_pid,
                        expires_at=int(slot.get("expires_at") or 0),
                        api_key=cfg.secret(kid),
                        status=str(slot.get("status") or _STATUS_ACTIVE),
                        reused=True,
                    )
                if entry_pid and entry_pid != owner_pid:
                    # Sibling process of the same card: adopt the key, keep a
                    # single reservation (the owner pid stays the one that
                    # created it, so the release path and the audit trail do
                    # not fork).
                    slot.setdefault("attached_pids", [])
                    attached = slot.get("attached_pids")
                    if isinstance(attached, list) and owner_pid not in attached:
                        attached.append(owner_pid)
                    slot["expires_at"] = max(
                        int(slot.get("expires_at") or 0), int(now + ttl)
                    )
                    _write_state(path, state)
                    logger.info(
                        "key-pool: pid %s attached to task %s's existing key %s",
                        owner_pid, task_id, kid,
                    )
                    print(
                        f"[key-pool] task {task_id} pid {owner_pid} attached to "
                        f"existing key #{kid} ({cfg.label(kid)}) held by pid "
                        f"{entry_pid}",
                        file=sys.stderr,
                    )
                    return Reservation(
                        key_id=kid,
                        label=cfg.label(kid),
                        task_id=task_id,
                        run_id=slot.get("run_id"),
                        pid=owner_pid,
                        expires_at=int(slot.get("expires_at") or 0),
                        api_key=cfg.secret(kid),
                        status=str(slot.get("status") or _STATUS_ACTIVE),
                        reused=True,
                    )

        # 2. Take the lowest card key that still has a free SLOT. The
        #    background key is not in ``card_ids`` at all, so it cannot be
        #    handed out here under any shortage (requirement 5).
        occupied: Dict[int, List[Dict[str, Any]]] = {}
        for key, entry in reservations.items():
            if not isinstance(entry, dict):
                continue
            kid = int(entry.get("key_id") or key)
            occupied.setdefault(kid, []).extend(_key_slots(entry))
        # A key is "taken" only once ALL of its slots are used — that is the
        # whole point of the change: 7 card keys × 2 slots = 14 places for 8
        # cards, so the eighth card no longer gets a refusal.
        taken = {
            kid for kid, slots in occupied.items() if len(slots) >= max_per_key
        }
        # A key the provider rejected is quarantined: it must not be handed to
        # the next task while the failure reason is still in force (dead key)
        # or its cooldown window has not elapsed (rate limit). Expired
        # quarantine entries are dropped on read so a healed key returns
        # without operator action. Quarantine covers the WHOLE key — a broken
        # credential must not serve a second slot either (requirement 7).
        failed: Dict[str, Any] = state.get("failed_keys") or {}
        for fid in list(failed.keys()):
            record = failed.get(fid)
            if not isinstance(record, dict):
                failed.pop(fid, None)
                continue
            try:
                until = int(record.get("quarantine_until") or 0)
            except (TypeError, ValueError):
                until = 0
            if until and now >= until:
                failed.pop(fid, None)
        state["failed_keys"] = failed
        poisoned = {int(fid) for fid in failed.keys() if str(fid).isdigit()}
        free = [kid for kid in card_ids if kid not in taken and kid not in poisoned]
        if not free:
            # Name the occupancy per key instead of a bare "no free key": the
            # operator must be able to tell "every slot is busy" (#2: 2/2)
            # from "one key is dead" (#3: 0/2 quarantined).
            occupancy = ", ".join(
                f"#{kid}: {len(occupied.get(kid, []))}/{max_per_key}"
                for kid in card_ids
            )
            detail = ""
            if failed:
                detail = " Quarantined: " + ", ".join(
                    f"#{fid} ({rec.get('reason')} @ {rec.get('status_code')})"
                    for fid, rec in sorted(failed.items())
                )
            raise KeyPoolError(
                f"no free card key slot: every card key is full at "
                f"{max_per_key} card(s) per key ({occupancy}).{detail} The "
                f"background key is never handed to a card. Wait for a card to "
                f"finish, add keys, raise max_cards_per_key, or clear a false "
                f"quarantine with `hermes kanban keypool clear`."
            )

        # Rotation, not ``free[0]``: least-loaded key wins, round-robin breaks
        # ties (see :func:`_pick_key`). The cursor is the last key actually
        # handed out and it is persisted in this same state file, so a
        # restarted process keeps turning the same ring.
        key_id = _pick_key(free, occupied, state.get("rr_cursor"))
        expires_at = int(now + ttl)
        entry = reservations.get(str(key_id))
        if not isinstance(entry, dict):
            entry = {
                "key_id": key_id,
                "key_label": cfg.label(key_id),
                "slots": [],
            }
            reservations[str(key_id)] = entry
        entry["slots"] = _key_slots(entry) + [{
            "task_id": task_id,
            "run_id": run_id,
            "pid": owner_pid,
            "host": socket.gethostname(),
            "workspace": (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip(),
            "reserved_at": int(now),
            "expires_at": expires_at,
            "status": _STATUS_ACTIVE,
        }]
        # Move the cursor only on an actual hand-out: the reuse/adopt branches
        # above return early, so an existing card never advances the ring.
        state["rr_cursor"] = key_id
        _write_state(path, state)

    logger.info(
        "key-pool: card %s (run %s, pid %s) reserved key %s (%s)",
        task_id,
        run_id,
        owner_pid,
        key_id,
        cfg.label(key_id),
    )
    # Deliberate, greppable, secret-free audit line — this is the string the
    # owner is told to grep for (report §8).
    print(
        f"[key-pool] reserved key #{key_id} ({cfg.label(key_id)}) "
        f"for task {task_id} run {run_id} pid {owner_pid} "
        f"until {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(expires_at))}",
        file=sys.stderr,
    )
    return Reservation(
        key_id=key_id,
        label=cfg.label(key_id),
        task_id=task_id,
        run_id=run_id,
        pid=owner_pid,
        expires_at=expires_at,
        api_key=cfg.secret(key_id),
        status=_STATUS_ACTIVE,
    )


def renew_for_card(
    task_id: str,
    key_id: int,
    *,
    pid: Optional[int] = None,
) -> bool:
    """Extend the lease on a reservation this process actually holds.

    Called from :func:`apply_card_key` on every resolution (cheap: one locked
    read-modify-write, and only when the entry is really ours). Together with
    the "a live owner is never stale" rule in :func:`_reservation_is_stale`
    this closes the review's TTL defect from both ends: a working card renews
    its lease, and even a card that resolves only once cannot lose its key
    while its process is alive.

    Refuses — returns ``False`` — unless the entry belongs to ``task_id`` AND
    the caller is one of its processes. A renewal is the one mutation that
    makes a foreign reservation live longer, so it must never be done on
    someone else's behalf.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return False
    caller_pid = int(pid if pid is not None else os.getpid())
    path = state_path()
    if path is None or not path.exists():
        return False
    now = time.time()
    with _state_lock(path):
        state = _read_state(path)
        entry = state["reservations"].get(str(int(key_id)))
        if not isinstance(entry, dict):
            return False
        for slot in _key_slots(entry):
            if slot.get("task_id") != task_id:
                continue
            owner_pid = int(slot.get("pid") or 0)
            attached = slot.get("attached_pids") or []
            is_mine = caller_pid == owner_pid or (
                isinstance(attached, list) and caller_pid in attached
            )
            if not is_mine:
                continue
            # Extend THIS slot only. Renewing the whole key would keep a second
            # card's lease alive on behalf of a process that is not using it.
            slot["expires_at"] = int(now + _ttl_seconds())
            slot["renewed_at"] = int(now)
            _write_state(path, state)
            return True
    return False


def release_for_task(
    task_id: str, *, pid: Optional[int] = None, defer_if_holder: bool = True
) -> List[Dict[str, Any]]:
    """Release the reservation belonging to ``task_id``.

    The reservation is scoped to the CARD, not to the process that happened to
    create it: on a terminal transition (``kanban_complete`` / ``kanban_block``
    / a human closing or blocking the card from the CLI) the key must go back
    to the pool no matter which process of that card — or which operator
    profile — performed the transition. Naming the task explicitly is what
    keeps this safe: a stale worker of a *different* task can never free
    someone else's key, because its ``HERMES_KANBAN_TASK`` does not match.

    ``defer_if_holder`` (default on): when THIS process is the card's current
    key holder, the reservation is *not* dropped here. A kanban worker keeps
    running for one more model turn after ``kanban_complete`` (the "here is my
    final summary" message) and that turn must still go out on the card's own
    key. Freeing the key at completion time would hand the same account to
    another card while this process is still using it. The release then
    happens on process exit (``atexit``) or by the PID/TTL backstop, which is
    immediate in practice — a worker exits right after its final turn.

    ``pid`` is accepted for call-site compatibility and only recorded in the
    log line; it does not gate the release.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return []
    caller_pid = int(pid if pid is not None else os.getpid())
    if defer_if_holder:
        holder = current_binding()
        if (
            holder is not None
            and holder.role == _ROLE_CARD
            and holder.task_id == task_id
            and int(holder.pid or 0) == caller_pid
        ):
            logger.debug(
                "key-pool: task %s completed by its own key holder %s — "
                "release deferred to process exit",
                task_id,
                caller_pid,
            )
            return []
    path = state_path()
    if path is None or not path.exists():
        return []
    now = time.time()
    released: List[Dict[str, Any]] = []
    with _state_lock(path):
        state = _read_state(path)
        reservations: Dict[str, Any] = state["reservations"]
        changed = False
        for key in list(reservations.keys()):
            entry = reservations.get(key)
            if not isinstance(entry, dict):
                reservations.pop(key, None)
                changed = True
                continue
            key_id = entry.get("key_id")
            kept: List[Dict[str, Any]] = []
            for slot in _key_slots(entry):
                if slot.get("task_id") == task_id:
                    # Requirement 4: release EXACTLY the slots of this card.
                    # The other card's slot on the SAME key stays untouched —
                    # dropping the whole key record here would cancel a live
                    # card's reservation.
                    record = dict(slot)
                    record["key_id"] = key_id
                    record["key_label"] = entry.get("key_label")
                    record["released_reason"] = "released"
                    record["released_at"] = int(now)
                    record["released_by_pid"] = caller_pid
                    released.append(record)
                    changed = True
                    continue
                kept.append(slot)
            if kept:
                entry["slots"] = kept
            else:
                reservations.pop(key, None)
        if changed:
            _write_state(path, state)
    for record in released:
        logger.info(
            "key-pool: released key %s from task %s (pid %s)",
            record.get("key_id"),
            task_id,
            caller_pid,
        )
    return released


def release_for_pid(pid: Optional[int] = None) -> List[Dict[str, Any]]:
    """Release everything this process holds — the atexit safety net."""
    owner_pid = int(pid if pid is not None else os.getpid())
    path = state_path()
    if path is None or not path.exists():
        return []
    now = time.time()
    released: List[Dict[str, Any]] = []
    with _state_lock(path):
        state = _read_state(path)
        reservations: Dict[str, Any] = state["reservations"]
        changed = False
        for key in list(reservations.keys()):
            entry = reservations.get(key)
            if not isinstance(entry, dict):
                reservations.pop(key, None)
                changed = True
                continue
            key_id = entry.get("key_id")
            kept: List[Dict[str, Any]] = []
            for slot in _key_slots(entry):
                slot_pid = int(slot.get("pid") or 0)
                if slot_pid == owner_pid:
                    record = dict(slot)
                    record["key_id"] = key_id
                    record["key_label"] = entry.get("key_label")
                    record["released_reason"] = "process_exit"
                    record["released_at"] = int(now)
                    released.append(record)
                    changed = True
                    continue
                # A sibling process of the same card holds no slot of its own —
                # it attached to the card's slot. On exit it just detaches; the
                # slot survives until its real owner is gone (or its TTL
                # expires), so a helper exiting cannot free a key the card is
                # still using.
                attached = slot.get("attached_pids")
                if isinstance(attached, list) and owner_pid in attached:
                    slot["attached_pids"] = [p for p in attached if p != owner_pid]
                    changed = True
                kept.append(slot)
            if kept:
                entry["slots"] = kept
            else:
                reservations.pop(key, None)
        if changed:
            _write_state(path, state)
    return released


def mark_key_failed(
    task_id: str,
    status_code: Optional[int],
    *,
    reason: str = "",
    pid: Optional[int] = None,
    key_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Record that the provider rejected this card's own key, then quarantine it.

    The card does **not** get a different key from here — that would be the
    silent mid-run hop the owner forbade. What happens instead:

    * the failure is written to the state file's ``failed_keys`` map, which is
      the audit trail (§4 of the report) and survives restarts;
    * the card's OWN slot is kept, flipped to ``bad`` (defect D1: deleting it
      freed the key for a second card while the first was still running on it,
      which at the production limit of one card per key inverted the
      "one card = one key" invariant). A ``bad`` slot still counts against the
      key's capacity, so the key stays unavailable until the card releases it
      (``release_for_task`` / ``release_for_pid``) or the PID/TTL backstop
      reaps it — and a *quarantine* keeps it out of the next reservation for
      new cards until it expires (or forever, for a dead 401/403 key);
    * this process's binding flips to ``bad``, so every further resolution in
      it raises instead of quietly working off some other key.

    **Who is allowed to report.** Any process that actually holds the key: the
    reserving owner (``pid == entry.pid``) or a sibling of the same card
    (``pid in entry.attached_pids``). The check is on the *pair* ``(task_id,
    pid)``, not on the pid alone, so a stale worker of a different card cannot
    poison someone else's key. Restricting it to the owner pid (the original
    implementation) meant an adopted helper — the process that may well be the
    one that saw the 401 — could not quarantine a key it was using, and the
    key came back for the next card. ``key_id`` is accepted so a caller that
    already knows which key failed does not depend on the scan order.

    Quarantine policy, chosen so neither extreme is silent:

    * ``401`` / ``403`` — the key is dead or revoked. Quarantined with no
      expiry (``quarantine_until: 0``): it stays out until the owner fixes the
      key file or runs ``hermes kanban keypool clear``.
    * ``429`` — rate limited, not dead. Quarantined for
      ``HERMES_KANBAN_KEYPOOL_QUARANTINE_SECONDS`` (default 900s = the
      documented limit window), then it becomes eligible again automatically.
    * anything else — treated like 401 but with the 429 default window, since a
      non-auth failure was not attributed to the credential by the classifier.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return None
    caller_pid = int(pid if pid is not None else os.getpid())
    path = state_path()
    if path is None or not path.exists():
        return None
    now = time.time()
    marked: Optional[Dict[str, Any]] = None
    with _state_lock(path):
        state = _read_state(path)
        reservations: Dict[str, Any] = state["reservations"]
        for key_entry, entry in list(reservations.items()):
            if not isinstance(entry, dict):
                continue
            kid = int(entry.get("key_id") or key_entry)
            if key_id is not None and kid != int(key_id):
                continue
            victim: Optional[Dict[str, Any]] = None
            for slot in _key_slots(entry):
                if slot.get("task_id") != task_id:
                    continue
                owner_pid = int(slot.get("pid") or 0)
                attached = slot.get("attached_pids") or []
                is_holder = caller_pid == owner_pid or (
                    isinstance(attached, list) and caller_pid in attached
                )
                if not is_holder:
                    continue
                victim = slot
                break
            if victim is None:
                continue
            owner_pid = int(victim.get("pid") or 0)
            failure = {
                "status_code": status_code,
                "reason": reason or f"http_{status_code}",
                "at": int(now),
                "task_id": task_id,
                "run_id": victim.get("run_id"),
                "pid": caller_pid,
                "owner_pid": owner_pid,
                "reported_by": "sibling" if caller_pid != owner_pid else "owner",
                "degradation": "card_kept_its_key_then_failed",
            }
            marked = {
                "key_id": kid,
                "key_label": entry.get("key_label"),
                **{k: v for k, v in victim.items()},
            }
            marked["status"] = _STATUS_BAD
            marked["failure"] = failure

            quarantine_until = 0
            if status_code == 429:
                quarantine_until = int(now + _quarantine_seconds())
            elif status_code in (401, 403):
                quarantine_until = 0  # dead key: no automatic return
            else:
                quarantine_until = int(now + _quarantine_seconds())
            # Quarantine the KEY, not the slot (requirement 7): a rejected
            # credential is not safe for the second card on the same key, so
            # the whole key leaves circulation while the window lasts.
            state.setdefault("failed_keys", {})[str(kid)] = {
                **failure,
                "key_id": kid,
                "key_label": entry.get("key_label") or f"key-{kid}",
                "quarantine_until": quarantine_until,
            }
            # The failing card keeps its slot while its own process is alive.
            # Deleting it was defect D1: at the production limit of one card
            # per key the key has exactly one slot, so ``kept`` came out empty,
            # the whole reservation record vanished, and the moment
            # ``quarantine_until`` elapsed the key went back into circulation
            # and was handed to a second card while the first was still
            # running on it. Marking the slot ``bad`` keeps it counted as
            # occupied, so the key is not free for anyone else. The slot is
            # removed by the card's own release (``release_for_task`` from
            # ``_release_card_key`` / ``release_for_pid`` on exit) or, if the
            # process dies without releasing, by the PID/TTL backstop in
            # ``_sweep_locked``. The OTHER card on a shared key keeps its own
            # slot untouched.
            victim["status"] = _STATUS_BAD
            victim["quarantined_at"] = int(now)
            break
        if marked is not None:
            _write_state(path, state)
    if marked is not None:
        logger.error(
            "key-pool: key %s for task %s rejected by the provider (HTTP %s) — "
            "marked bad and quarantined; this run kept its key and was not "
            "re-keyed (release_reason=key_failed)",
            marked.get("key_id"),
            task_id,
            status_code,
        )
    return marked


def clear_quarantine(key_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Drop quarantine records (owner action: trusted a key again).

    ``key_id=None`` clears every record — the recovery step after re-issuing a
    previously dead key.
    """
    path = state_path()
    if path is None or not path.exists():
        return []
    cleared: List[Dict[str, Any]] = []
    with _state_lock(path):
        state = _read_state(path)
        failed: Dict[str, Any] = state.get("failed_keys") or {}
        if key_id is None:
            cleared = list(failed.values())
            state["failed_keys"] = {}
        else:
            record = failed.pop(str(int(key_id)), None)
            if record is not None:
                cleared = [record]
            state["failed_keys"] = failed
        if cleared:
            _write_state(path, state)
    return cleared


# ── background / card resolution ───────────────────────────────────────────


def background_key() -> Optional[KeyBinding]:
    """Return the background binding (key #1), or ``None`` when not pooled.

    Used for every non-card resolution: interactive chat, cron, auxiliary and
    background work. Cards never see this id — ``card_key_ids`` excludes it.
    """
    cfg = load_config()
    if cfg is None or not cfg.enabled:
        return None
    kid = cfg.background_key_id
    return KeyBinding(
        key_id=kid,
        label=cfg.label(kid),
        role=_ROLE_BACKGROUND,
        api_key=cfg.secret(kid),
        status=_STATUS_ACTIVE,
    )


def _urls_match(a: str, b: str) -> bool:
    na, nb = _normalize_base_url(a), _normalize_base_url(b)
    if not na or not nb:
        return False
    return na == nb


def apply_card_key(runtime: Dict[str, Any]) -> Dict[str, Any]:
    """Override ``runtime['api_key']`` with this process's pooled key.

    The single hook called by ``resolve_runtime_provider``. Contract:

    * pool not configured / disabled / unreadable → the runtime is returned
      **unchanged**. This is the "identical to today" guarantee.
    * runtime points at a *different* endpoint than the pool declares → left
      alone (the pool only owns its own provider's traffic).
    * ``api_key`` is a callable (``key_cmd``/Entra) → left alone; a derived
      bearer is not a pooled static key.
    * card worker (proven dispatcher-owned worker — full env envelope AND
      not ``is_non_dispatcher_owned_context()``; see :func:`is_card_process`) →
      reserve and use the reserved key. Failure to reserve raises; it never
      falls back to the background key or to the shared env key.
    * background (everything else — including a stale bare
      ``HERMES_KANBAN_TASK`` and an in-process cron run inside a worker) → the
      background key.
    """
    # Honour the WHOLE enable gate, not just the file flag: ``is_enabled()``
    # also covers the ``HERMES_KANBAN_KEYPOOL=off`` env override and the
    # ``kanban-keypool.disabled`` sentinel written by `hermes kanban keypool
    # off`. Checking only ``cfg.enabled`` here would leave the documented
    # rollback command without effect — exactly the kind of "one flag returns
    # the old scheme" guarantee the mission requires.
    if not is_enabled():
        return runtime
    cfg = load_config()
    if cfg is None:
        return runtime
    if not isinstance(runtime, dict):
        return runtime

    api_key = runtime.get("api_key")
    if callable(api_key) and not isinstance(api_key, str):
        return runtime

    runtime_base = str(runtime.get("base_url") or "")
    if not cfg.base_url or not _urls_match(runtime_base, cfg.base_url):
        # Pool owns one endpoint only. Anything else (fallback provider,
        # secondary custom endpoint) keeps its own credentials. The
        # ``not cfg.base_url`` half is defence in depth behind
        # ``KeyPoolConfig.usable``: an unnamed endpoint cannot be matched, so a
        # pool key would go to whatever host the runtime names. Never guess.
        return runtime

    task_id, run_id = _card_identity()
    if not task_id:
        bg = background_key()
        if bg is None or not bg.api_key:
            return runtime
        out = dict(runtime)
        out["api_key"] = bg.api_key
        out["key_pool"] = bg.to_audit()
        return out

    # ── card path: fail closed, never fall back ────────────────────────────
    #
    # Everything below is wrapped so that a plumbing failure (state file
    # unreadable, disk full, lock timeout, an unexpected bug) surfaces as a
    # ``KeyPoolError`` for a CARD. Returning the untouched runtime here would
    # hand the card the shared ``.env`` key, which is exactly the one outcome
    # the owner forbade ("карточкам не выдаётся никогда"). A background
    # caller keeps the forgiving behaviour above; a card does not get to
    # "probably work" on someone else's account.
    try:
        _install_exit_release()
        global _binding, _binding_attempted, _binding_error
        with _binding_lock:
            if _binding_error is not None:
                # A previous resolution already failed in this process. Repeat
                # the same error instead of retrying (a retry could hand this
                # card a different key) and instead of degrading (which would
                # silently run the card on the shared env key).
                raise _binding_error
            if not _binding_attempted:
                reservation = reserve_for_card(task_id, run_id)
                _binding_attempted = True
                _binding = KeyBinding(
                    key_id=reservation.key_id,
                    label=reservation.label,
                    role=_ROLE_CARD,
                    api_key=reservation.api_key,
                    task_id=task_id,
                    run_id=run_id,
                    pid=reservation.pid,
                    status=reservation.status,
                )
            binding = _binding

        if binding is None:  # pragma: no cover - defensive
            raise KeyPoolError(
                f"key pool could not pin a key to card {task_id}; refusing to "
                f"run the card on the shared key"
            )
        if not binding.usable:
            # The reserved key was already rejected in this process. Do not
            # re-resolve to a different key — fail the call instead.
            raise KeyPoolError(
                f"this card's reserved key #{binding.key_id} ({binding.label}) was "
                f"rejected by the provider ({binding.failure or 'no detail'}); a "
                f"card never switches keys mid-run — start a new run to get a "
                f"fresh key"
            )
        # Keep the lease fresh: a card that is genuinely working must not lose
        # its key to the TTL backstop half-way through a long run. Renewed as
        # THIS pid (not ``binding.pid``, which is the reservation's owner): the
        # holder check in ``renew_for_card`` then proves the right to renew via
        # ``attached_pids`` instead of renewing under someone else's identity.
        #
        # Best-effort on purpose: the binding is already valid and the
        # "a live pid is never stale" rule keeps the key ours, so a lock
        # timeout here must not fail a card that is otherwise fine.
        try:
            renew_for_card(task_id, binding.key_id, pid=os.getpid())
        except KeyPoolError:
            logger.debug("key-pool: lease renewal failed for %s; key kept", task_id)
    except KeyPoolError:
        raise
    except Exception as exc:
        wrapped = KeyPoolError(
            f"key pool could not pin a key to card {task_id} ({type(exc).__name__}: "
            f"{exc}); refusing to run the card on the shared key"
        )
        with _binding_lock:
            _binding_error = wrapped
        raise wrapped from exc

    out = dict(runtime)
    out["api_key"] = binding.api_key
    out["key_pool"] = {
        "key_id": binding.key_id,
        "label": binding.label,
        "role": binding.role,
        "task_id": binding.task_id,
        "run_id": binding.run_id,
        "pid": binding.pid,
    }
    return out


def current_binding() -> Optional[KeyBinding]:
    """Return what this process is pinned to (``None`` when not pooled)."""
    with _binding_lock:
        return _binding


def active_card_binding() -> Optional[KeyBinding]:
    """The process binding, but only when THIS execution is the card itself.

    The binding is process-global (one key per process, contractually), while
    the process may host more than one execution: ``cronjob(action="run")``
    runs a whole cron agent in-process, and ``cron/scheduler.py`` marks that
    run with ``enter_non_dispatcher_owned_context()`` precisely because it is
    not the dispatcher-owned worker.

    Consumers that *act on* the binding — blocking credential rotation,
    blocking a provider fallback, quarantining the key — must therefore use
    this accessor instead of :func:`current_binding`. Reading the raw binding
    there meant a cron job inside a worker inherited the card's reservation:
    its 401 quarantined the live card's key (``mark_key_failed`` under the
    card's ``task_id``), and the card then failed its next resolution with
    ``KeyPoolError`` — a stranger burning the card's account.

    A ``delegate_task`` child is the opposite case and must keep seeing the
    binding: it is the card's own work running beside it, so the card's "never
    hop mid-run" discipline has to hold inside the child too. That is why this
    accessor keys on ``is_non_dispatcher_owned_context()`` (cron only) rather
    than on ``is_dispatcher_owned_worker_context()`` (cron *and* delegated
    child), which would let a child 401 silently move to another account's key.

    Observability and release paths keep using :func:`current_binding`: the
    status view must show what the process holds, and the release guard is
    only reached from the worker's own ``kanban_complete``/``kanban_block``.

    The failure directions are deliberately asymmetric with
    :func:`is_card_process`. There, an unusable probe raises
    :class:`KeyPoolError` (a proven worker envelope must never be demoted to
    ``background`` — that is exactly defect K7); here, an unusable probe
    returns the binding. Reason: this
    accessor's callers *block* a mid-run key hop. Returning ``None`` because a
    probe raised would let a real card silently rotate onto another account —
    the exact D3b regression. Keeping the binding can at worst make a foreign
    execution inside a card process fail loudly, which is the safe side.
    """
    binding = current_binding()
    if binding is None:
        return None
    try:
        # Narrow predicate only: a cron run inside the worker must not see the
        # card's binding, a ``delegate_task`` child must. Import plumbing or a
        # probe failure keeps the binding (see the asymmetry note above).
        if _narrow_non_dispatcher_owned():
            return None
    except Exception:  # pragma: no cover - defensive
        return binding
    return binding


def mark_current_key_failed(
    status_code: Optional[int], *, reason: str = ""
) -> Optional[Dict[str, Any]]:
    """Mark this process's reserved key bad. No-op for background/unpooled.

    Uses :func:`active_card_binding`, so an in-process cron run inside a worker
    cannot quarantine the worker's live card key: only the execution that
    genuinely owns the reservation may report it dead.
    """
    global _binding
    binding = active_card_binding()
    if binding is None or binding.role != _ROLE_CARD or binding.status != _STATUS_ACTIVE:
        return None
    marked = mark_key_failed(
        binding.task_id,
        status_code,
        reason=reason,
        # This process's OWN pid, not the reservation's owner pid: for an
        # adopted sibling the owner pid is another live process, and passing it
        # here would attribute the failure to the wrong process (and, before
        # the task-scoped holder check, silently do nothing).
        pid=os.getpid(),
        key_id=binding.key_id,
    )
    if marked is not None:
        failure = marked.get("failure") or {}
        with _binding_lock:
            _binding = KeyBinding(
                key_id=binding.key_id,
                label=binding.label,
                role=binding.role,
                api_key=binding.api_key,
                task_id=binding.task_id,
                run_id=binding.run_id,
                pid=binding.pid,
                status=_STATUS_BAD,
                failure=failure,
            )
    return marked


_atexit_installed = False


def _install_exit_release() -> None:
    """Free the key when the worker process dies, however it dies.

    ``atexit`` covers a clean return and an unhandled exception. A hard kill
    (SIGKILL / Task Manager / a dispatcher timeout) is covered by the PID
    check in :func:`_reservation_is_stale` on the next reservation.
    """
    global _atexit_installed
    if _atexit_installed:
        return
    _atexit_installed = True
    import atexit

    def _release() -> None:  # pragma: no cover - runs at interpreter shutdown
        try:
            release_for_pid()
        except Exception:
            pass

    atexit.register(_release)


def reset_process_cache() -> None:
    """Forget this process's memoised binding (tests and explicit teardown)."""
    global _binding, _binding_attempted, _binding_error, _atexit_installed
    with _binding_lock:
        _binding = None
        _binding_attempted = False
        _binding_error = None


# ── introspection (``hermes kanban keypool status``) ────────────────────────


def pool_status() -> Dict[str, Any]:
    """Secret-free snapshot of the pool for the CLI and the audit report.

    Two shapes are reported, because the owner needs both:

    * ``reservations`` — one flat row PER CARD SLOT (so "task X is on key #2"
      stays a single lookup and the pre-slot call sites keep working);
    * ``keys`` — one row per card key with ``used``/``limit`` and the task ids
      on it, which is the "key_id → занято/лимит" view requirement 6 asks for.
    """
    cfg = load_config()
    path = state_path()
    state = _read_state(path) if path is not None and path.exists() else {
        "version": 2,
        "reservations": {},
        "failed_keys": {},
    }
    now = time.time()
    limit = int(getattr(cfg, "max_cards_per_key", _DEFAULT_MAX_CARDS_PER_KEY) or 1) \
        if cfg is not None else _DEFAULT_MAX_CARDS_PER_KEY
    reservations = []
    occupancy: Dict[str, Dict[str, Any]] = {}
    for key, entry in state["reservations"].items():
        if not isinstance(entry, dict):
            continue
        kid = int(entry.get("key_id") or key)
        slots = _key_slots(entry)
        label = entry.get("key_label") or (cfg.label(kid) if cfg else f"key-{kid}")
        row = occupancy.setdefault(str(kid), {
            "key_id": kid,
            "key_label": label,
            "used": 0,
            "limit": limit,
            "task_ids": [],
        })
        for slot in slots:
            view = {k: v for k, v in slot.items() if k != "api_key"}
            view["key_id"] = kid
            view["key_label"] = label
            stale = _reservation_is_stale(slot, now)
            view["stale"] = stale or False
            reservations.append(view)
            row["used"] += 1
            row["task_ids"].append(slot.get("task_id"))
    reservations.sort(key=lambda e: (int(e.get("key_id") or 0), str(e.get("task_id") or "")))
    keys = [occupancy[k] for k in sorted(occupancy, key=lambda s: int(s))]
    if cfg is not None:
        # Card keys with no card on them must still show up as 0/limit — a
        # status view that lists only busy keys hides the free capacity.
        present = {row["key_id"] for row in keys}
        for kid in cfg.card_key_ids:
            if kid in present:
                continue
            keys.append({
                "key_id": kid,
                "key_label": cfg.label(kid),
                "used": 0,
                "limit": limit,
                "task_ids": [],
            })
        keys.sort(key=lambda row: int(row["key_id"]))
    failed = []
    for record in (state.get("failed_keys") or {}).values():
        if isinstance(record, dict):
            failed.append(dict(record))
    failed.sort(key=lambda e: int(e.get("key_id") or 0))
    # Why the pool is not in force, when it is not. ``None`` here while
    # ``enabled`` is False means "disabled by the env override or the rollback
    # sentinel" — those are checked before the config, so the config cannot
    # report them.
    if cfg is None:
        inactive_reason = "no_key_file" if not key_pool_path().exists() else "unusable_key_file"
    else:
        inactive_reason = cfg.inactive_reason
    return {
        "enabled": is_enabled(),
        "inactive_reason": inactive_reason,
        "env_override": (os.environ.get(ENV_KEYPOOL) or "").strip() or None,
        "env_says_off": _env_says_off(),
        "key_file": str(key_pool_path()),
        "key_file_exists": key_pool_path().exists(),
        "state_file": str(path) if path is not None else None,
        "disabled_marker": str(disabled_marker_path()),
        "disabled_marker_exists": disabled_marker_path().exists(),
        "base_url": cfg.base_url if cfg else None,
        "provider": cfg.provider if cfg else None,
        "background_key_id": cfg.background_key_id if cfg else None,
        "card_key_ids": cfg.card_key_ids if cfg else [],
        # The owner's intentional exclusions, so the status view can say *why*
        # a card-role key is absent from distribution instead of leaving the
        # operator to diff the key file against `card_key_ids` by hand.
        "excluded_card_key_ids": cfg.excluded_card_key_ids if cfg else [],
        # Labels for those ids. They are deliberately NOT in `keys` (that array
        # is the distribution/occupancy view), so the reason line needs its own
        # lookup or the operator sees a bare number.
        "excluded_card_key_labels": (
            {str(kid): cfg.label(kid) for kid in cfg.excluded_card_key_ids}
            if cfg else {}
        ),
        "key_count": len(cfg.keys) if cfg else 0,
        "max_cards_per_key": limit,
        "rr_cursor": state.get("rr_cursor"),
        "keys": keys,
        "reservations": reservations,
        "failed_keys": failed,
        "this_process": (current_binding().to_audit() if current_binding() else None),
    }


def sweep() -> List[Dict[str, Any]]:
    """Drop stale reservations now; return what was reaped."""
    path = state_path()
    if path is None or not path.exists():
        return []
    now = time.time()
    with _state_lock(path):
        state = _read_state(path)
        dropped = _sweep_locked(state, now)
        if dropped:
            _write_state(path, state)
    return dropped
