"""Daytime concurrency window for the kanban dispatcher.

``kanban.max_in_progress`` bounds how many cards run at once on this host. That
one number has to serve two very different parts of the day: during business
hours the owner wants a narrow lane (fewer parallel workers, less context
switching on the human side), at night the full width is fine. This sibling
turns two config keys into that layered cap:

```yaml
kanban:
  max_in_progress: 7
  day_max_in_progress: 4
  day_window: ["08:00", "18:00"]   # 08:00 inclusive, 18:00 exclusive
  day_window_tz: "Europe/Moscow"
  day_window_days: ["mon", "tue", "wed", "thu", "fri"]  # optional; unset = every day
```

``day_window_days`` narrows the layer to the listed weekdays (``mon``…``sun``, or
``0``…``6`` with Monday as 0); on any other weekday the base cap applies for the
whole day, so a weekend keeps the full width while business days get the narrow
lane. A malformed list warns once and keeps the pre-key behaviour (window every
day) — a typo must not silently widen the weekday cap.

It is a *layer*, never a replacement: with the day keys unset (their shipped
default) :func:`resolve_day_window_cap` returns the base cap untouched, so
every existing install keeps exactly the behaviour it had.

Two properties are load-bearing and both are why this lives outside the
dispatcher tick:

* **It is evaluated on every tick, not at boot.** The 08:00 and 18:00 edges
  have to move the cap in a gateway process that has been up for days, so the
  transition cannot be a startup-time read.
* **Every failure is fail-open.** A malformed time, an unknown zone or a
  non-integer cap degrades to the base cap with a single WARN — a typo in
  ``config.yaml`` must never stop dispatch or crash the tick.

The level change is logged once per transition (under a lock) instead of on
each of the 600 ticks an eight-hour window spans.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional

DEFAULT_DAY_WINDOW_TZ = "Europe/Moscow"

# UTC+3 — Moscow has no DST, so a fixed offset is a faithful fallback when the
# platform has no tz database (and it keeps the window meaningful instead of
# silently disabling it).
FALLBACK_TZ = timezone(timedelta(hours=3))

_LOG_LOCK = threading.Lock()
_logged_level: Any = "unset"
_warned: set[str] = set()


def _log():
    """The dispatcher's logger, late-bound (``hermes_cli.kanban_db._log``)."""
    from hermes_cli import kanban_db

    return kanban_db._log


def _warn_once(key: str, message: str, *args: Any) -> None:
    """WARN once per distinct bad-config value; a 60s tick must not spam."""
    with _LOG_LOCK:
        if key in _warned:
            return
        _warned.add(key)
    _log().warning(message, *args)


def reset_state() -> None:
    """Forget the logged level and the warn-once memory (tests, and config reloads)."""
    global _logged_level
    with _LOG_LOCK:
        _logged_level = "unset"
        _warned.clear()


def _parse_time(raw: Any) -> Optional[dt_time]:
    """``"08:00"`` -> ``time(8, 0)``; anything else -> ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        hours, minutes = raw.strip().split(":")
        return dt_time(int(hours), int(minutes))
    except (AttributeError, TypeError, ValueError):
        return None


_DAY_NAMES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _parse_day_of_week(raw: Any) -> Optional[int]:
    """``"mon"``/``"Mon"``/``0`` -> ``0`` (Monday); anything else -> ``None``."""
    if isinstance(raw, bool):  # bool is an int subclass; not a weekday
        return None
    if isinstance(raw, int):
        return raw if 0 <= raw <= 6 else None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _DAY_NAMES:
            return _DAY_NAMES[text]
        if text.isdigit():
            value = int(text)
            return value if 0 <= value <= 6 else None
    return None


def _parse_days(raw: Any) -> Optional[frozenset[int]]:
    """``kanban.day_window_days`` -> the weekdays the window applies to.

    ``None`` means "every day" — both when the key is unset (the shipped
    default) and when it is unusable. A broken list deliberately keeps the
    window running every day: falling back to "no day limit at all" would
    silently hand the owner the wide cap on business days, the exact opposite
    of the misconfiguration's likely intent.
    """
    if raw is None:
        return None
    days: set[int] = set()
    if isinstance(raw, (list, tuple)) and raw:
        for item in raw:
            day = _parse_day_of_week(item)
            if day is None:
                days = set()
                break
            days.add(day)
    if not days:
        _warn_once(
            f"days:{raw!r}",
            "kanban dispatch: invalid kanban.day_window_days=%r (expected a non-empty "
            "list of mon..sun or 0..6, Monday = 0); the daytime window applies to every "
            "day of the week",
            raw,
        )
        return None
    return frozenset(days)


def _load_settings(
) -> tuple[Optional[int], Optional[tuple[dt_time, dt_time]], Any, Optional[frozenset[int]]]:
    """Read and validate ``kanban.day_*``.

    Returns ``(day_cap, (start, end), tzinfo, days)``. ``day_cap is None`` means
    "the window is not configured, or is unusable — apply the base cap";
    ``days is None`` means "the window applies to every day of the week".
    """
    try:
        from hermes_cli.config import load_config_readonly

        kanban_cfg = (load_config_readonly() or {}).get("kanban", {})
    except Exception:  # a broken config must not take dispatch down
        return None, None, None, None
    if not isinstance(kanban_cfg, dict):
        return None, None, None, None

    raw_cap = kanban_cfg.get("day_max_in_progress")
    raw_window = kanban_cfg.get("day_window")
    raw_tz = kanban_cfg.get("day_window_tz")
    raw_days = kanban_cfg.get("day_window_days")

    day_cap: Optional[int] = None
    if raw_cap is not None:
        if isinstance(raw_cap, bool):  # bool is an int subclass; not a cap
            _warn_once(
                f"cap:{raw_cap!r}",
                "kanban dispatch: kanban.day_max_in_progress=%r is a boolean; the daytime "
                "cap is ignored and the base max_in_progress applies",
                raw_cap,
            )
        else:
            try:
                day_cap = int(raw_cap)
            except (TypeError, ValueError):
                _warn_once(
                    f"cap:{raw_cap!r}",
                    "kanban dispatch: invalid kanban.day_max_in_progress=%r (expected a "
                    "positive integer); the daytime cap is ignored and the base "
                    "max_in_progress applies",
                    raw_cap,
                )
            else:
                if day_cap < 1:
                    _warn_once(
                        f"cap:{raw_cap!r}",
                        "kanban dispatch: kanban.day_max_in_progress=%r is below 1; the "
                        "daytime cap is ignored and the base max_in_progress applies",
                        raw_cap,
                    )
                    day_cap = None

    window: Optional[tuple[dt_time, dt_time]] = None
    if raw_window is not None:
        start = end = None
        if isinstance(raw_window, (list, tuple)) and len(raw_window) == 2:
            start = _parse_time(raw_window[0])
            end = _parse_time(raw_window[1])
        if start is None or end is None or start >= end:
            # A window that wraps midnight (18:00 -> 08:00) is deliberately not
            # supported: `start >= end` reads as a mistake, and silently
            # inventing wrap-around semantics would cap the wrong eight hours.
            _warn_once(
                f"window:{raw_window!r}",
                "kanban dispatch: invalid kanban.day_window=%r (expected "
                "[\"HH:MM\", \"HH:MM\"] with start earlier than end); the daytime cap is "
                "ignored and the base max_in_progress applies",
                raw_window,
            )
        else:
            window = (start, end)

    if day_cap is None or window is None:
        return None, None, None, None

    tz: Any = FALLBACK_TZ
    if raw_tz is not None and not isinstance(raw_tz, str):
        _warn_once(
            f"tz:{raw_tz!r}",
            "kanban dispatch: invalid kanban.day_window_tz=%r (expected an IANA zone "
            "name); the daytime window uses UTC+3 instead",
            raw_tz,
        )
    else:
        name = (raw_tz or DEFAULT_DAY_WINDOW_TZ).strip() or DEFAULT_DAY_WINDOW_TZ
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(name)
        except Exception:  # unknown zone, or a host without a tz database
            _warn_once(
                f"tz:{name}",
                "kanban dispatch: unknown kanban.day_window_tz=%r; the daytime window "
                "uses UTC+3 instead",
                name,
            )
            tz = FALLBACK_TZ
    days = _parse_days(raw_days)
    return day_cap, window, tz, days


def resolve_day_window_cap(
    base_cap: Optional[int],
    *,
    now: Optional[float] = None,
    settings: Optional[tuple] = None,
) -> Optional[int]:
    """The concurrency cap for ``now`` with the daytime window applied.

    Inside ``[day_window[0], day_window[1])`` of ``day_window_tz`` **and** on a
    weekday listed in ``day_window_days`` (every day when that key is unset) the
    result is ``min(base_cap, day_max_in_progress)`` — or ``day_max_in_progress``
    alone when ``base_cap is None``. Outside the window, on an unlisted weekday,
    and whenever the day config is unset or unusable, it is ``base_cap``
    unchanged.

    ``settings`` is a test seam: pass the tuple :func:`_load_settings` would
    return to avoid touching the real config. The historical 3-element tuple
    (no weekday list) is still accepted.
    """
    if settings is None:
        settings = _load_settings()
    day_cap, window, tz = settings[0], settings[1], settings[2]
    days = settings[3] if len(settings) > 3 else None
    if day_cap is None or window is None:
        return base_cap

    stamp = time.time() if now is None else float(now)
    local = datetime.fromtimestamp(stamp, tz)
    local_time = local.timetz().replace(tzinfo=None)
    weekday = local.weekday()  # Monday == 0, as datetime documents
    start, end = window
    in_window = start <= local_time < end
    on_day = days is None or weekday in days
    if not (in_window and on_day):
        effective = base_cap
    elif base_cap is None:
        effective = day_cap
    else:
        effective = min(int(base_cap), day_cap)

    global _logged_level
    with _LOG_LOCK:
        changed = _logged_level != effective
        if changed:
            _logged_level = effective
    if changed:
        if in_window and on_day:
            state = "window applies"
        elif not in_window:
            state = "window does not apply (outside the window)"
        else:
            state = (
                "window does not apply (day_window_days excludes "
                f"{local.strftime('%a').lower()})"
            )
        _log().info(
            "kanban dispatch: daytime window %s-%s %s (%s) — concurrency cap is now %s "
            "(day_max_in_progress=%d, base=%s)",
            start.strftime("%H:%M"),
            end.strftime("%H:%M"),
            getattr(tz, "key", tz),
            state,
            effective,
            day_cap,
            base_cap,
        )
    return effective
