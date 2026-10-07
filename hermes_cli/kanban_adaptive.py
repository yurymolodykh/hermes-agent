"""AIMD-adaptive host-level worker cap for the kanban dispatcher (M2).

Why
---
The dispatcher's host concurrency cap (``kanban.max_in_progress``) is static:
it has to be sized for the worst case (a busy board under memory pressure) or
a small host can be dispatched into swap-thrash / OOM; sized for the average
case it wastes headroom on a big, idle host. This module runs an AIMD
(additive-increase / multiplicative-decrease) automaton on top of the K1
cross-platform memory sampler so the effective cap tracks live host pressure
instead of a single frozen number.

When to use
-----------
Only the dispatcher integrates this, and only when ``kanban.adaptive: true``
is set in config.yaml (off by default; flag rollout is tracked separately —
see the kanban mission's K5 card). With the flag off the module is never
consulted: ``kanban_db.dispatch_once`` keeps using the static
``max_in_progress`` and behaves bit-for-bit as before (fail-open contract:

* broken/corrupt state file  -> state discarded, rebuilt from base;
* sampler returns "unknown" -> no growth and no decay, no harm;
* base is None (no static cap) -> module is inert, effective cap stays None;
* any exception while ticking -> the caller keeps the static base.

The automaton
-------------
State is persisted atomically (tmp + rename) to
``<kanban_home>/kanban/adaptive_state.json`` so it survives dispatcher
restarts. Fields:

* ``cap``               -- current effective host cap (starts at the static
                           base and moves in ``[floor, ceiling]``);
* ``streak``            -- consecutive CALM ticks (growth accumulator);
* ``last_spawn_ts``     -- most recent worker spawn (stabilization window);
* ``last_decrease_ts``  -- most recent multiplicative decrease;
* ``cooldown_until``    -- wall clock until which growth is blocked after a
                           decrease (600s);
* ``empty_since``       -- wall clock since the ready queue was last seen
                           non-empty under ok pressure (30-min idle-decay
                           trigger);
* ``last_advance_ts``   -- throttle: the automaton advances at most once per
                           dispatch interval even when the gateway ticks
                           several boards in the same interval.

Per dispatch tick (pressure from the K1 sampler, queue = any spawnable
ready task on the dispatched board):

* elevated pressure -> cap = max(floor, cap // 2); cooldown 600s; streak = 0;
* critical pressure -> streak = 0 (the existing memory-pressure guard in
                       kanban_db already freezes spawning for the tick);
* calm tick         -> ok pressure AND ready queue AND past the 180s
                       post-spawn stabilization AND not in cooldown:
                       streak += 1; at 3 consecutive calm ticks cap += 1;
* idle decay        -> once the queue has been empty under ok pressure for
                       >= 30 minutes, cap decays by 1 per tick toward base
                       (never below base).

Ceiling = min(base + 4, 12); floor = min(4, base). The cap is clamped to
that band on every advance and on every load, so a config change to
``max_in_progress`` cannot wedge a persisted cap out of range.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (unit-tested via the controller; keep in sync with the card t_4b7d10af)
# ---------------------------------------------------------------------------

STATE_VERSION = 1
STATE_FILENAME = "adaptive_state.json"

#: Consecutive calm ticks required for cap +1.
GROWTH_STREAK_REQUIRED = 3
#: ``ceiling = min(base + CAP_CEILING_EXTRA, CAP_CEILING_GLOBAL)``.
CAP_CEILING_EXTRA = 4
#: Absolute ceiling (base 8 -> 12 per the mission card).
CAP_CEILING_GLOBAL = 12
#: Absolute floor for ``cap // 2`` under elevated pressure.
CAP_FLOOR_DEFAULT = 4
#: Growth is blocked for this many seconds after a multiplicative decrease.
DECREASE_COOLDOWN_SECONDS = 600
#: A freshly spawned worker allocates memory lazily — do not count ticks
#: within this window after the last spawn as "calm".
SPAWN_STABILIZATION_SECONDS = 180
#: Idle decay begins once the queue has been empty under ok pressure for
#: this many seconds (30 minutes).
IDLE_DECAY_SECONDS = 1800
#: Fallback for ``kanban.dispatch_interval_seconds`` when unset.
DEFAULT_DISPATCH_INTERVAL = 60.0
#: The automaton never advances more often than half a dispatch interval,
#: even when several boards tick in the same interval (single host cap).
MIN_ADVANCE_INTERVAL_MIN = 1.0


# ---------------------------------------------------------------------------
# Config + state path (lazy imports: kanban_adaptive must not import
# kanban_db at module import time -- kanban_db imports this module lazily)
# ---------------------------------------------------------------------------


def _load_kanban_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def adaptive_enabled() -> bool:
    """Whether ``kanban.adaptive`` is enabled in config (default: off)."""
    return bool(_load_kanban_config().get("adaptive", False))


def adaptive_state_path() -> Path:
    """Path of the persisted automaton state, next to the kanban DB.

    ``kanban_home()`` anchors the shared board root (the same resolution
    ``kanban_db_path`` uses), so the state file sits beside ``kanban.db`` at
    ``<kanban_home>/kanban/adaptive_state.json``.
    """
    from hermes_cli.kanban_db import kanban_home  # lazy: avoids import cycle
    return kanban_home() / "kanban" / STATE_FILENAME


def default_min_advance_interval() -> float:
    """Throttle interval for a single automaton advance (config-derived)."""
    interval = DEFAULT_DISPATCH_INTERVAL
    try:
        interval = float(
            _load_kanban_config().get(
                "dispatch_interval_seconds", DEFAULT_DISPATCH_INTERVAL
            )
        )
    except (TypeError, ValueError):
        pass
    if interval <= 0:
        interval = DEFAULT_DISPATCH_INTERVAL
    return max(MIN_ADVANCE_INTERVAL_MIN, interval / 2.0)


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


def _opt_float(value) -> Optional[float]:
    """Coerce a persisted timestamp; None for absent/blank, ValueError for
    garbage so ``from_dict`` can treat it as corrupt."""
    if value is None or value == "":
        return None
    return float(value)


def _floor_for(base: int) -> int:
    return min(CAP_FLOOR_DEFAULT, int(base))


def _ceiling_for(base: int) -> int:
    return min(int(base) + CAP_CEILING_EXTRA, CAP_CEILING_GLOBAL)


def _clamp(cap: int, floor: int, ceiling: int) -> int:
    return max(floor, min(ceiling, cap))


class AdaptiveState:
    """Serializable automaton state (see module docstring)."""

    __slots__ = (
        "cap",
        "streak",
        "last_spawn_ts",
        "last_decrease_ts",
        "cooldown_until",
        "empty_since",
        "last_advance_ts",
    )

    def __init__(
        self,
        cap: int,
        streak: int = 0,
        last_spawn_ts: Optional[float] = None,
        last_decrease_ts: Optional[float] = None,
        cooldown_until: Optional[float] = None,
        empty_since: Optional[float] = None,
        last_advance_ts: float = 0.0,
    ):
        self.cap = int(cap)
        self.streak = int(streak)
        self.last_spawn_ts = last_spawn_ts
        self.last_decrease_ts = last_decrease_ts
        self.cooldown_until = cooldown_until
        self.empty_since = empty_since
        self.last_advance_ts = float(last_advance_ts)

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "cap": self.cap,
            "streak": self.streak,
            "last_spawn_ts": self.last_spawn_ts,
            "last_decrease_ts": self.last_decrease_ts,
            "cooldown_until": self.cooldown_until,
            "empty_since": self.empty_since,
            "last_advance_ts": self.last_advance_ts,
        }

    @classmethod
    def from_dict(cls, data, base: int) -> Optional["AdaptiveState"]:
        """Build from a parsed JSON object; None when the payload is corrupt.

        A persisted ``cap`` outside ``[1, CAP_CEILING_GLOBAL]`` is treated as
        corrupt (the automaton can never produce it), so a hand-edited or
        stale value falls back to a fresh-from-base state.
        """
        try:
            cap = int(data["cap"])
            streak = int(data.get("streak", 0))
            last_spawn_ts = _opt_float(data.get("last_spawn_ts"))
            last_decrease_ts = _opt_float(data.get("last_decrease_ts"))
            cooldown_until = _opt_float(data.get("cooldown_until"))
            empty_since = _opt_float(data.get("empty_since"))
            last_advance_ts = _opt_float(data.get("last_advance_ts")) or 0.0
        except (KeyError, TypeError, ValueError):
            return None
        if not 1 <= cap <= CAP_CEILING_GLOBAL:
            return None
        if streak < 0:
            return None
        return cls(
            cap,
            streak,
            last_spawn_ts,
            last_decrease_ts,
            cooldown_until,
            empty_since,
            last_advance_ts,
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_state(base: int, path: Optional[Path] = None) -> AdaptiveState:
    """Load persisted state; fresh-from-base on missing / corrupt file."""
    path = path if path is not None else adaptive_state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AdaptiveState(cap=base)
    if not isinstance(raw, dict):
        return AdaptiveState(cap=base)
    state = AdaptiveState.from_dict(raw, base)
    if state is None:
        return AdaptiveState(cap=base)
    # clamp to the current base's band (config may have changed)
    state.cap = _clamp(state.cap, _floor_for(base), _ceiling_for(base))
    return state


def _now() -> float:
    """Wall clock. Module-level seam so tests / wiring can pin time."""
    return time.time()


# ---------------------------------------------------------------------------
# Controller (pure automaton; tests drive it directly)
# ---------------------------------------------------------------------------


class AdaptiveController:
    """One AIMD automaton bound to one persisted state file.

    ``advance`` mutates ``self.state`` and returns the effective cap for the
    current tick. It is throttled to one advance per dispatch interval so a
    gateway that ticks several boards in the same interval advances the
    single host-level automaton only once.
    """

    def __init__(self, state: AdaptiveState, path: Optional[Path] = None):
        self.state = state
        self.path = path if path is not None else adaptive_state_path()
        self._dirty = False

    @classmethod
    def from_disk(
        cls, base: int, path: Optional[Path] = None, now: Optional[float] = None
    ) -> "AdaptiveController":
        return cls(load_state(base, path=path), path=path)

    # -- helpers ------------------------------------------------------------

    def _touch(self) -> None:
        self._dirty = True

    def cap_clamped(self, base: int) -> Optional[int]:
        """Current cap without mutating state (used by dry-run ticks)."""
        if base is None:
            return None
        return _clamp(self.state.cap, _floor_for(base), _ceiling_for(base))

    # -- automaton ----------------------------------------------------------

    def advance(
        self,
        base: int,
        pressure: str,
        queue_exists: bool,
        now: Optional[float] = None,
        min_advance_interval: Optional[float] = None,
    ) -> Optional[int]:
        """Advance the automaton by one dispatch tick and return the cap.

        ``pressure`` is one of ``ok`` / ``elevated`` / ``critical`` /
        ``unknown`` (from the K1 sampler). ``queue_exists`` is from
        ``has_spawnable_ready`` on the dispatched board. Returns ``None``
        only when ``base`` is None (module inert).
        """
        if base is None:
            return None
        now = _now() if now is None else now
        if min_advance_interval is None:
            min_advance_interval = default_min_advance_interval()

        st = self.state
        floor = _floor_for(base)
        ceiling = _ceiling_for(base)

        # Throttle: at most one advance per dispatch interval. The cap is
        # still returned (read-only) so a sibling board's tick this interval
        # sees the same effective cap without double-advancing.
        if now - st.last_advance_ts < min_advance_interval:
            return _clamp(st.cap, floor, ceiling)

        st.last_advance_ts = now
        self._touch()

        if pressure == "elevated":
            new_cap = _clamp(st.cap // 2, floor, ceiling)
            if new_cap != st.cap:
                st.cap = new_cap
                self._touch()
            if st.streak:
                st.streak = 0
                self._touch()
            if st.cooldown_until != now + DECREASE_COOLDOWN_SECONDS:
                st.cooldown_until = now + DECREASE_COOLDOWN_SECONDS
                self._touch()
            if st.last_decrease_ts != now:
                st.last_decrease_ts = now
                self._touch()
            if st.empty_since is not None:
                st.empty_since = None  # pressure broke the idle window
                self._touch()

        elif pressure == "critical":
            # The dispatcher's memory-pressure guard freezes spawning for
            # this tick; here we only refuse to count it as calm.
            if st.streak:
                st.streak = 0
                self._touch()
            if st.empty_since is not None:
                st.empty_since = None
                self._touch()

        else:
            # ok / unknown. Never grow on unknown (can't prove calm), and
            # idle decay only runs when pressure is provably ok.
            if pressure == "ok":
                if queue_exists:
                    if st.empty_since is not None:
                        st.empty_since = None
                        self._touch()
                else:
                    if st.empty_since is None:
                        st.empty_since = now
                        self._touch()
                    elif (
                        now - st.empty_since >= IDLE_DECAY_SECONDS
                        and st.cap > base
                    ):
                        st.cap = max(base, st.cap - 1)
                        self._touch()

            in_cooldown = (
                st.cooldown_until is not None
                and now < st.cooldown_until
            )
            stabilizing = (
                st.last_spawn_ts is not None
                and now - st.last_spawn_ts < SPAWN_STABILIZATION_SECONDS
            )
            calm = (
                pressure == "ok"
                and queue_exists
                and not stabilizing
                and not in_cooldown
            )
            if calm:
                st.streak += 1
                if st.streak >= GROWTH_STREAK_REQUIRED:
                    if st.cap < ceiling:
                        st.cap += 1
                    st.streak = 0
                self._touch()
            elif st.streak:
                st.streak = 0
                self._touch()

        st.cap = _clamp(st.cap, floor, ceiling)
        return st.cap

    def note_spawned(self, now: Optional[float] = None) -> None:
        """Record a worker spawn (arms the 180s stabilization window)."""
        now = _now() if now is None else now
        st = self.state
        if st.last_spawn_ts != now:
            st.last_spawn_ts = now
            self._touch()

    # -- persistence --------------------------------------------------------

    def save_dirty(self) -> None:
        if not self._dirty:
            return
        self._write()
        self._dirty = False

    def _write(self) -> None:
        """Atomic tmp + rename write; failures are non-fatal (fail-open)."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    self.state.to_dict(), fh, ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(tmp, self.path)
        except OSError as exc:
            _log.debug("kanban adaptive: state write failed: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Module wiring for kanban_db.dispatch_once
# ---------------------------------------------------------------------------

_controllers: dict = {}
_controllers_lock = threading.Lock()


def _controller_for(base: int, path: Path) -> AdaptiveController:
    with _controllers_lock:
        ctl = _controllers.get(str(path))
        if ctl is None:
            ctl = AdaptiveController.from_disk(base, path=path)
            _controllers[str(path)] = ctl
        return ctl


def tick_for_dispatch(
    *,
    base: int,
    queue_exists: bool,
    board: Optional[str] = None,
    dry_run: bool = False,
    now: Optional[float] = None,
    min_advance_interval: Optional[float] = None,
) -> Optional[int]:
    """Advance the host AIMD automaton for one dispatcher tick and return the
    effective cap to gate spawning with.

    ``queue_exists`` comes from ``has_spawnable_ready`` on the dispatched
    board. Live pressure is sampled via ``kanban_db._memory_pressure_level``
    (the K1 sampler). ``dry_run`` ticks read the current cap without
    advancing or persisting. Returns the static ``base`` unchanged when the
    automaton is inert (see module docstring).
    """
    path = adaptive_state_path()
    ctl = _controller_for(base, path)
    if dry_run:
        return ctl.cap_clamped(base)
    cap = ctl.advance(
        base=base,
        pressure=_live_pressure(),
        queue_exists=queue_exists,
        now=now,
        min_advance_interval=min_advance_interval,
    )
    ctl.save_dirty()
    return cap


def note_spawned(now: Optional[float] = None) -> None:
    """Record a worker spawn from this interval on the active automaton.

    No-op when adaptive is off (no controller exists for the state path).
    """
    with _controllers_lock:
        ctl = _controllers.get(str(adaptive_state_path()))
        if ctl is None:
            return
        ctl.note_spawned(now=now)
        ctl.save_dirty()


def _live_pressure() -> str:
    """Current system memory pressure via the K1 sampler (unknown on error)."""
    try:
        from hermes_cli import kanban_db as _kb
        return _kb._memory_pressure_level()
    except Exception:
        return "unknown"


def _reset_for_tests() -> None:
    """Drop the in-process controller cache (test isolation helper)."""
    with _controllers_lock:
        _controllers.clear()
