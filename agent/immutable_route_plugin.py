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
        return {"action": "block", "message": f"MODEL_LOCK: live effective route unavailable; no {target} task created."}
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
