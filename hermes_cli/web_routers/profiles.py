"""Profiles dashboard routes.

Two routers because route order matters: ``sessions_router`` (/api/profiles/sessions*,
projects/tree, pull-requests) was registered long before the generic
``/api/profiles/{name}`` routes on ``router``; the original global registration order is
preserved rather than relying on Starlette's literal-before-param matching.

Shared helpers are reached via the late-binding seam in :mod:`hermes_cli.web_deps`
so a test's ``monkeypatch.setattr(<owning module>, "_helper", ...)`` keeps working.
"""

import asyncio
import contextlib
import copy
import functools
from hermes_cli.web_read_coalescing import coalesced_read
import inspect
import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request

import hermes_acl_profiles as acl_profiles
from hermes_cli.session_listing import subagent_listing_scope
from hermes_cli.web_deps import late
from hermes_cli.config import get_process_hermes_home
from hermes_cli.profiles import ProfileIdentitySettlementPending
from hermes_cli.web_server_config import (
    _apply_main_model_assignment, _normalize_main_model_assignment, _validated_main_model_selection,
)
from hermes_cli.web_server_gateway import _strip_session_list_rows
from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK, config_scoped_to_thread
from hermes_cli.web_server_profiles import (
    _fallback_profile_dicts, _hub_action_name, _write_profile_mcp_servers,
)
from hermes_cli.web_server_sessions import _open_session_db_at_path
from hermes_state_health import STORAGE_CORRUPT, note_storage_error, storage_state
from starlette.concurrency import run_in_threadpool
from hermes_cli.web_models import (
    ProfileCreate, ProfileActiveUpdate, ProfileExport, ProfileImport, ProfileRename,
    ProfileSoulUpdate, ProfileSoulQuestionnaire, ProfileDescriptionUpdate, ProfileModelUpdate, ProfileDescribeAuto,
    SessionPrScanBody)
from hermes_cli.web_server_profiles import _config_profile_scope, _hermes_home_scope

# Same logger the handlers used before extraction (identical logger object).
_log = logging.getLogger("hermes_cli.web_server")

# A per-profile read failure is not an empty session list. The sidebar slice for
# that profile is a failed load (``failed`` + ``retry``) so Desktop can offer Retry
# instead of "No sessions yet". A successful read of zero rows still returns
# ``sessions: []``. Warn once per (profile, message) per process so a persistent
# failure is loud in errors.log without turning every sidebar poll into spam.
_profile_read_warned: set = set()


def _warn_profile_read_error(profile: str, exc: Exception) -> None:
    key = (profile, str(exc))
    if key in _profile_read_warned:
        return
    _profile_read_warned.add(key)
    _log.warning("profile session read failed for %r (reported only in the response "
                 "errors array): %s", profile, exc)

sessions_router = APIRouter()
router = APIRouter()

# Late-bound web_server helpers (resolved at call time; cycle-safe, monkeypatch-transparent).
_cron_profile_home = late("_cron_profile_home", "hermes_cli.web_server_cron")
_resolve_profile_dir = late("_resolve_profile_dir", "hermes_cli.web_server_profiles")
_spawn_hermes_action = late("_spawn_hermes_action", "hermes_cli.web_server_gateway")

# ---------------------------------------------------------------------------
# Profile management endpoints (minimal — list/create/rename/delete + SOUL.md)
# ---------------------------------------------------------------------------


def _profile_to_dict(info) -> dict[str, Any]:
    attr = functools.partial(getattr, info)
    return {
        "name": attr("name", ""), "path": str(attr("path", "")),
        "is_default": bool(attr("is_default", False)),
        "model": attr("model", None), "provider": attr("provider", None),
        "has_env": bool(attr("has_env", False)),
        "skill_count": int(attr("skill_count", 0) or 0),
        "gateway_running": bool(attr("gateway_running", False)),
        "description": attr("description", "") or "",
        "description_auto": bool(attr("description_auto", False)),
        "display_name": attr("display_name", "") or "",
        "bot_title": attr("bot_title", "") or "",
        "distribution_name": attr("distribution_name", None),
        "distribution_version": attr("distribution_version", None),
        "distribution_source": attr("distribution_source", None),
        "has_alias": attr("alias_path", None) is not None}


def _profile_setup_command(name: str) -> str:
    """Return the shell command used to configure a profile in the CLI."""
    _resolve_profile_dir(name)
    return "hermes setup" if name == "default" else f"{name} setup"


def _scope_profile_name(path: Path) -> Optional[str]:
    """Map a profile directory onto the query name ``_config_profile_scope`` expects: None for
    the process home (current-profile semantics: launch secret scope, no home override),
    ``"default"`` for the default root (its basename -- ``.hermes`` or a custom root -- is not a
    profile name; launched from ``profiles/<name>`` the root is a *different* profile), the
    directory name for ``profiles/<name>``."""
    from hermes_constants import get_default_hermes_root
    resolved = path.resolve()
    if resolved == get_process_hermes_home().resolve():
        return None
    if resolved == get_default_hermes_root().resolve():
        return "default"
    return path.name


def _write_profile_model(profile_dir: Path, provider: str, model: str, validate_in: Optional[Path] = None,
                         base_url: str = "") -> None:
    """Write the main model assignment into ``profile_dir``'s config.yaml (HERMES_HOME-scoped)
    through the same validated /model shape as ``POST /api/model/set``.

    ``validate_in`` is the home whose ``providers:``/``.env``/catalog vouch for the pick (default:
    ``profile_dir`` itself). Profile-create passes the dashboard's own home: the picker that offered
    the model read THAT catalog, and a just-created profile has no credentials yet, so validating
    in the empty profile rejected every non-env provider (anthropic, ollama, custom).

    Both spans enter ``_config_profile_scope`` (home + secret scope), matching ``/api/model/set``:
    once the dashboard has served a secondary profile, ``switch_model``'s ``key_env`` probe goes
    through ``get_secret``, which fails closed without a scope and reports the provider as
    unconnected (UnscopedSecretError class, #114676).

    ``base_url`` is the endpoint a bare ``custom``/``local`` pick binds to (a fresh profile's config
    names none); other providers ignore it."""
    from hermes_cli.config import load_config, save_config
    from hermes_cli.config_backend import refusing_locked_omissions
    with _config_profile_scope(_scope_profile_name(validate_in or profile_dir)):
        provider, model = _normalize_main_model_assignment(provider, model)
        result = _validated_main_model_selection(load_config(), provider, model, base_url)
    with _config_profile_scope(_scope_profile_name(profile_dir)), _CONFIG_MUTATION_LOCK:  # RMW span
        cfg = load_config()
        cfg["model"] = _apply_main_model_assignment(cfg.get("model", {}), result)
        with refusing_locked_omissions("model"):
            save_config(cfg)


def _disable_unselected_skills(profile_dir: Path, keep: list[str]) -> int:
    """Disable every installed skill in ``profile_dir`` not in ``keep``; returns how many were
    newly disabled. Profiles manage activation via a *disabled* list (everything installed is
    active by default); the builder's skill step has "replace" semantics. Hub skills are
    installed separately via subprocess and are active on install."""
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills
    keep_set = {s.strip() for s in keep if s and s.strip()}
    with _hermes_home_scope(profile_dir), _CONFIG_MUTATION_LOCK:  # RMW span
        skills_root = profile_dir / "skills"
        installed = ([md.parent.name for md in skills_root.rglob("SKILL.md")]
                     if skills_root.is_dir() else [])
        cfg = load_config()
        disabled = get_disabled_skills(cfg)
        newly = 0
        for name in installed:
            if name not in keep_set and name not in disabled:
                disabled.add(name)
                newly += 1
        if newly:
            save_disabled_skills(cfg, disabled)
    return newly

# Returned by the offloaded file readers below to mean "the file is not there", which a plain
# ``None`` cannot express: ``desktop.json`` may legitimately hold the document ``null``.
_MISSING = object()


def _questionnaire_path(profile_dir: Path) -> str:
    """The questionnaire's on-disk path when the profile has one, else ``""``.

    Only a personal root is seeded with it (``hermes_acl_soul``); reporting the path for any
    profile would tell a client to open a file that does not exist.
    """
    try:
        from hermes_acl_soul import QUESTIONNAIRE_FILENAME

        path = Path(profile_dir) / QUESTIONNAIRE_FILENAME
        return str(path) if path.is_file() else ""
    except Exception:  # health: allow BLE001 -- an advisory field must not 500 the create
        _log.debug("questionnaire path lookup failed", exc_info=True)
        return ""


@contextlib.contextmanager
def _profile_errors(log_msg: str, *args, not_found=(FileNotFoundError,),
                    bad_request=(ValueError,)):
    """Map hermes_cli.profiles exceptions to HTTP: ``not_found`` -> 404, ``bad_request`` -> 400
    (in that order), anything else is logged with ``log_msg`` -> 500. HTTPException passes, and so
    does ``ProfileIdentitySettlementPending`` — a typed partial success the calling endpoint (the
    delete route) owns; it must not flatten into the generic 500."""
    try:
        yield
    except HTTPException:
        raise
    except not_found as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bad_request as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ProfileIdentitySettlementPending:
        raise
    except Exception as e:
        _log.exception(log_msg, *args)
        raise HTTPException(status_code=500, detail=str(e))


async def _read_off_loop(read, label: str, errors):
    """``read()`` on a worker thread; ``errors`` become ``500 "Could not read <label>: e"``."""
    try:
        return await run_in_threadpool(read)
    except errors as e:
        raise HTTPException(status_code=500, detail=f"Could not read {label}: {e}")


def _best_effort(log_msg: str, *args, fn, default=None):
    """Run ``fn()``; on failure log ``log_msg`` with traceback and return ``default`` (the
    parent operation already succeeded, so never 500)."""
    try:
        return fn()
    except Exception:
        _log.exception(log_msg, *args)
        return default


def _profile_targets(log_label: str) -> list[tuple[str, Path]]:
    """(name, home) for every profile, falling back to ``default`` alone. Uses
    ``profiles_to_serve`` (pure directory read) instead of ``list_profiles``, which parses
    config/meta and probes gateways per profile — every caller here is a polled sidebar
    fan-out that only needs name/path (#114041)."""
    from hermes_cli import profiles as profiles_mod
    try:
        targets = list(profiles_mod.profiles_to_serve(multiplex=True, include_standalone=True, include_parked=True))
    except Exception:
        _log.exception("%s: profile enumeration failed", log_label)
        targets = []
    if not targets:
        targets.append(("default", profiles_mod.get_profile_dir("default")))
    return targets


def _visible_targets(request: Optional[Request], log_label: str) -> list[tuple[str, Path]]:
    """``_profile_targets`` restricted to the profiles this request may use (МПД Т2).

    The fan-out routes (sessions list/sidebar/projects tree/pull-requests) open EVERY profile's
    ``state.db``; a second person must not reach a profile that is not theirs. With
    ``acl.multi_user`` off (or for the host owner) the target list is returned untouched.
    """
    targets = _profile_targets(log_label)
    if request is None:
        return targets
    visible = acl_profiles.scope_for(request, [name for name, _home in targets])
    if visible is None:
        return targets
    allowed = set(visible)
    return [(name, home) for name, home in targets if name in allowed]


def _scoped_profile_targets(log_label: str, profile: Optional[str]) -> list[tuple[str, Path]]:
    """Every profile, or only ``profile``: a native grant may read just the profile it names,
    so the Desktop assembles the all-profiles view from one scoped read per profile."""
    scope = (profile or "all").strip() or "all"
    targets = _profile_targets(log_label)
    return targets if scope == "all" else [(name, home) for name, home in targets if name == scope]


def _tag_rows(rows: list[dict[str, Any]], name: str, now: float) -> list[dict[str, Any]]:
    """Stamp session rows with their owning profile and the 300s active heuristic; SQLite
    stores flags as 0/1 and the sidebar needs booleans."""
    for s in rows:
        s["profile"] = name
        s["is_default_profile"] = name == "default"
        s["is_active"] = (
            s.get("ended_at") is None and (now - s.get("last_active", s.get("started_at", 0))) < 300
        )
        s["archived"] = bool(s.get("archived"))
        s["pinned"] = bool(s.get("pinned"))
    return rows


def _pinned_window(rows: list[dict[str, Any]], offset: int, cap: int) -> list[dict[str, Any]]:
    """``rows[offset:offset+cap]`` plus every pinned row past it: the per-profile queries
    back-fill pinned rows past their LIMIT, so truncating on recency alone would drop them."""
    window = rows[offset:offset + cap]
    if len(rows) > offset + cap:
        seen = {id(s) for s in window}
        window.extend(s for s in rows[offset + cap:] if s.get("pinned") and id(s) not in seen)
    return window


def _recency(s: dict[str, Any]) -> Any:
    return s.get("last_active") or s.get("started_at") or 0


def _read_profile_db(name: str, home, errors: Optional[list[dict[str, str]]],
                     fn: Callable[[Any], Any]) -> Any:
    """``fn(db)`` against the profile's read-only state.db; None when the file is missing,
    the open fails or ``fn`` raises (warned once, recorded in ``errors`` when given).

    Read-only on the healthy path: this runs on every sidebar refresh, so it must not
    routinely DDL/write-lock another profile's live DB. A store that predates a schema addition
    gets the missing declared tables/columns ONCE from the open helper, and only while no gateway
    owns it; an owned store is reported in ``errors`` with the restart remedy (#140315)."""
    db_path = Path(home) / "state.db"
    if not db_path.exists():
        return None
    db = None
    try:
        db = _open_session_db_at_path(db_path, read_only=True)
        return fn(db)
    except Exception as exc:
        # An open that dies on a damaged file never reaches SessionDB's read helpers.
        note_storage_error(db_path, exc)
        _warn_profile_read_error(name, exc)
        if errors is not None:
            errors.append({"profile": name, "error": str(exc)})
        return None
    finally:
        if db is not None:
            db.close()


def _corrupt_profile_stores(targets) -> dict[str, str]:
    """``{profile: "corrupt"}`` for every scanned profile whose state.db this process has latched
    as structurally corrupt (``hermes_state_health``). Lets Desktop tell an empty or partial list
    from a damaged store, including when some reads still succeed (#72046)."""
    return {name: STORAGE_CORRUPT for name, home in targets
            if storage_state(Path(home) / "state.db") == STORAGE_CORRUPT}


# Sidebar scan cache TTL: short enough that the UI never shows meaningfully stale data, long
# enough to coalesce the desktop's reconnect/focus/change poll bursts into one scan.
_SIDEBAR_CACHE_TTL_SECONDS = 5.0
_SIDEBAR_CACHE_MAX_ENTRIES = 32
_SIDEBAR_PROFILE_CACHE_MAX_ENTRIES = 256
_SIDEBAR_PROFILE_CACHE = OrderedDict()
_SIDEBAR_PROFILE_CACHE_LOCK = threading.Lock()


def _stat_fingerprint(path: Path):
    """Return identity + mutation metadata without opening the file."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _sidebar_db_fingerprint(db_path: Path):
    """Track SQLite content changes through the main DB and its WAL."""
    return (_stat_fingerprint(db_path), _stat_fingerprint(Path(f"{db_path}-wal")))


def _sidebar_profile_cache_get(key):
    with _SIDEBAR_PROFILE_CACHE_LOCK:
        value = _SIDEBAR_PROFILE_CACHE.get(key)
        if value is None:
            return None
        _SIDEBAR_PROFILE_CACHE.move_to_end(key)
        return copy.deepcopy(value)


def _sidebar_profile_cache_put(key, value):
    db_path, fingerprint = key[:2]
    snapshot = copy.deepcopy(value)
    with _SIDEBAR_PROFILE_CACHE_LOCK:
        # A changed DB/WAL obsoletes every older parameter variant for that profile; drop
        # them eagerly rather than waiting for LRU pressure.
        for existing in [k for k in _SIDEBAR_PROFILE_CACHE
                         if k[0] == db_path and k[1] != fingerprint]:
            _SIDEBAR_PROFILE_CACHE.pop(existing, None)
        _SIDEBAR_PROFILE_CACHE[key] = snapshot
        _SIDEBAR_PROFILE_CACHE.move_to_end(key)
        while len(_SIDEBAR_PROFILE_CACHE) > _SIDEBAR_PROFILE_CACHE_MAX_ENTRIES:
            _SIDEBAR_PROFILE_CACHE.popitem(last=False)


def _sidebar_profile_cache_clear():
    with _SIDEBAR_PROFILE_CACHE_LOCK:
        _SIDEBAR_PROFILE_CACHE.clear()


def _profile_state_db(home) -> Path:
    return Path(home) / "state.db"


def _slice_has_rows(slices: dict[str, Any]) -> bool:
    return any(slices.get(key) for key in ("recents", "cron", "messaging"))


def _retryable_profile_errors(errors: list[dict[str, str]], scanned) -> list[dict[str, str]]:
    """Scan failures Retry can re-attempt. A latched corrupt store has its own notice."""
    corrupt = set(_corrupt_profile_stores(scanned))
    return [dict(err) for err in errors if err.get("profile") not in corrupt]


def _failed_load_slice(errors: list[dict[str, str]], **extra) -> dict[str, Any]:
    """Failed load: no ``sessions`` key. An empty list would read as data loss."""
    return {"failed": True, "retry": True, "errors": [dict(err) for err in errors], **extra}


def _profiles_failed(errors: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    return {
        err["profile"]: {"failed": True, "retry": True, "error": err.get("error", "")}
        for err in errors if err.get("profile")
    }


def _sidebar_singleflight_cache(func):
    """Coalesce concurrent sidebar scans and briefly reuse their response.

    Every uncached refresh opens every profile database; desktop poll bursts overlap identical
    scans in AnyIO worker threads, starving the uvicorn loop for the GIL. The TTL bounds UI
    staleness; the single-flight lock guarantees one expensive scan at a time. Cached values
    are copied on store and hit so serialization or a caller cannot mutate shared state.
    """
    signature = inspect.signature(func)
    cache = OrderedDict()
    cache_lock = threading.Lock()
    refresh_lock = threading.Lock()
    miss = object()

    def _lookup(key):
        now = time.monotonic()
        with cache_lock:
            item = cache.get(key)
            if item is None or now >= item[0]:
                cache.pop(key, None)
                return miss
            cache.move_to_end(key)
            return copy.deepcopy(item[1])

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        ttl = _SIDEBAR_CACHE_TTL_SECONDS
        if ttl <= 0:
            return func(*args, **kwargs)

        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        key = tuple(bound.arguments.items())
        cached = _lookup(key)
        if cached is not miss:
            return cached

        # A plain Lock is intentional: FastAPI executes this sync handler in the AnyIO
        # worker pool, so contenders sleep without holding the GIL.
        with refresh_lock:
            cached = _lookup(key)
            if cached is not miss:
                return cached
            result = func(*args, **kwargs)
            # A 200 carrying errors[] is a FAILED profile scan, not a successful empty page.
            # Caching it would hold the empty recents in front of a store that has already
            # recovered, for the whole TTL.
            if isinstance(result, dict) and result.get("errors"):
                return result
            try:
                snapshot = copy.deepcopy(result)
            except Exception:
                _log.exception("sidebar response could not be cached")
                return result
            with cache_lock:
                cache[key] = (time.monotonic() + ttl, snapshot)
                cache.move_to_end(key)
                while len(cache) > _SIDEBAR_CACHE_MAX_ENTRIES:
                    cache.popitem(last=False)
            return result

    return wrapped


def _csv_list(value: Optional[str]) -> list[str]:
    return [s.strip() for s in (value or "").split(",") if s.strip()]


@sessions_router.get("/api/profiles/sessions")
def get_profiles_sessions(
    request: Request,
    # ``le=500`` caps the page size — this endpoint fans out across EVERY profile's state.db.
    # 500 (not 100) because desktop callers use limit=200 and the electron remote-merge
    # over-fetches ``limit + offset``.
    limit: int = Query(20, ge=0, le=500), offset: int = Query(0, ge=0), min_messages: int = 0,
    archived: str = "exclude", order: str = "recent", profile: str = "all",
    source: str | None = None, sources: str | None = None, exclude_sources: str | None = None,
    full: bool = False):
    """Unified, read-only session list aggregated across ALL profiles: opens each profile's
    ``state.db`` directly (no dashboard backend per profile) and tags rows with their owning
    ``profile``. Rows omit ``system_prompt`` / ``model_config`` unless ``full=1`` — same
    projection as ``/api/sessions``.

    ``all`` means "every profile THIS caller may use" (МПД Т2): the fan-out is over the visible
    set, and a ``?profile=`` naming a profile outside it is refused like an unknown one."""
    if archived not in ("exclude", "only", "include"):
        raise HTTPException(status_code=400, detail="archived must be one of: exclude, only, include")
    if order not in ("created", "recent"):
        raise HTTPException(status_code=400, detail="order must be one of: created, recent")

    if profile and profile != "all":
        acl_profiles.require_visible(request, profile)
        targets = [_cron_profile_home(profile)]
    else:
        targets = _visible_targets(request, "GET /api/profiles/sessions")

    # Source scoping (see /api/sessions): recents pass exclude_sources=cron, the cron-jobs
    # section source=cron — two independent lists so cron sessions can't starve recents.
    filters = dict(
        source=source or None, sources=_csv_list(sources) or None,
        exclude_sources=_csv_list(exclude_sources) or None, min_message_count=max(0, min_messages),
        include_archived=archived == "include", archived_only=archived == "only")
    # Over-fetch per profile so the merged+sorted window is correct for the requested page.
    # ``limit`` is already bounded to 500 by FastAPI; the offset is caller-controlled
    # pagination state, so include it instead of capping the source window at 500.
    # Otherwise page 3 of a 577-row profile is permanently empty even though ``total``
    # reports the remaining rows.
    per_profile = limit + offset

    merged: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    now = time.time()
    # МПД Т2/Т4: a profile in the visible set is a runtime, not a container of one person's rows.
    # "May use profile 03" therefore never means "may read somebody else's conversation in it" —
    # rows keep their own owner scope from step 4. ``None`` (acl off) keeps the old query.
    owner_scope = _request_scope(request)
    for name, home in targets:
        def _read(db, name=name, home=home):
            include_subagents, exclude = subagent_listing_scope(
                home, source=filters["source"], sources=filters["sources"],
                exclude_sources=filters["exclude_sources"])
            scoped = {**filters, "exclude_sources": exclude, "include_subagents": include_subagents,
                      "user_id": owner_scope}
            rows = db.list_sessions_rich(
                limit=per_profile, offset=0, order_by_last_active=order == "recent",
                # Same SQL-level blob skip as /api/sessions.
                compact_rows=not full, include_pinned=True, **scoped)
            totals[name] = db.session_count(exclude_children=True, **scoped)
            merged.extend(_tag_rows(rows, name, now))
        _read_profile_db(name, home, errors, _read)

    sort_key = "last_active" if order == "recent" else "started_at"
    merged.sort(key=lambda s: s.get(sort_key) or s.get("started_at") or 0, reverse=True)
    if archived == "only":
        # Pinned live chats back-fill this view, but cannot consume positions
        # in its archived-only cursor (whose total counts only archived rows).
        window = _pinned_window([s for s in merged if s.get("archived")], offset, limit)
        # Keep backfill after the window: clients retain only limit rows plus pins.
        window.extend(s for s in merged if s.get("pinned") and not s.get("archived"))
    else:
        window = _pinned_window(merged, offset, limit)
    if not full:
        _strip_session_list_rows(window)
    return {"sessions": window, "total": sum(totals.values()), "profile_totals": totals,
            "limit": limit, "offset": offset, "errors": errors,
            "storage": _corrupt_profile_stores(targets)}


@sessions_router.get("/api/profiles/sessions/sidebar")
def get_profiles_sessions_sidebar(
    request: Request,
    recents_profile: str = "all", recents_limit: int = 20, recents_exclude: str | None = None,
    cron_limit: int = 50, messaging_limit: int = 100, messaging_exclude: str | None = None):
    """Batched sidebar session slices (recents / cron / messaging) — one profile-DB open per
    refresh instead of three ``/api/profiles/sessions`` calls. Same row projection and 300s
    active heuristic as the per-slice endpoint; all slices use ``min_messages=1`` /
    ``archived=exclude`` / recency order.

    ``recents_profile`` scopes the WHOLE payload, not just recents — the sidebar has one
    scope, so a concrete profile must never show another profile's Telegram threads or
    cronjobs; ``all`` asks for everything THIS caller may use (МПД Т2).

    See #42651, #65710, #70629.
    """
    scope = (recents_profile or "all").strip() or "all"
    if scope != "all":
        acl_profiles.require_visible(request, scope)
    visible = acl_profiles.scope_for(request, [name for name, _home in _profile_targets("sidebar")])
    # The cache below keys on its ARGUMENTS, so the caller's visible profile set is one of them:
    # two people sharing a process must never be answered from each other's cached payload.
    return _sidebar_payload(
        recents_profile=recents_profile, recents_limit=recents_limit,
        recents_exclude=recents_exclude, cron_limit=cron_limit,
        messaging_limit=messaging_limit, messaging_exclude=messaging_exclude,
        visible=None if visible is None else tuple(visible), owner_scope=_request_scope(request))


def _request_scope(request: Optional[Request]) -> Optional[tuple[str, ...]]:
    """The caller's session owner stamps (``None`` = unscoped rows, the pre-МПД answer)."""
    if request is None:
        return None
    from hermes_acl_scope import request_owner_scope
    scope = request_owner_scope(request)
    return None if scope is None else tuple(scope)


@_sidebar_singleflight_cache
def _sidebar_payload(
    recents_profile: str = "all", recents_limit: int = 20, recents_exclude: str | None = None,
    cron_limit: int = 50, messaging_limit: int = 100, messaging_exclude: str | None = None,
    visible: Optional[tuple[str, ...]] = None, owner_scope: Optional[tuple[str, ...]] = None):
    """The cached body of :func:`get_profiles_sessions_sidebar` (see it for the contract)."""
    targets = _profile_targets("GET /api/profiles/sessions/sidebar")
    if visible is not None:
        allowed = set(visible)
        targets = [(name, home) for name, home in targets if name in allowed]

    recents_scope = (recents_profile or "all").strip() or "all"
    recents_exclude_list = [s for s in (recents_exclude or "").split(",") if s.strip()]
    messaging_exclude_list = [s for s in (messaging_exclude or "").split(",") if s.strip()]
    # (source, exclude) per slice; ``source=cron`` is the implicit cron taxonomy.
    slice_scope = {"recents": (None, recents_exclude_list), "cron": ("cron", None),
                   "messaging": (None, messaging_exclude_list)}
    cap = {"recents": min(max(recents_limit, 1), 500), "cron": min(max(cron_limit, 1), 500),
           "messaging": min(max(messaging_limit, 1), 500)}
    rows: dict[str, list[dict[str, Any]]] = {k: [] for k in slice_scope}
    recents_truncated: dict[str, bool] = {}
    profile_totals: dict[str, dict[str, float]] = {}
    errors: list[dict[str, str]] = []
    now = time.time()

    def _slice(db, key, recents_subagents=(False, None)):
        source, exclude = slice_scope[key]
        # Only recents takes subagent runs (sessions.show_subagents); cron/messaging keep shape.
        include_subagents, exclude = recents_subagents if key == "recents" else (False, exclude)
        # include_pinned: a pinned conversation must reach the sidebar even when it has aged
        # past the window, or its Pinned row renders empty.
        return db.list_sessions_rich(
            source=source, exclude_sources=exclude or None, limit=cap[key], offset=0,
            min_message_count=1, include_archived=False, archived_only=False,
            order_by_last_active=True, compact_rows=True, include_pinned=True,
            include_subagents=include_subagents,
            # МПД Т2/Т4: a SHARED profile is a runtime several people use, so its rows are still
            # scoped to their own owner stamp — "may use profile 03" never means "may read
            # somebody else's conversation in it". ``None`` (acl off) keeps the old query.
            user_id=owner_scope)

    def _build_slices(db, cache_key, recents_subagents):
        # ``usage`` is aggregated in SQL rather than over the recents window: the window is a
        # page, and a total that shrank when you scrolled would be worse than no total at all.
        slices = {"recents": _slice(db, "recents", recents_subagents),
                  "usage": db.usage_totals(user_id=owner_scope),
                  "cron": _slice(db, "cron"), "messaging": _slice(db, "messaging")}
        _sidebar_profile_cache_put(cache_key, slices)
        return slices

    scanned = []
    contributed: set = set()
    for name, home in targets:
        if recents_scope != "all" and name != recents_scope:
            continue
        scanned.append((name, home))
        db_path = _profile_state_db(home)
        if not db_path.exists():
            continue
        recents_subagents = subagent_listing_scope(home, exclude_sources=recents_exclude_list or None)
        profile_cache_key = (str(db_path), _sidebar_db_fingerprint(db_path), cap["recents"],
                             tuple(recents_exclude_list), cap["cron"], cap["messaging"],
                             tuple(messaging_exclude_list), recents_subagents[0],
                             # The cache is per store, so the caller's row scope is part of its
                             # identity: two people must not read each other's cached slices.
                             owner_scope)
        slices = _sidebar_profile_cache_get(profile_cache_key)
        if slices is None:
            slices = _read_profile_db(
                name, home, errors,
                lambda db: _build_slices(db, profile_cache_key, recents_subagents))
            if slices is None:
                continue
        if _slice_has_rows(slices):
            contributed.add(name)
        # A full window means more rows remain on disk — all "load more" needs, at no cost
        # beyond the rows already read. Pinned rows count: they occupy LIMIT slots, and a
        # short list has nothing past the page for the pin back-fill to add, so pins cannot
        # fake a full page (#81484).
        recents_truncated[name] = len(slices["recents"]) >= cap["recents"]
        profile_totals[name] = slices["usage"]
        for key in slice_scope:
            rows[key].extend(_tag_rows(slices[key], name, now))

    def _window(key: str) -> list[dict[str, Any]]:
        rows[key].sort(key=_recency, reverse=True)
        win = _pinned_window(rows[key], 0, cap[key])
        _strip_session_list_rows(win)
        return win

    storage = _corrupt_profile_stores(scanned)
    retryable = _retryable_profile_errors(errors, scanned)
    failed_names = {err["profile"] for err in retryable if err.get("profile")}
    corrupt = set(storage)
    live = [name for name, _home in scanned if name not in corrupt]
    scoped_failed = (
        recents_scope != "all"
        and recents_scope in failed_names
        and recents_scope not in contributed
    )
    all_failed = (
        recents_scope == "all"
        and bool(live)
        and not contributed
        and all(name in failed_names for name in live)
    )
    if scoped_failed or all_failed:
        # The whole answer is this profile's (or every profile's) failed scan.
        # Do not include ``sessions``: an empty list is a successful read.
        return {
            "recents": _failed_load_slice(
                retryable, profiles_truncated={}, profiles_usage={}),
            "cron": _failed_load_slice(retryable),
            "messaging": _failed_load_slice(retryable, total=0),
            "errors": errors, "storage": storage}

    body = {
        "recents": {"sessions": _window("recents"), "profiles_truncated": recents_truncated,
                    "profiles_usage": profile_totals},
        "cron": {"sessions": _window("cron")},
        "messaging": {"sessions": _window("messaging"), "total": len(rows["messaging"])},
        "errors": errors, "storage": storage}
    # A sibling profile still listed does not make the failed profile's absence
    # a successful empty slice. Stamp that profile as a failed load with Retry.
    failed = _profiles_failed(retryable)
    if failed:
        body["profiles_failed"] = failed
        for key in ("recents", "cron", "messaging"):
            body[key]["profiles_failed"] = failed
            body[key]["errors"] = [dict(err) for err in retryable]
    return body


def _merge_by_id(into: dict[str, dict[str, Any]], entries: list[dict[str, Any]], child_key: str) -> None:
    """Fold ``entries`` into ``into`` by id, recursing through one child list (repos merge
    their lanes, lanes merge their sessions). Counts add up; everything else is
    first-writer, since the entries describe the same path either way."""
    for entry in entries:
        existing = into.get(entry["id"])
        if existing is None:
            into[entry["id"]] = entry
            continue
        if child_key == "sessions":
            existing["sessions"].extend(entry.get("sessions") or [])
        else:
            children: dict[str, dict[str, Any]] = {c["id"]: c for c in existing.get(child_key) or []}
            _merge_by_id(children, entry.get(child_key) or [], "sessions")
            existing[child_key] = list(children.values())
        if "sessionCount" in existing:
            existing["sessionCount"] = (existing.get("sessionCount") or 0) + (entry.get("sessionCount") or 0)


def _merge_profile_tree(
    merged: dict[str, dict[str, Any]], projects: list[dict[str, Any]], profile: str,
    preview_limit: int) -> None:
    """Fold one profile's projects into the shared tree, keyed by folder: the same checkout
    in two profiles is one group, as is ``__no_project__`` (else one "Home" per profile), and
    a declared project (``p_<hash>``) folds with another profile's auto entry for the same
    folder. Sessions carry the owning profile; a group header never claims a single owner."""
    for project in projects:
        lane_sessions = (s for r in project.get("repos") or []
                         for lane in r.get("groups") or []
                         for s in lane.get("sessions") or [])
        for session in [*lane_sessions, *(project.get("previewSessions") or [])]:
            session["profile"] = profile
            session["is_default_profile"] = profile == "default"

        # Same folder-identity notion the per-profile tree builder uses (``_path_key``):
        # shape-derived — Windows paths case-fold on any host, separators unify, NFC —
        # so the cross-profile merge agrees by construction with the trees it merges.
        from tui_gateway.project_tree import _path_key
        key = _path_key(project.get("path") or project["id"])
        # A per-profile read is merged again by its caller, which cannot compute this key.
        project["mergeKey"] = key
        existing = merged.get(key)
        if existing is None:
            merged[key] = project
            continue

        # A declared project carries the label, color and icon the user chose, so it wins
        # the identity when it meets another profile's auto entry.
        if existing.get("isAuto") and not project.get("isAuto"):
            existing, project = project, existing
            merged[key] = existing

        repos: dict[str, dict[str, Any]] = {r["id"]: r for r in existing.get("repos") or []}
        _merge_by_id(repos, project.get("repos") or [], "groups")
        existing["repos"] = list(repos.values())
        for total_key in ("sessionCount", "totalTokens", "totalCostUsd"):
            existing[total_key] = (existing.get(total_key) or 0) + (project.get(total_key) or 0)
        existing["lastActive"] = max(existing.get("lastActive") or 0, project.get("lastActive") or 0)
        previews = (existing.get("previewSessions") or []) + (project.get("previewSessions") or [])
        previews.sort(key=_recency, reverse=True)
        existing["previewSessions"] = previews[:preview_limit]


@sessions_router.get("/api/profiles/projects/tree")
def get_profiles_projects_tree(request: Request, preview_limit: int = 3, session_limit: int = 2000,
                               profile: Optional[str] = None):
    """Project tree for every profile at once, for the all-profiles sidebar.

    ``projects.tree`` over JSON-RPC answers for the backend's own profile only; this runs the
    same builder once per profile against its ``state.db``, scoping the other inputs
    (projects.db, repo-scan policy, junk filters) through the home override. Discovery is
    off: a repo with zero sessions is the same repo in every profile (the disk scan would
    multiply empty lanes by the profile count), and discovery is the one part of the builder
    that writes (policy reconciliation), which a read-only fan-out must not do.

    ``profile`` limits the scan to that one profile: a native grant may read only its own, so
    the Desktop merges one such read per profile by each project's ``mergeKey``. A profile
    outside the caller's set is refused like an unknown one (МПД Т2), and the rows inside a
    visible profile keep their own owner scope — a shared profile is a runtime, not a folder
    of one person's conversations.
    """
    scope = (profile or "").strip()
    if scope:
        acl_profiles.require_visible(request, scope)
    visible = acl_profiles.scope_for(request, [name for name, _home in _profile_targets("projects/tree")])
    return _projects_tree_payload(
        preview_limit=preview_limit, session_limit=session_limit, profile=profile or None,
        visible=None if visible is None else tuple(visible), owner_scope=_request_scope(request))


@_sidebar_singleflight_cache
def _projects_tree_payload(preview_limit: int = 3, session_limit: int = 2000,
                           profile: Optional[str] = None, visible: Optional[tuple[str, ...]] = None,
                           owner_scope: Optional[tuple[str, ...]] = None):
    """The cached body of :func:`get_profiles_projects_tree` (see it for the contract)."""
    from tui_gateway import server as gateway_server
    merged: dict[str, dict[str, Any]] = {}
    scoped_session_ids: list[str] = []
    errors: list[dict[str, str]] = []

    targets = _scoped_profile_targets("GET /api/profiles/projects/tree", profile)
    if visible is not None:
        allowed = set(visible)
        targets = [(name, home) for name, home in targets if name in allowed]
    for name, home in targets:
        def _read(db, name=name, home=home):
            with _hermes_home_scope(home):
                tree, _active_id = gateway_server._build_project_tree(
                    db, preview_limit=preview_limit, hydrate=False,
                    session_limit=session_limit, include_discovered=False,
                    user_id=owner_scope)
                _merge_profile_tree(merged, tree["projects"], name, preview_limit)
                scoped_session_ids.extend(tree["scoped_session_ids"])
        _read_profile_db(name, home, errors, _read)

    # active_id is None: ownership is per profile, so no project is "the active one" here.
    projects = sorted(merged.values(), key=lambda p: p.get("lastActive") or 0, reverse=True)
    return {"projects": projects, "active_id": None, "scoped_session_ids": scoped_session_ids,
            "errors": errors}


# `gh pr create` prints the PR url and nothing else, so a tool result whose whole output IS a
# PR url means this session opened that PR; a url inside prose is a session TALKING about one.
_PR_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)/?$")


def _pr_url_from_tool_output(content: str) -> Optional[tuple[int, str]]:
    """The (number, url) a tool result announces, or None."""
    try:
        output = (json.loads(content) or {}).get("output")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
    if not isinstance(output, str):
        return None
    match = _PR_URL_RE.match(output.strip())
    return (int(match.group(1)), match.group(0)) if match else None


@sessions_router.post("/api/profiles/sessions/pull-requests")
def post_profiles_sessions_pull_requests(body: SessionPrScanBody, request: Request,
                                         profile: Optional[str] = None):
    """The PR each of these sessions opened, recovered from its own transcript: a session
    that starts in the main checkout and works in a worktree has no branch of its own, so
    its PR is invisible to the branch join — but ``gh pr create`` ran in the conversation
    (see ``_pr_url_from_tool_output``). Read-only across every profile THIS caller may use."""
    wanted = list(dict.fromkeys(s for s in (body.ids or []) if s))[:2000]
    if not wanted:
        return {"pull_requests": {}, "scanned": []}
    scope = (profile or "all").strip() or "all"
    if scope != "all":
        acl_profiles.require_visible(request, scope)

    found: dict[str, dict[str, Any]] = {}

    def _read(db):
        for pr in db.find_pr_url_messages(wanted):
            parsed = _pr_url_from_tool_output(pr["content"])
            if parsed:
                # Oldest-first, so a later `gh pr create` (the replacement PR) wins.
                found[pr["session_id"]] = {"number": parsed[0], "url": parsed[1]}

    if scope == "all":
        # Only the profiles this caller may use: a foreign session id must not be answered
        # with its PR — the transcript scan would otherwise confirm a session exists (МПД Т2).
        targets = _visible_targets(request, "POST /api/profiles/sessions/pull-requests")
    else:
        targets = [(name, home) for name, home in _profile_targets("pull-requests") if name == scope]
    for name, home in targets:
        _read_profile_db(name, home, None, _read)

    # ``scanned``: every id looked at, so the caller can remember "nothing there".
    return {"pull_requests": found, "scanned": wanted}


@functools.partial(coalesced_read, thread_runner=lambda func: run_in_threadpool(func))
def _read_profiles():
    from hermes_cli import profiles as profiles_mod
    try:
        # Polled by the Desktop on every focus/roster tick: never walk skill trees in-request.
        profiles = profiles_mod.list_profiles(lazy_skill_count=True)
        return {"profiles": [_profile_to_dict(p) for p in profiles]}
    except Exception:
        _log.exception("GET /api/profiles failed; falling back to profile directory scan")
        return {"profiles": _fallback_profile_dicts(profiles_mod)}


def _scoped_profile_view(payload: dict[str, Any], subject: Optional[str]) -> dict[str, Any]:
    """Drop the profiles *subject* may not see from a ``GET /api/profiles`` payload.

    Scoping is off (or nothing to filter): the payload is returned untouched, so the coalesced
    read above is shared by every caller exactly as before. Only the filtered case is per-caller.
    """
    rows = payload.get("profiles") or []
    visible = acl_profiles.scope_for_names(subject, [str(row.get("name") or "") for row in rows])
    if visible is None:
        return payload
    kept = set(visible)
    return {**payload, "profiles": [row for row in rows if str(row.get("name") or "") in kept]}


@router.get("/api/profiles")
async def list_profiles_endpoint(request: Request):
    """Every profile — for the host owner, and (``acl.multi_user``) the caller's own + shared ones.

    МПД Т2/Т4: the roster used to be host-wide, so one login enumerated every orchestrator on the
    box. See :mod:`hermes_acl_profiles` for the rule; with the flag off this is the old answer.
    """
    payload = await _read_profiles()
    return _scoped_profile_view(payload, acl_profiles.request_subject(request))


@router.post("/api/profiles")
async def create_profile_endpoint(body: ProfileCreate, request: Request):
    from hermes_cli import profiles as profiles_mod
    # МПД Т3: profiles are created by the admin (§6.2). With `acl.profile_self_service` a person
    # may create exactly their own `01-<login>` root — nothing else, and never a name another
    # person's record already holds.
    acl_profiles.require_create(request, body.name)
    explicit_source = (body.clone_from or "").strip()
    if explicit_source:
        # Clone config/skills/SOUL (or full state when clone_all) from the named source.
        clone, clone_from, clone_config = True, explicit_source, not body.clone_all
    elif body.clone_all:
        # Historical dashboard behavior: clone-all with no explicit source copies default.
        clone, clone_from, clone_config = True, "default", False
    else:
        clone = body.clone_from_default
        clone_from = "default" if clone else None
        clone_config = clone
    with _profile_errors("POST /api/profiles failed", not_found=(),
                         bad_request=(ValueError, FileExistsError, FileNotFoundError)):
        # Off the loop: creation asks a live multiplexer to serve the profile now over its control
        # socket, and on the gateway's own listener (the Webapp) that socket answers on this loop.
        path = await asyncio.to_thread(
            profiles_mod.create_profile,
            name=body.name, clone_from=clone_from, clone_all=body.clone_all,
            clone_config=clone_config, no_skills=body.no_skills, description=body.description,
            clone_channels=body.clone_channels, **({"soul": body.soul} if body.soul is not None else {}))
        # Match the CLI flow: fresh named profiles get the bundled skills (cloning already
        # copied the source's; no_skills wrote the opt-out marker so seeding no-ops) and a
        # ~/.local/bin wrapper when the alias is safe.
        if not clone:
            profiles_mod.seed_profile_skills(path, quiet=True)
        if not profiles_mod.check_alias_collision(body.name):
            profiles_mod.create_wrapper_script(body.name)

    # Everything below is best-effort: the profile already exists, so a hiccup must not 500
    # the whole create — the user can fix it from the dashboard or `<profile> setup`.
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    # Validate against THIS dashboard's home (the catalog the picker offered), not the empty
    # new profile; the write still lands in the new profile. A rejection is reported (not just
    # logged) so the dialog can say WHY the model was not set.
    model_error = ""

    def _set_model() -> bool:
        nonlocal model_error
        try:
            _write_profile_model(path, provider, model, validate_in=get_process_hermes_home())
        except HTTPException as exc:
            model_error = str(exc.detail)
            return False
        return True

    model_set = bool(provider and model) and _best_effort(
        "Setting model for new profile %s failed", body.name, fn=_set_model, default=False)
    mcp_written = _best_effort(
        "Writing MCP servers for new profile %s failed", body.name, default=0,
        fn=lambda: _write_profile_mcp_servers(path, body.mcp_servers)) if body.mcp_servers else 0
    # "keep" has replace semantics; skipped when empty (legacy: keep the bundle).
    skills_disabled = _best_effort(
        "Applying skill selection for new profile %s failed", body.name, default=0,
        fn=lambda: _disable_unselected_skills(path, body.keep_skills)) if body.keep_skills else 0

    # Hub installs spawn async, scoped via `-p <name>` (a fresh subprocess re-binds
    # skills_hub.SKILLS_DIR at import). PIDs go back for the UI to poll.
    def _spawn_install(ident: str):
        return _spawn_hermes_action(["-p", body.name, "skills", "install", ident, "--yes"],
                                    _hub_action_name("install", ident)).pid

    hub_installs: list[dict[str, Any]] = [
        {"identifier": ident, "pid": _best_effort(
            "Spawning hub-skill install %s for new profile %s failed", ident, body.name,
            fn=lambda: _spawn_install(ident))}
        for ident in ((i or "").strip() for i in body.hub_skills) if ident]

    return {"ok": True, "name": body.name, "path": str(path), "model_set": model_set, "model_error": model_error,
            "mcp_written": mcp_written, "skills_disabled": skills_disabled,
            "hub_installs": hub_installs, "soul_set": body.soul is not None,
            # A personal root (``01-<login>``) was seeded with the personalization questionnaire
            # next to its SOUL; the desktop client offers it on first entry at this path.
            "questionnaire_path": _questionnaire_path(path)}


@router.get("/api/profiles/active")
async def get_active_profile_endpoint(request: Request):
    """``active`` is the sticky default written by ``hermes profile use`` (what new CLI
    invocations pick up); ``current`` is the profile this running dashboard is scoped to.

    ``active`` is a HOST-wide sticky value, so for a non-owner (``acl.multi_user``) it may name
    another person's orchestrator: it is answered only when the caller may see it, and otherwise
    replaced by a profile they do hold. The answer never becomes a roster leak of its own.
    """
    from hermes_cli import profiles as profiles_mod

    def _run():
        # Both reads touch the filesystem; one hop so sidebar polling costs one round-trip.
        def _or_default(fn, on_error="default"):
            try:
                return fn() or "default"
            except Exception:
                return on_error
        # A dashboard that cannot name its own home must not read as the machine
        # dashboard: "default" is exactly what lets the SPA adopt the sticky profile.
        return {"active": _or_default(profiles_mod.get_active_profile),
                "current": _or_default(profiles_mod.get_active_profile_name, on_error="custom")}

    body = await run_in_threadpool(_run)
    if not acl_profiles.multi_user_enabled() or acl_profiles.request_is_owner(request):
        return body
    return _own_profile_names(request, body)


def _own_profile_names(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Answer the sticky-profile question with names this caller may actually use."""
    from hermes_cli import profiles as profiles_mod
    try:
        names = list(profiles_mod.list_profile_names())
    except Exception:  # health: allow BLE001 -- a listing failure must not 500 a sidebar poll
        _log.exception("profile enumeration failed")
        names = []
    visible = acl_profiles.scope_for(request, names)
    if visible is None:
        return body
    allowed = set(visible)
    out = dict(body)
    for key in ("active", "current"):
        if out.get(key) not in allowed:
            # Nothing to point at: an empty answer is the honest one (the SPA then asks for the
            # caller's own profile list, which is already scoped).
            out[key] = visible[0] if visible else ""
    return out


@router.post("/api/profiles/active")
async def set_active_profile_endpoint(body: ProfileActiveUpdate, request: Request):
    """Set the sticky active profile (mirrors ``hermes profile use``); does not retarget the
    running dashboard, only subsequent CLI commands and gateways.

    The value is host-wide — every profile's next CLI invocation reads it — so it stays the
    owner's to set; a second person choosing it would retarget the owner's shell.
    """
    from hermes_cli import profiles as profiles_mod
    acl_profiles.require_owner(request, action="owner_only")
    with _profile_errors("POST /api/profiles/active failed"):
        # Stats the target, creates the state directory, writes via temp file + replace.
        await run_in_threadpool(profiles_mod.set_active_profile, body.name)
    return {"ok": True, "active": profiles_mod.normalize_profile_name(body.name)}


@router.get("/api/profiles/{name}/setup-command")
async def get_profile_setup_command(name: str, request: Request):
    acl_profiles.require_visible(request, name)
    return {"command": _profile_setup_command(name)}


# (executable, flag): None = takes one quoted `sh -lc '…'` string after -e; "" = argv follows
# the executable directly (kitty).
_LINUX_TERMINALS = (
    ("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"),
    ("xfce4-terminal", None), ("mate-terminal", None), ("lxterminal", None),
    ("tilix", "-e"), ("alacritty", "-e"), ("kitty", ""), ("xterm", "-e"))


def _linux_terminal_commands(command: str) -> list:
    sh = ["sh", "-lc", command]
    quoted = f"sh -lc '{command}'"
    return [
        (exe, [exe, "-e", quoted] if flag is None else [exe, *([flag] if flag else []), *sh])
        for exe, flag in _LINUX_TERMINALS]


@router.post("/api/profiles/{name}/open-terminal")
async def open_profile_terminal_endpoint(name: str, request: Request):
    acl_profiles.require_visible(request, name)
    with _profile_errors("POST /api/profiles/%s/open-terminal failed", name):
        command = _profile_setup_command(name)

        if sys.platform.startswith("win"):
            subprocess.Popen(["cmd.exe", "/c", "start", "", command])  # noqa: ASYNC220 -- fire-and-forget spawn; Popen returns immediately and callers need the Popen object
        elif sys.platform == "darwin":
            escaped = command.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e",  # noqa: ASYNC220 -- fire-and-forget spawn; Popen returns immediately and callers need the Popen object
                              f'tell application "Terminal"\nactivate\ndo script "{escaped}"\nend tell'])
        else:
            for executable, popen_args in _linux_terminal_commands(command):
                if await asyncio.to_thread(subprocess.call, ["which", executable], stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL) == 0:
                    subprocess.Popen(popen_args)  # noqa: ASYNC220 -- fire-and-forget spawn; Popen returns immediately and callers need the Popen object
                    break
            else:
                raise HTTPException(status_code=400, detail="No supported terminal emulator found")
    return {"ok": True, "command": command}


@router.patch("/api/profiles/{name}")
async def rename_profile_endpoint(name: str, body: ProfileRename, request: Request):
    from hermes_cli import profiles as profiles_mod
    # A rename moves a directory and re-keys a person's data — the target must be theirs to
    # reconfigure (a shared profile's SOUL/skills are not one user's to rename), and the NEW name
    # must not land inside another person's namespace.
    acl_profiles.require_writable(request, name)
    acl_profiles.require_create(request, body.new_name)
    with _profile_errors("PATCH /api/profiles/%s failed", name,
                         bad_request=(ValueError, FileExistsError)):
        # Stops a running gateway (10 s poll), renames the directory, lets the profile's memory
        # provider re-key its state and regenerates the wrapper script.
        path = await run_in_threadpool(profiles_mod.rename_profile, name, body.new_name)
    # For the default profile the rename lands as a presentation-only display_name; the
    # canonical id ("default") is always returned so callers keying on `name` stay correct.
    try:
        is_default = profiles_mod.normalize_profile_name(name) == "default"
    except ValueError:
        is_default = False
    if is_default:
        return {"ok": True, "name": "default", "display_name": body.new_name.strip(), "path": str(path)}
    return {"ok": True, "name": profiles_mod.normalize_profile_name(body.new_name), "path": str(path)}


@router.delete("/api/profiles/{name}")
async def delete_profile_endpoint(name: str, request: Request):
    """The dashboard collects the user's confirmation in its own dialog, so ``yes=True``
    always skips the CLI's interactive prompt.

    A delete whose identity settlement stays pending answers ``ok`` with ``settlement_pending``
    and the retry command: the profile directory is already gone, and folding that state into
    the generic 500 made a dashboard client read a completed delete as a failure (its retry
    then 404'd)."""
    from hermes_cli import profiles as profiles_mod
    # Deleting removes a profile and its state for everyone who used it: owner-only, never the
    # "I may use it" grant (МПД Т2 — «чужой недоступен»).
    acl_profiles.require_owner(request, action="owner_only")
    try:
        with _profile_errors("DELETE /api/profiles/%s failed", name):
            # Polls a running gateway's PID for up to 10 s, then rmtree()s the directory; on the
            # loop that parks every request past the desktop's 10 s WebSocket ready-probe.
            path = await run_in_threadpool(profiles_mod.delete_profile, name, yes=True)
    except ProfileIdentitySettlementPending as exc:
        return {"ok": True, "path": str(exc.path), "identity_settled": False,
                "settlement_pending": True, "retry_command": exc.retry_command}
    return {"ok": True, "path": str(path)}


@router.get("/api/profiles/{name}/soul")
async def get_profile_soul(name: str, request: Request):
    # Reading a profile's instructions is reading that profile: a foreign name answers 404.
    acl_profiles.require_visible(request, name)
    soul_path = _resolve_profile_dir(name) / "SOUL.md"
    def _run():
        # Probe and read in one hop (two round-trips would widen the check/read window).
        if not soul_path.exists():
            return _MISSING
        return soul_path.read_text(encoding="utf-8-sig")

    content = await _read_off_loop(_run, "SOUL.md", OSError)
    if content is _MISSING:
        return {"content": "", "exists": False}
    return {"content": content, "exists": True}


@router.put("/api/profiles/{name}/soul")
async def update_profile_soul(name: str, body: ProfileSoulUpdate, request: Request):
    # SOUL.md is the profile's INSTRUCTIONS: shared profiles are runtimes everyone uses, so only
    # their own person's profiles are writable (МПД Т4 — «личное не попадает в общий профиль»).
    acl_profiles.require_writable(request, name)
    soul_path = _resolve_profile_dir(name) / "SOUL.md"

    def _run():
        from utils import atomic_write_text
        # Atomic: a bare write_text() truncates SOUL.md before the new body lands, and the
        # paired GET reports an unreadable file as "never set" — so an interrupted save would
        # make the editor's next Save persist an empty document. preserve_mode keeps an
        # existing file's mode/owner; create_mode=0o644 covers the first save (profiles
        # chmods only .env to 0600 and SOUL.md is not a secret).
        atomic_write_text(soul_path, body.content, preserve_mode=True, create_mode=0o644)

    try:
        # Temp file + fsync + replace blocks for as long as the filesystem takes to commit.
        await run_in_threadpool(_run)
    except OSError as e:
        _log.exception("PUT /api/profiles/%s/soul failed", name)
        raise HTTPException(status_code=500, detail=f"Could not write SOUL.md: {e}")
    return {"ok": True}


@router.post("/api/profiles/{name}/soul/apply-questionnaire")
async def apply_soul_questionnaire(name: str, body: ProfileSoulQuestionnaire, request: Request):
    """Apply a filled personalization questionnaire to a profile's SOUL and memory.

    SOUL.md is the profile's instructions, so the same rule as the editor applies: only the
    profile's own person (or the host owner) may rewrite it — ``require_writable`` also refuses
    a shared profile with 403 and a foreign one with the 404 an absent name gets.

    Empty answers leave their section untouched; the response carries the sections that actually
    changed and the SOUL's new hash so a client can show the edit without re-reading the file.
    Block 5–6 answers (terms, sources, free-form) are durable facts about the person, not
    instructions: they go to the profile's memory through the same store the ``memory`` tool uses.
    """
    from hermes_acl_soul import apply_questionnaire, questionnaire_memory_entries

    acl_profiles.require_writable(request, name)
    profile_dir = _resolve_profile_dir(name)
    soul_path = profile_dir / "SOUL.md"
    answers = body.model_dump()

    def _run() -> dict[str, Any]:
        import hashlib

        from utils import atomic_write_text

        before_text = soul_path.read_text(encoding="utf-8-sig") if soul_path.exists() else ""
        if not before_text.strip():
            # Nothing to personalize: a profile with no SOUL would get a document built from
            # nothing, and «пустой ответ = по умолчанию» has no default to fall back to.
            raise ValueError("profile has no SOUL.md to personalize")
        new_text, report = apply_questionnaire(before_text, answers)
        changed = new_text != before_text
        if changed:
            atomic_write_text(soul_path, new_text, preserve_mode=True, create_mode=0o644)
        return {
            "ok": True,
            "changed": changed,
            "changed_sections": report["changed_sections"] if changed else [],
            "soul_sha256": hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
        }

    def _write_memory() -> list[str]:
        """Durable facts go to the profile's own memory store, under that profile's home."""
        from tools.memory_tool import load_on_disk_store

        store = load_on_disk_store()
        written: list[str] = []
        for entry in questionnaire_memory_entries(answers):
            result = store.add("memory", entry)
            if result.get("success"):
                written.append(entry)
            else:
                _log.warning("questionnaire memory write refused: %s", result.get("error"))
        return written

    with _profile_errors("POST /api/profiles/%s/soul/apply-questionnaire failed", name,
                         not_found=(FileNotFoundError,), bad_request=(ValueError,)):
        result = await run_in_threadpool(_run)
        # SOUL is already saved: a memory hiccup is reported, never 500s the personalization.
        entries = questionnaire_memory_entries(answers)
        if entries:
            try:
                written = await config_scoped_to_thread(name, _write_memory)
            except Exception:  # health: allow BLE001 -- SOUL is already saved; report, never 500
                _log.exception("questionnaire memory write failed for %s", name)
                written = None
            result["memory_entries"] = written or []
            result["memory_error"] = "" if written is not None else "memory write failed"
        else:
            result["memory_entries"] = []
            result["memory_error"] = ""
    return result


@router.put("/api/profiles/{name}/description")
async def update_profile_description_endpoint(name: str, body: ProfileDescriptionUpdate, request: Request):
    """Set or clear a profile's role description (kanban routing signal), stored as
    user-authored (``description_auto: false``) so the auto-describer won't overwrite it."""
    from hermes_cli import profiles as profiles_mod
    acl_profiles.require_writable(request, name)
    profile_dir = _resolve_profile_dir(name)
    text = (body.description or "").strip()
    with _profile_errors("PUT /api/profiles/%s/description failed", name,
                         not_found=(), bad_request=()):
        await run_in_threadpool(
            profiles_mod.write_profile_meta, profile_dir, description=text, description_auto=False)
    return {"ok": True, "description": text, "description_auto": False}


@router.put("/api/profiles/{name}/model")
async def update_profile_model_endpoint(name: str, body: ProfileModelUpdate, request: Request):
    """Set the main model for a specific profile's config.yaml without touching the dashboard's
    own profile — ``POST /api/model/set`` (main scope) via the HERMES_HOME override."""
    acl_profiles.require_writable(request, name)
    profile_dir = _resolve_profile_dir(name)
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    if not provider or not model:
        raise HTTPException(status_code=400, detail="provider and model are required")
    with _profile_errors("PUT /api/profiles/%s/model failed", name,
                         not_found=(), bad_request=()):
        await run_in_threadpool(_write_profile_model, profile_dir, provider, model)
    return {"ok": True, "provider": provider, "model": model}


@router.post("/api/profiles/{name}/describe-auto")
async def describe_profile_auto_endpoint(name: str, body: ProfileDescribeAuto, request: Request):
    """Auto-generate a profile's description via the auxiliary LLM (mirrors ``hermes profile
    describe <name> --auto``). A failed generation is ``ok: false`` with a reason rather than
    an HTTP error so the UI can surface it inline and let the operator retry."""
    # Resolution stays on the loop: it owns the 400/404 mapping the 500 fallback would flatten.
    acl_profiles.require_writable(request, name)
    _resolve_profile_dir(name)

    def _run():
        from hermes_cli import profile_describer
        return profile_describer.describe_profile(name, overwrite=bool(body.overwrite))

    with _profile_errors("POST /api/profiles/%s/describe-auto failed", name,
                         not_found=(), bad_request=()):
        # A synchronous LLM round-trip with a 60 s ceiling; on the loop it stalls everything.
        outcome = await run_in_threadpool(_run)
    # description_auto mirrors ok: a failed sweep leaves any existing description untouched.
    return {"ok": bool(outcome.ok), "reason": outcome.reason, "description": outcome.description,
            "description_auto": bool(outcome.ok)}


# ── Export / Import ── wraps hermes_cli.profiles.export_profile / import_profile. Paths are
# exchanged, not bytes — the desktop backends share the filesystem with the native dialogs.


def _read_desktop_overlay(profile_dir: Path) -> Any:
    """The desktop appearance overlay bundled with an imported profile
    (``desktop.json`` at the profile root); raises when unreadable."""
    return json.loads((profile_dir / "desktop.json").read_text(encoding="utf-8-sig"))


@router.post("/api/profiles/{name}/export")
async def export_profile_endpoint(name: str, body: ProfileExport, request: Request):
    from hermes_cli import profiles as profiles_mod
    # An export is a portable SNAPSHOT: it carries the profile's .env (bot tokens, API keys) and
    # its full state, so it stays the owner's operation — never a shared profile's consumer.
    acl_profiles.require_owner(request, action="owner_only")
    output = (body.output or "").strip()
    if not output:
        try:
            output = str(profiles_mod.get_profile_export_path(name))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not create export directory: {exc}")

    with _profile_errors("POST /api/profiles/%s/export failed", name):
        result = await run_in_threadpool(
            profiles_mod.export_profile, name, output, extra_files=body.extra_files or None)
    return {"ok": True, "archive": str(result)}


@router.post("/api/profiles/import")
async def import_profile_endpoint(body: ProfileImport, request: Request):
    from hermes_cli import profiles as profiles_mod
    # Import writes a whole profile (and its .env) from an archive: owner-only, and the imported
    # name must not land inside somebody's personal namespace.
    acl_profiles.require_owner(request, action="owner_only")
    archive = (body.archive or "").strip()
    if not archive:
        raise HTTPException(status_code=400, detail="archive path is required")

    with _profile_errors("POST /api/profiles/import failed",
                         bad_request=(ValueError, FileExistsError)):
        profile_dir = await run_in_threadpool(
            profiles_mod.import_profile, archive, name=(body.name or "").strip() or None)

    imported = profile_dir.name

    # Match the CLI import flow: create the wrapper alias when it's safe.
    _best_effort("Creating wrapper for imported profile %s failed", imported,
                 fn=lambda: (profiles_mod.check_alias_collision(imported)
                             or profiles_mod.create_wrapper_script(imported)))

    # Bundled desktop appearance overlay, so the desktop needn't make another round-trip.
    desktop_overlay = None
    overlay_path = profile_dir / "desktop.json"
    if overlay_path.is_file():
        desktop_overlay = _best_effort(
            "Reading desktop.json from imported profile %s failed", imported,
            fn=lambda: _read_desktop_overlay(profile_dir))
    return {"ok": True, "name": imported, "path": str(profile_dir), "desktop": desktop_overlay}


@router.get("/api/profiles/{name}/desktop-overlay")
async def get_profile_desktop_overlay(name: str, request: Request):
    """The desktop appearance/interface overlay bundled with an imported profile
    (``desktop.json`` at the profile root), or ``exists: false``."""
    acl_profiles.require_visible(request, name)
    profile_dir = _resolve_profile_dir(name)

    def _run():
        # Probe and read in one hop; _MISSING because desktop.json may hold ``null``.
        if not (profile_dir / "desktop.json").is_file():
            return _MISSING
        return _read_desktop_overlay(profile_dir)

    overlay = await _read_off_loop(_run, "desktop.json", Exception)
    if overlay is _MISSING:
        return {"exists": False, "desktop": None}
    return {"exists": True, "desktop": overlay}
