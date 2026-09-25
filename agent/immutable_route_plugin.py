"""Defense-in-depth plugin for the core immutable provider/model route."""
from functools import wraps
from agent import auxiliary_client as aux

FALLBACKS = (
    "_try_payment_fallback", "_try_main_agent_model_fallback",
    "_try_configured_fallback_chain", "_try_configured_fallback_for_unavailable_client",
    "_try_main_fallback_chain",
)
WRAPPED = FALLBACKS + (
    "_resolve_task_provider_model", "_resolve_auto_route",
    "resolve_vision_provider_client", "get_text_auxiliary_client",
    "get_async_text_auxiliary_client", "_call_llm_impl", "_async_call_llm_impl",
)


def current_route(runtime=None):
    live = runtime if isinstance(runtime, dict) else aux._RUNTIME_MAIN_CONTEXT.get()
    if not isinstance(live, dict):
        raise RuntimeError("MODEL_LOCK: live effective route unavailable")
    route = aux._normalize_main_runtime(live)
    provider = str(route.get("provider") or "").strip().lower()
    model = str(route.get("model") or "").strip()
    if not provider or not model or provider in {"auto", "main", "moa"}:
        raise RuntimeError("MODEL_LOCK: concrete live provider/model pair required")
    # Bare ``custom`` (and its ``local/custom`` alias) is the *resolved billing
    # class* shared by every named providers:/custom_providers: entry — the
    # entry name is lost. Pinning a card with provider="custom" makes the
    # spawned worker run ``--provider custom`` with no endpoint/key and die
    # instantly ("No API key found for provider 'custom'", kanban t_e1b78631).
    # Recover the canonical ``custom:<name>`` identity from the live route's
    # durable facts (base_url, then model) — exactly what session-persistence
    # does — so downstream kanban_create never persists an unroutable provider.
    if provider in {"custom", "local/custom"}:
        recovered = _canonical_custom_identity(
            base_url=route.get("base_url"), model=model,
        )
        if recovered:
            provider = recovered
    route.update(provider=provider, model=model, route_locked=True)
    return route


def _canonical_custom_identity(*, base_url=None, model=None):
    """Recover ``custom:<name>`` for a bare-custom live route, else ``None``.

    Uses :func:`hermes_cli.runtime_provider.canonical_custom_identity`
    (base_url reverse-lookup first, model second) so the pinned provider stays
    routable on the worker (which resolves from config alone, without the
    session's in-memory base_url). Returns ``None`` when no config entry owns
    the endpoint — the caller then keeps the bare ``custom`` label.
    """
    try:
        from hermes_cli.runtime_provider import canonical_custom_identity
        return canonical_custom_identity(base_url=base_url, model=model)
    except Exception:
        return None



def pre_tool(tool_name, args=None, **kwargs):
    args = dict(args or {})
    is_kanban = tool_name == "kanban_create"
    is_cron = (
        tool_name == "cronjob"
        and str(args.get("action") or "").strip().lower() == "create"
        and not args.get("no_agent")
    )
    if tool_name == "kanban_decompose" or (is_kanban and args.get("triage")):
        # These surfaces cannot prove route inheritance. When this caller's
        # immutable route IS provable, the operation must be decomposed by the
        # planter (who can inherit the route); otherwise defer to the tool layer
        # so a locked caller is still fail-closed downstream.
        try:
            current_route()
        except Exception:
            return None
        return {"action": "block", "message": "MODEL_LOCK: native decomposition cannot prove route inheritance"}
    if not is_kanban and not is_cron:
        return None
    if is_kanban and args.get("parents"):
        # Parent lineage, not this intermediate caller's adaptive route, owns
        # the child. The DB layer resolves and validates the immutable root
        # route for tool/CLI/API callers alike.
        return None
    target = "Kanban" if is_kanban else "cron"
    try:
        route = current_route()
    except Exception:
        # No immutable route is provable in THIS call's context (e.g. a CLI /
        # dashboard / non-locked agent whose live runtime ContextVar is not
        # populated). Do NOT hard-block here: the integrated tool/DB layer
        # (tools/kanban_tools.py, tools/cronjob_tools.py, kanban_db.py)
        # already fails closed for unpinnable cards and blocks them before any
        # worker spawn. Deferring keeps this hook defense-in-depth for locked
        # sessions without breaking legitimate non-locked callers.
        return None
    model, provider = args.get("model"), args.get("provider")
    if model is not None or provider is not None:
        if model == route["model"] and str(provider or "").lower() == route["provider"]:
            return None
        return {"action": "block", "message": f"MODEL_LOCK: {target} provider/model conflicts with caller route."}
    return {"action": "modify", "args": {"model": route["model"], "provider": route["provider"]}}


def install_aux_guard():
    if getattr(aux, "_profile_model_lock_installed", False):
        return
    for name in WRAPPED:
        if not callable(getattr(aux, name, None)):
            raise RuntimeError("MODEL_LOCK: incompatible Hermes auxiliary API: " + name)
    sync_impl, async_impl = aux._call_llm_impl, aux._async_call_llm_impl

    def no_fallback(*args, **kwargs):
        return None, None, ""

    def resolve_task(task=None, provider=None, model=None, base_url=None, api_key=None):
        route = current_route()
        if provider and str(provider).lower() not in {"auto", "main", route["provider"]}:
            raise RuntimeError("MODEL_LOCK: auxiliary provider mismatch")
        if model and model not in {"auto", route["model"]}:
            raise RuntimeError("MODEL_LOCK: auxiliary model mismatch")
        return route["provider"], route["model"], route.get("base_url") or None, route.get("api_key") or None, route.get("api_mode") or None

    def direct(runtime=None, async_mode=False, vision=False):
        route = current_route(runtime)
        if vision and not aux._main_model_supports_vision(route["provider"], route["model"]):
            raise RuntimeError("MODEL_LOCK: locked model does not support images")
        client, resolved_model = aux.resolve_provider_client(
            route["provider"], model=route["model"], async_mode=async_mode,
            explicit_base_url=route.get("base_url") or None,
            explicit_api_key=route.get("api_key") or None,
            api_mode=route.get("api_mode") or None, main_runtime=route, is_vision=vision,
        )
        if client is None or resolved_model != route["model"]:
            raise RuntimeError("MODEL_LOCK: provider resolver changed or failed locked route")
        aux._tag_effective_provider(client, route["provider"])
        return client, resolved_model, route["provider"]

    def auto_route(main_runtime=None, task=None):
        return direct(main_runtime)

    def vision_route(provider=None, model=None, *, base_url=None, api_key=None, async_mode=False, main_runtime=None):
        client, resolved_model, resolved_provider = direct(main_runtime, async_mode, True)
        return resolved_provider, client, resolved_model

    def text_client(task="", *, main_runtime=None):
        client, resolved_model, _ = direct(main_runtime)
        return client, resolved_model

    def async_text_client(task="", *, main_runtime=None):
        client, resolved_model, _ = direct(main_runtime, True)
        return client, resolved_model

    def pinned_kwargs(kwargs):
        route = current_route(kwargs.get("main_runtime"))
        return route, dict(
            kwargs, provider=route["provider"], model=route["model"], main_runtime=route,
            base_url=route.get("base_url") or None, api_key=route.get("api_key") or None,
            api_mode=route.get("api_mode") or None,
        )

    @wraps(sync_impl)
    def sync_locked(*args, **kwargs):
        route, locked_kwargs = pinned_kwargs(kwargs)
        with aux.scoped_runtime_main(route):
            return sync_impl(*args, **locked_kwargs)

    @wraps(async_impl)
    async def async_locked(*args, **kwargs):
        route, locked_kwargs = pinned_kwargs(kwargs)
        with aux.scoped_runtime_main(route):
            return await async_impl(*args, **locked_kwargs)

    aux._resolve_task_provider_model = resolve_task
    aux._resolve_auto_route = auto_route
    aux.resolve_vision_provider_client = vision_route
    aux.get_text_auxiliary_client = text_client
    aux.get_async_text_auxiliary_client = async_text_client
    aux._call_llm_impl = sync_locked
    aux._async_call_llm_impl = async_locked
    for name in FALLBACKS:
        setattr(aux, name, no_fallback)
    aux._profile_model_lock_installed = True


def register(ctx):
    install_aux_guard()
    ctx.register_hook("pre_tool_call", pre_tool)


def _register_pre_tool_hook():
    """Idempotently register the ``pre_tool`` callback on the live plugin
    manager's ``pre_tool_call`` hook list.

    ``immutable_route_plugin`` is a core agent module, not a directory plugin
    with a manifest, so it has no plugin ``ctx`` to register through. We append
    directly to the manager's hook slot exactly the way ``register_hook`` does
    (``hermes_cli.plugins.PluginManager._hooks``), guarded by a module flag so a
    profile switch / manager rebuild cannot double-register the same callback.
    """
    global _PRE_TOOL_HOOK_REGISTERED
    if _PRE_TOOL_HOOK_REGISTERED:
        return
    try:
        from hermes_cli.plugins import get_plugin_manager

        manager = get_plugin_manager()
        callbacks = manager._hooks.setdefault("pre_tool_call", [])
        if pre_tool not in callbacks:
            callbacks.append(pre_tool)
        _PRE_TOOL_HOOK_REGISTERED = True
    except Exception:
        # Any failure to reach the plugin registry must never block agent init.
        # The tool layer (tools/kanban_tools.py, tools/cronjob_tools.py) still
        # enforces the immutable route; this hook is defense-in-depth only.
        pass

def activate_for_locked_agent(agent):
    """Register the MODEL_LOCK defense-in-depth plugin for a locked agent.

    Called from agent init once ``agent._immutable_route`` is established.

    - Registers the ``pre_tool_call`` hook so a live locked agent's
      kanban_create / cronjob calls are pinned to the caller's canonical route
      (including the ``custom:<name>`` identity recovery) at the tool-hook layer
      whenever an immutable route is provable in that call's context, and
      otherwise defers to the integrated tool/DB enforcement.

    ``install_aux_guard`` is deliberately NOT auto-installed here. It replaces
    auxiliary resolver/client functions process-globally and hard-requires a
    concrete live route in the runtime ContextVar for every auxiliary call,
    which is incompatible with multiplexed gateways and non-locked concurrent
    sessions (a single locked agent would break every other session's aux
    calls). The integrated aux layer already hardens ``_resolve_task_provider_model``
    and ``_call_llm_impl`` (route_locked check / ``allow_fallback=False``); this
    plugin's guard remains available as an opt-in for fully-locked single-process
    deployments only.

    Activation is fail-soft: the integrated enforcement is authoritative, so any
    registration failure degrades and never blocks agent startup.
    """
    _register_pre_tool_hook()


_PRE_TOOL_HOOK_REGISTERED = False
