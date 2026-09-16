"""Regression coverage for immutable provider/model execution routes."""
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_main_fallback_is_disabled_for_locked_agent():
    from agent.chat_completion_helpers import FailoverReason, try_activate_fallback

    agent = SimpleNamespace(
        _immutable_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
        _model_explicitly_selected=False,
        model="gpt-5.6-sol",
        provider="openai-codex",
    )
    assert try_activate_fallback(agent, FailoverReason.rate_limit) is False
    assert agent._explicit_model_unavailable is True
    assert agent._explicit_model_unavailable_reason == "rate_limit"
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")


def test_locked_primary_runtime_is_not_restored_or_rerouted():
    from agent.agent_runtime_helpers import restore_primary_runtime

    agent = SimpleNamespace(
        _immutable_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
        _fallback_index=9,
    )
    assert restore_primary_runtime(agent) is False
    assert agent._fallback_index == 0


def test_auxiliary_uses_execution_local_locked_pair(monkeypatch):
    from agent import auxiliary_client as aux

    lock = {
        "provider": "openai-codex", "model": "gpt-5.6-sol",
        "base_url": "https://locked.invalid/v1", "api_key": "locked-key",
        "route_locked": True,
    }
    monkeypatch.setattr(
        aux, "_get_auxiliary_task_config",
        lambda task: {"provider": "nous", "model": "different-model"},
    )
    with aux.scoped_runtime_main(lock):
        resolved = aux._resolve_task_provider_model(task="compression")
    assert resolved[:2] == ("openai-codex", "gpt-5.6-sol")
    assert resolved[2:4] == ("https://locked.invalid/v1", "locked-key")


def test_auxiliary_does_not_treat_process_global_compat_mirror_as_lock(monkeypatch):
    from agent import auxiliary_client as aux

    monkeypatch.setattr(aux, "_RUNTIME_MAIN_CONTEXT", ContextVar("test-empty", default=None))
    monkeypatch.setattr(
        aux, "_compat_runtime_main",
        lambda: {"provider": "wrong", "model": "wrong-model", "route_locked": True},
    )
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    resolved = aux._resolve_task_provider_model(
        task="compression", provider="nous", model="wanted-model"
    )
    assert resolved[:2] == ("nous", "wanted-model")


def test_background_review_ignores_task_route_for_locked_parent():
    from agent.background_review import _resolve_review_runtime

    agent = SimpleNamespace(
        provider="openai-codex", model="gpt-5.6-sol",
        _immutable_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
        _credential_pool=None, request_overrides={}, max_tokens=4096,
        acp_command=None, acp_args=[],
        _current_main_runtime=lambda: {
            "provider": "openai-codex", "model": "gpt-5.6-sol",
            "base_url": "https://locked.invalid/v1", "api_key": "locked-key",
            "api_mode": "responses", "route_locked": True,
        },
    )
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider") as resolver:
        runtime = _resolve_review_runtime(
            agent, {"provider": "nous", "model": "different-model"}
        )
    resolver.assert_not_called()
    assert (runtime["provider"], runtime["model"]) == (
        "openai-codex", "gpt-5.6-sol"
    )


def test_current_route_recovers_named_custom_identity_from_bare_custom(monkeypatch):
    """The root-cause fix: a live route whose provider resolved to the bare
    ``custom`` billing class (entry name lost) must be re-pinned to the
    routable ``custom:<name>`` identity before it is persisted to a card.
    Regresses kanban t_e1b78631 (provider='custom' -> worker dies with
    'No API key found for provider custom')."""
    import hermes_cli.runtime_provider as rp
    from agent import immutable_route_plugin as plug

    # Config that owns the endpoint via a named custom provider.
    monkeypatch.setattr(
        rp, "load_config",
        lambda: {
            "providers": {
                "letovo": {"base_url": "https://llm.letovo.site/openai"},
            },
            "custom_providers": [],
        },
    )

    live = {
        "provider": "custom",          # resolved billing class, name lost
        "model": "deepseek-v4-flash",
        "base_url": "https://llm.letovo.site/openai",
        "api_key": lambda: "k",
    }
    route = plug.current_route(live)
    # The bare 'custom' is upgraded to a routable custom:<name> identity —
    # never left as the unroutable bare class.
    assert route["provider"] != "custom"
    assert route["provider"].lower().startswith("custom:")
    assert "letovo" in route["provider"].lower()
    assert route["model"] == "deepseek-v4-flash"
    assert route["route_locked"] is True


def test_current_route_keeps_bare_custom_only_when_identity_unrecoverable(monkeypatch):
    """When no config entry owns the endpoint/model, current_route cannot
    recover a named identity and must not fabricate one."""

    import hermes_cli.runtime_provider as rp
    from agent import immutable_route_plugin as plug

    monkeypatch.setattr(rp, "load_config", lambda: {"providers": {}})
    monkeypatch.setattr(
        rp, "get_compatible_custom_providers", lambda config: []
    )

    live = {
        "provider": "custom",
        "model": "some-model",
        "base_url": "http://localhost:9000/v1",
    }
    route = plug.current_route(live)
    # Bare custom is the documented last resort for a genuine ad-hoc endpoint;
    # it is NOT silently upgraded to a fake identity.
    assert route["provider"] == "custom"
    assert route["model"] == "some-model"

