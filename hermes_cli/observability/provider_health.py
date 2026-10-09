"""Per-provider LLM drop-rate ledger for ``/status`` (provider health).

The owner's requirement: the ``/status`` slash command should also report the
**percent of dropped LLM sessions per provider over the last 24 hours**, so
token-supplier stability is diagnosable at a glance.

Design (mirrors ``gateway/sysinfo.py``: collected by code, fast, best-effort):

* Recording rides the existing first-party observability hooks (see
  ``hermes_cli/lifecycle.py``):
  - ``post_api_request``  -> outcome ``ok``  (the logical request completed),
  - ``api_request_error`` -> outcome ``err`` (one attempt failed).
* A *logical request* is identified by ``api_request_id``
  (``"<turn_id>:api:<api_call_count>"``), which stays stable across in-loop
  retries.  N failed attempts followed by a success therefore collapse to one
  ``ok`` outcome; a request whose **last** recorded event is an error is a
  *drop* (retries exhausted or non-retryable failure).
* Storage is append-only JSONL under
  ``<hermes_home>/metrics/provider_health-YYYYMMDD.jsonl`` — one file per
  local day with 3-day retention, so a 24h aggregation reads at most two
  small files.  Multiple Hermes processes (gateway, CLI) append to the same
  shared ledger; per-line appends are small enough to be atomic in practice.
* Everything here is best-effort: a recording or aggregation failure must
  never raise into a lifecycle hook or into ``/status``.

Public API:

* ``observe_lifecycle(hook_name, **kwargs)`` — first-party hook consumer.
* ``handles_hook(hook_name)`` — whether this module consumes ``hook_name``.
* ``provider_drop_stats(window_hours=24.0)`` —
  ``{provider: {"total": int, "dropped": int, "drop_pct": float}}``.
* ``format_provider_health_lines(stats)`` — the localized ``/status`` block.
* ``enabled()`` — the ``telemetry.provider_health`` policy gate (default on).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Hooks this observer consumes.  ``post_api_request`` is the success signal
# and ``api_request_error`` the failure signal; both already carry the
# provider/model identity and (for errors) the classified status code.
HANDLED_HOOKS = frozenset({"post_api_request", "api_request_error"})

# Ledger layout: one file per local day keeps any 24h read to <= 2 files.
_LEDGER_PREFIX = "provider_health-"
_LEDGER_SUFFIX = ".jsonl"
_DATE_FMT = "%Y%m%d"

# How long old day-files stay on disk before an opportunistic sweep.
_RETENTION_DAYS = 3

# Upper bound on providers rendered in /status (rest is summarized).
_MAX_PROVIDERS_SHOWN = 8

_APPEND_LOCK = threading.Lock()
_SWEEP_LOCK = threading.Lock()
_last_sweep_date: Optional[date] = None


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Return the ``telemetry.provider_health`` policy (default: on).

    Read-only fast path (same helper ``relay_shared_metrics.enabled()`` uses,
    which is already called 2-3x per agent turn).
    """
    try:
        from hermes_cli.config import read_raw_config_readonly

        config = read_raw_config_readonly() or {}
    except Exception:
        # A broken config must not disable diagnostics silently; default on.
        return True
    telemetry = config.get("telemetry") if isinstance(config, dict) else None
    if isinstance(telemetry, dict) and "provider_health" in telemetry:
        return bool(telemetry.get("provider_health"))
    return True


def handles_hook(hook_name: str) -> bool:
    """Whether this module consumes ``hook_name`` (policy-gated)."""
    return hook_name in HANDLED_HOOKS and enabled()


# ---------------------------------------------------------------------------
# Ledger layout
# ---------------------------------------------------------------------------

def _ledger_dir(home: Optional[Path] = None) -> Path:
    """Return the metrics directory (``<hermes_home>/metrics``)."""
    if home is not None:
        return Path(home)
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "metrics"


def _day_path(directory: Path, day: date) -> Path:
    return directory / f"{_LEDGER_PREFIX}{day.strftime(_DATE_FMT)}{_LEDGER_SUFFIX}"


def _parse_day(path: Path) -> Optional[date]:
    """Extract the date from a ``provider_health-YYYYMMDD.jsonl`` name."""
    name = path.name
    if not name.startswith(_LEDGER_PREFIX) or not name.endswith(_LEDGER_SUFFIX):
        return None
    raw = name[len(_LEDGER_PREFIX):-len(_LEDGER_SUFFIX)]
    try:
        return datetime.strptime(raw, _DATE_FMT).date()
    except ValueError:
        return None


def _sweep_old_files(directory: Path, today: date) -> None:
    """Best-effort delete day-files older than the retention window."""
    global _last_sweep_date
    with _SWEEP_LOCK:
        if _last_sweep_date == today:
            return
        _last_sweep_date = today
        cutoff = today - timedelta(days=_RETENTION_DAYS)
        try:
            if not directory.is_dir():
                return
            for path in directory.iterdir():
                day = _parse_day(path)
                if day is not None and day < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _record(
    provider: Any,
    api_request_id: Any,
    outcome: str,
    *,
    model: Any = None,
    status_code: Any = None,
    error_type: Any = None,
    ts: Optional[float] = None,
    now: Optional[datetime] = None,
    home: Optional[Path] = None,
) -> None:
    """Append one outcome line.  Never raises."""
    if outcome not in ("ok", "err"):
        return
    prov = str(provider or "").strip() or "unknown"
    req_id = str(api_request_id or "").strip()
    if not req_id:
        return
    line = {
        "ts": float(ts) if ts is not None else datetime.now().timestamp(),
        "p": prov,
        "r": req_id,
        "o": outcome,
    }
    if model:
        line["m"] = str(model)
    if status_code is not None:
        line["sc"] = status_code
    if error_type:
        line["e"] = str(error_type)

    stamp = now or datetime.now()
    try:
        directory = _ledger_dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        path = _day_path(directory, stamp.date())
        with _APPEND_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        _sweep_old_files(directory, stamp.date())
    except Exception:
        logger.debug("provider_health record failed", exc_info=True)


def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    """First-party observer: project hook events into the ledger."""
    if hook_name not in HANDLED_HOOKS or not enabled():
        return
    if hook_name == "post_api_request":
        _record(
            kwargs.get("provider"),
            kwargs.get("api_request_id"),
            "ok",
            model=kwargs.get("model"),
        )
    elif hook_name == "api_request_error":
        error = kwargs.get("error")
        error_type = error.get("type") if isinstance(error, dict) else None
        _record(
            kwargs.get("provider"),
            kwargs.get("api_request_id"),
            "err",
            model=kwargs.get("model"),
            status_code=kwargs.get("status_code"),
            error_type=error_type,
        )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _iter_window_lines(
    window_hours: float,
    now: datetime,
    home: Optional[Path] = None,
):
    """Yield parsed ledger lines whose ``ts`` falls inside the window."""
    cutoff = now.timestamp() - max(0.0, window_hours) * 3600.0
    directory = _ledger_dir(home)
    if not directory.is_dir():
        return
    # A window of <= 24h touches at most today's and yesterday's file, but
    # iterate by parsed date so larger windows stay correct.
    min_day = (now - timedelta(hours=window_hours)).date()
    for path in sorted(directory.iterdir()):
        day = _parse_day(path)
        if day is None or day < min_day:
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(line, dict):
                        continue
                    if float(line.get("ts") or 0.0) < cutoff:
                        continue
                    yield line
        except OSError:
            continue


def provider_drop_stats(
    window_hours: float = 24.0,
    *,
    now: Optional[datetime] = None,
    home: Optional[Path] = None,
) -> Dict[str, Dict[str, float]]:
    """Aggregate per-provider drop rate over the window.

    A logical request (distinct ``api_request_id`` per provider) counts once;
    it is *dropped* when its last recorded event inside the window is an
    error (retries exhausted / non-retryable), and *ok* when a success was
    recorded after the last error.  Returns
    ``{provider: {"total": int, "dropped": int, "drop_pct": float}}`` sorted
    by total descending.
    """
    moment = now or datetime.now()
    # (provider, request_id) -> last outcome, in append (chronological) order.
    last_outcome: Dict[tuple, str] = {}
    for line in _iter_window_lines(window_hours, moment, home):
        outcome = line.get("o")
        if outcome not in ("ok", "err"):
            continue
        key = (str(line.get("p") or "unknown"), str(line.get("r") or ""))
        if not key[1]:
            continue
        last_outcome[key] = outcome

    stats: Dict[str, Dict[str, float]] = {}
    for (prov, _req), outcome in last_outcome.items():
        bucket = stats.setdefault(prov, {"total": 0, "dropped": 0})
        bucket["total"] += 1
        if outcome == "err":
            bucket["dropped"] += 1
    for bucket in stats.values():
        total = bucket["total"]
        bucket["drop_pct"] = round(bucket["dropped"] / total * 100.0, 1) if total else 0.0
    return dict(sorted(stats.items(), key=lambda kv: (-kv[1]["total"], kv[0])))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_pct(pct: float) -> str:
    text = f"{pct:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text


def format_provider_health_lines(
    stats: Dict[str, Dict[str, float]],
    *,
    max_providers: int = _MAX_PROVIDERS_SHOWN,
) -> str:
    """Render the localized multi-line ``/status`` block body."""
    from agent.i18n import t

    if not stats:
        return (
            t("gateway.status.provider_health_header")
            + "\n"
            + t("gateway.status.provider_health_none")
        )
    items = list(stats.items())
    lines = [t("gateway.status.provider_health_header")]
    for prov, bucket in items[:max_providers]:
        lines.append(
            t(
                "gateway.status.provider_health_line",
                provider=prov,
                pct=_fmt_pct(bucket.get("drop_pct", 0.0)),
                dropped=int(bucket.get("dropped", 0)),
                count=int(bucket.get("total", 0)),
            )
        )
    hidden = len(items) - max_providers
    if hidden > 0:
        lines.append(t("gateway.status.provider_health_more", count=hidden))
    return "\n".join(lines)


__all__ = [
    "HANDLED_HOOKS",
    "enabled",
    "handles_hook",
    "observe_lifecycle",
    "provider_drop_stats",
    "format_provider_health_lines",
]
