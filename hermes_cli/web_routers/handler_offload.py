"""The one seam through which a dashboard router moves BLOCKING work off the event loop.

Why a seam and not bare ``asyncio.to_thread`` at each call site: the deployment needs a
kill switch. ``dashboard.handler_offload: false`` (or ``HERMES_HANDLER_OFFLOAD=0``) makes
every wrapped call run INLINE on the loop — the pre-change, single-process behaviour — so a
regression caused by threading can be rolled back by one config write plus a restart, with
no code change. The mode is logged once at boot and reported by ``/api/status``.

Why a private pool instead of ``loop.set_default_executor``: the default executor is shared
with every unrelated ``asyncio.to_thread`` in the process (agent turns, MCP, PTY drains). A
routers-only pool keeps the blast radius of a resize to the routes this card owns, and
avoids the classic deadlock where an executor task blocks on another ``to_thread`` call
with every worker already taken.

Sizing: these handlers do file metadata and short writes (stat/mkdir/replace/unlink) plus
the occasional archive scan — milliseconds each, no network waits. ``2 x cpu`` workers (cap
32, floor 8) covers the realistic dashboard burst (a handful of tabs polling and one save)
without parking a thread per socket. When every worker is busy the extra calls queue, and
``queue_depth``/``max_queue_depth`` in ``/api/status`` is how you see that instead of
guessing; ``wait_ms`` records how long a caller waited for a slot.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("hermes_cli.web_server")

# Config key (dashboard section) and its env override. The env form exists so the emergency
# rollback is one line in the unit file / shell, not an edit of a YAML document.
CONFIG_KEY = "handler_offload"
ENV_KEY = "HERMES_HANDLER_OFFLOAD"
DEFAULT_ENABLED = True
DEFAULT_WORKERS = max(8, min(32, (os.cpu_count() or 4) * 2))


def _env_switch() -> Optional[bool]:
    raw = (os.environ.get(ENV_KEY) or "").strip().lower()
    if not raw:
        return None
    return raw not in ("0", "false", "off", "no", "disabled")


def _config_section() -> Dict[str, Any]:
    """The raw ``dashboard`` section of the active profile's config.yaml (no defaults).

    Raw on purpose: a behaviour switch must be readable without pulling the merged default
    tree, and an unreadable config must not silently flip the mode — the caller keeps the
    default when this raises.
    """
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import get_hermes_home

    cfg = load_user_config_effective(get_hermes_home() / "config.yaml")
    section = cfg.get("dashboard") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def _truthy(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("0", "false", "off", "no", "disabled")


class HandlerOffload:
    """Stateful offload seam: switch position, one bounded pool, and its counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._enabled = DEFAULT_ENABLED
        self._workers = DEFAULT_WORKERS
        self._settled = False
        self.submitted = 0
        self.inline = 0
        self.in_flight = 0
        self.queue_depth = 0
        self.max_queue_depth = 0
        self.wait_ms_total = 0.0

    # ── switch ────────────────────────────────────────────────────────────────

    def resolve(self) -> None:
        """Settle the switch once: env override → config → default. Logged by ``install``."""
        env = _env_switch()
        if env is not None:
            self._enabled = env
            source = f"env {ENV_KEY}"
        else:
            workers = DEFAULT_WORKERS
            try:
                section = _config_section()
                if CONFIG_KEY in section:
                    self._enabled = _truthy(section.get(CONFIG_KEY), DEFAULT_ENABLED)
                    source = f"config dashboard.{CONFIG_KEY}"
                else:
                    self._enabled = DEFAULT_ENABLED
                    source = "default"
                raw_workers = section.get(f"{CONFIG_KEY}_workers")
                if isinstance(raw_workers, int) and 1 <= raw_workers <= 128:
                    workers = raw_workers
            except Exception:  # an unreadable config keeps the shipped default
                self._enabled = DEFAULT_ENABLED
                workers = DEFAULT_WORKERS
                source = "default (config unreadable)"
                log.warning("handler offload switch unreadable, keeping default", exc_info=True)
            self._workers = workers
        self._settled = True
        with self._lock:
            mode = "threaded" if self._enabled else "inline (single-process)"
            log.info(
                "handler offload mode=%s source=%s workers=%s", mode, source,
                self._workers if self._enabled else 0,
            )

    def install(self, app: Any = None) -> "HandlerOffload":
        """Settle the switch and, when enabled, arm the pool on ``app.state``."""
        if not self._settled:
            self.resolve()
        if self._enabled:
            self._arm()
        if app is not None:
            app.state.handler_offload = self
        return self

    def _arm(self) -> None:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._workers, thread_name_prefix="web-offload")
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(self._workers)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def workers(self) -> int:
        return self._workers

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "workers": self._workers if self._enabled else 0,
            "submitted": self.submitted,
            "inline": self.inline,
            "in_flight": self.in_flight,
            "queue_depth": self.queue_depth,
            "max_queue_depth": self.max_queue_depth,
            "avg_wait_ms": round(self.wait_ms_total / self.submitted, 2) if self.submitted else 0.0,
        }

    # ── execution ─────────────────────────────────────────────────────────────

    def run_inline(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.inline += 1
        return fn(*args, **kwargs)

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Await ``fn`` off the loop, or inline when the switch is off."""
        if not self._enabled:
            return self.run_inline(fn, *args, **kwargs)
        self._arm()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self._lock:
            self.submitted += 1
            self.queue_depth += 1
            if self.queue_depth > self.max_queue_depth:
                self.max_queue_depth = self.queue_depth
        assert self._semaphore is not None and self._executor is not None
        async with self._semaphore:
            waited = (loop.time() - started) * 1000.0
            with self._lock:
                self.queue_depth -= 1
                self.in_flight += 1
                self.wait_ms_total += waited
            try:
                return await loop.run_in_executor(self._executor, _bind(fn, args, kwargs))
            finally:
                with self._lock:
                    self.in_flight -= 1

    def shutdown(self) -> None:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=False)


def _bind(fn: Callable[..., Any], args: tuple, kwargs: Dict[str, Any]) -> Callable[[], Any]:
    """Wrap the call with a COPY of the caller's contextvars.

    ``run_in_executor`` does not propagate context (``asyncio.to_thread`` does), and the
    profile-scope contextvar is how a request's profile reaches ``get_hermes_home()``: without
    this copy a worker would read the launch profile and write into the wrong home.
    """
    ctx = contextvars.copy_context()

    def _call() -> Any:
        return ctx.run(functools.partial(fn, *args, **kwargs))

    return _call


# Process-wide instance: one switch, one pool, whatever route asks.
HANDLER_OFFLOAD = HandlerOffload()


async def off_loop(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking callable through the switch-controlled seam."""
    return await HANDLER_OFFLOAD.run(fn, *args, **kwargs)


def offload_status() -> Dict[str, Any]:
    """`/api/status` view of the mode and the serving load behind it."""
    return HANDLER_OFFLOAD.snapshot()
