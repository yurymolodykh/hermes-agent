"""Tests that gateway /model switch persists across messages.

The gateway /model command stores session overrides in
``_session_model_overrides``.  These must:

1. Be applied in ``run_sync()`` so the next agent uses the switched model.
2. Not be mistaken for fallback activation (which evicts the cached agent).
3. Survive across multiple messages until /reset clears them.

Tests exercise the real ``_apply_session_model_override()`` and
``_is_intentional_model_switch()`` methods on ``GatewayRunner``.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_runner():
    """Create a minimal GatewayRunner with stubbed internals."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="tok")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_one_turn_model_restores = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._effective_model = None
    runner._effective_provider = None
    runner.session_store = MagicMock()
    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._entries = {session_key: session_entry}
    return runner


# ---------------------------------------------------------------------------
# Tests: _apply_session_model_override
# ---------------------------------------------------------------------------


class TestApplySessionModelOverride:
    """Verify _apply_session_model_override replaces config defaults."""

    def test_override_replaces_all_fields(self):
        runner = _make_runner()
        sk = build_session_key(_make_source())

        runner._session_model_overrides[sk] = {
            "model": "gpt-5.4-turbo",
            "provider": "openrouter",
            "api_key": "or-key-123",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        }

        model, rt = runner._apply_session_model_override(
            sk,
            "anthropic/claude-sonnet-4",
            {"provider": "anthropic", "api_key": "ant-key", "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages"},
        )

        assert model == "gpt-5.4-turbo"
        assert rt["provider"] == "openrouter"
        assert rt["api_key"] == "or-key-123"
        assert rt["base_url"] == "https://openrouter.ai/api/v1"
        assert rt["api_mode"] == "chat_completions"

    def test_no_override_returns_originals(self):
        runner = _make_runner()
        sk = build_session_key(_make_source())

        orig_model = "anthropic/claude-sonnet-4"
        orig_rt = {"provider": "anthropic", "api_key": "key", "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages"}

        model, rt = runner._apply_session_model_override(sk, orig_model, dict(orig_rt))

        assert model == orig_model
        assert rt == orig_rt


# ---------------------------------------------------------------------------
# Tests: _is_intentional_model_switch
# ---------------------------------------------------------------------------


class TestIsIntentionalModelSwitch:
    """Verify fallback detection respects intentional /model overrides."""

    def test_matches_override(self):
        runner = _make_runner()
        sk = build_session_key(_make_source())

        runner._session_model_overrides[sk] = {
            "model": "gpt-5.4",
            "provider": "openai",
            "api_key": "key",
            "base_url": "",
            "api_mode": "chat_completions",
        }

        assert runner._is_intentional_model_switch(sk, "gpt-5.4") is True


class TestExplicitModelPin:
    """An explicit /model pin blocks automatic failover and is actionable."""

    def test_session_override_is_detected_as_explicit_selection(self):
        runner = _make_runner()
        runner._peek_session_state = MagicMock(
            return_value=SimpleNamespace(
                conversation=SimpleNamespace(
                    model_override={
                        "model": "deepseek-v4-flash",
                        "provider": "custom:letovo",
                    }
                )
            )
        )

        assert runner._has_explicit_model_selection("telegram:dm:u1:c1") is True

    def test_fallback_guard_marks_explicit_model_unavailable(self):
        from agent.chat_completion_helpers import try_activate_fallback

        agent = SimpleNamespace(
            _model_explicitly_selected=True,
            model="deepseek-v4-flash",
            provider="custom:letovo",
        )

        assert try_activate_fallback(agent) is False
        assert agent._explicit_model_unavailable is True

    def test_unavailable_reply_names_model_and_refuses_silent_switch(self):
        from gateway.run import _explicit_model_unavailable_reply

        reply = _explicit_model_unavailable_reply(
            "deepseek-v4-flash", "custom:letovo"
        )

        assert "deepseek-v4-flash" in reply
        assert "не переключал" in reply
        assert "/model" in reply


class TestOneTurnModelOverrideRestore:
    """Verify gateway one-turn overrides restore previous session state."""

    def test_restores_previous_override(self):
        runner = _make_runner()
        sk = build_session_key(_make_source())
        previous = {
            "model": "old/model",
            "provider": "openrouter",
            "api_key": "old-key",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        }
        runner._session_model_overrides[sk] = previous

        snapshot = runner._snapshot_session_model_override(sk)
        runner._session_model_overrides[sk] = {
            "model": "temp/model",
            "provider": "anthropic",
        }

        runner._restore_session_model_override(sk, snapshot)

        assert runner._session_model_overrides[sk] == previous


class TestOneTurnNeverPersisted:
    """/model --once must never write through to the session store.

    Regression guard for the #29923 review defect: the original
    implementation wrote the once-override through set_model_override, so a
    gateway restart before the finally-restore rehydrated a supposedly
    one-turn model permanently. Drives the real _handle_model_command with
    a mocked switch pipeline and asserts on the store boundary.
    """

    @staticmethod
    def _runner_with_store(tmp_path, monkeypatch):
        import yaml as _yaml

        import gateway.run as gateway_run
        from gateway.run import GatewayRunner
        from hermes_cli.model_switch import ModelSwitchResult

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            _yaml.safe_dump(
                {"model": {"default": "old-model", "provider": "openrouter"}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
        monkeypatch.setattr(
            "hermes_cli.model_switch.switch_model",
            lambda **kw: ModelSwitchResult(
                success=True,
                new_model="gpt-5.5",
                target_provider="openrouter",
                provider_changed=False,
                api_key="sk-test",
                base_url="https://openrouter.ai/api/v1",
                api_mode="chat_completions",
                provider_label="OpenRouter",
            ),
        )
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)

        runner = object.__new__(GatewayRunner)
        runner.adapters = {}
        runner._voice_mode = {}
        runner._session_model_overrides = {}
        runner._pending_one_turn_model_restores = {}
        runner._running_agents = {}
        # async_session_store is a property over session_store; install the
        # mock behind the private cache attribute it reads.
        _store = MagicMock()
        _store.set_model_override = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @staticmethod
    def _event(text):
        from gateway.platforms.base import MessageEvent, MessageType

        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=_make_source(),
        )

    @pytest.mark.asyncio
    async def test_once_skips_session_store_write_through(
        self, tmp_path, monkeypatch
    ):
        runner = self._runner_with_store(tmp_path, monkeypatch)
        sk = build_session_key(_make_source())

        result = await runner._handle_model_command(
            self._event("/model gpt-5.5 --once")
        )

        assert result is not None and "gpt-5.5" in result
        # In-memory override installed for the next turn + restore queued...
        assert runner._session_model_overrides[sk]["model"] == "gpt-5.5"
        assert sk in runner._pending_one_turn_model_restores
        # ...but NEVER written through to the persistent session store.
        runner.async_session_store.set_model_override.assert_not_awaited()

