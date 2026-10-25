"""Unit tests: PWA-source (yadisk_bridge) RGZ trigger pinning (task t_b030b28a).

The W2 follow-up established that the «подумай» and «гигантская задача»
triggers must ALSO work on the PWA path (PWA -> bridge 8765 -> ingress 8766 ->
gateway/run.py) — not only on api_server/telegram. The RGZ mechanical
guarantee (owner decision 2026-10-20) lives in ``agent/rgz_reasoning.py`` and
is deliberately source-agnostic: it fires for ANY session key.

These tests lock in the PWA-source contract (analog to
``tests/agent/test_rgz_reasoning.py``):

1. **RGZ trigger fires for a PWA/yadisk_bridge session key.** The live PWA
   session key format is ``agent:main:yadisk_bridge:dm:<chat_id>`` (observed
   in gateway.log / agent.log on 2026-10-24). A message carrying
   «гигантский план»/«гигантская задача»/«гигантская миссия»/«гигантский
   проект» MUST pin that session to ``reasoning=max`` — the same field as
   ``/reasoning --session`` writes.
2. **PWA source is never an exclusion.** ``apply_rgz_session_override`` takes
   no ``source`` argument and has no source allow-list/deny-list — prove the
   PWA (yadisk_bridge) session key pins exactly like every other source.
3. **«подумай» marker fires on the PWA wire.** The fast-lane text marker
   (design §3 marker 2) drives `slow_mode` for the PWA path too (source
   ``tg-prefix``). This is the same text-marker catalogue the PWA bridge
   delivers verbatim (verified by W3 transport audit). Note: «подумай» is a
   slow-lane marker (full lane, review preserved) — the reasoning=max pin is
   the RGZ phrase guarantee (§1), not the «подумай» marker.
4. **Non-string payload never crashes.** The hook degrades safely on any
   input shape; a PWA turn never breaks on the trigger check.
5. **The wire form is exactly `reasoning_effort: max`** —
   ``MAX_REASONING_CONFIG == {"enabled": True, "effort": "max"}``, the
   same value the api_server path pins to for a giant-task request.

Live verification (2026-10-24, PWA/yadisk_bridge wire, agent.log:18286):
`rgz_reasoning: RGZ trigger — session agent:main:yadisk_bridge:dm:32de449e…
pinned to reasoning=max until cleared` — the PWA-source pin was observed live.

Nothing here touches profile 11, auxiliary config, or the reasoning-effort
floor — the test suite is pure and runs on the default (non-integration)
pytest path.
"""

from __future__ import annotations

import inspect

from agent.fast_lane import (
    contains_always_slow_phrase,
    decide_slow_mode,
    strip_telegram_slow_prefix,
)
from agent.rgz_reasoning import (
    MAX_REASONING_CONFIG,
    apply_rgz_session_override,
    rgz_trigger_detected,
)

# ---------------------------------------------------------------------------
# PWA-source session key shapes
# ---------------------------------------------------------------------------

# Live shape observed on 2026-10-24 (gateway.log/agent.log, yadisk_bridge
# platform):  agent:main:<platform>:<chat_type>:<chat_id>
PWA_SESSION_KEY = "agent:main:yadisk_bridge:dm:3f9c1c42-8b3a-4b2d-9e01-517f0aa1c0e0"
# A second PWA chat (multi-chat isolation — only the triggered one pins).
PWA_SESSION_KEY_2 = "agent:main:yadisk_bridge:dm:7d21e6f0-44c5-4a10-9b86-6e74b84f2a1a"


class _FakeRunner:
    """Runner stub recording ``_set_session_reasoning_override`` calls."""

    def __init__(self):
        self.overrides = {}
        self.calls = []

    def _set_session_reasoning_override(self, key, cfg):
        self.calls.append((key, dict(cfg) if cfg is not None else None))
        if cfg is None:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = dict(cfg)


# ---------------------------------------------------------------------------
# 1. RGZ trigger fires for a PWA/yadisk_bridge session key
# ---------------------------------------------------------------------------

def test_pwa_source_giant_plan_pins_session_to_max():
    runner = _FakeRunner()
    assert apply_rgz_session_override(
        runner, PWA_SESSION_KEY, "Составь гигантский план внедрения РГЗ"
    ) is True
    assert runner.overrides == {PWA_SESSION_KEY: MAX_REASONING_CONFIG}


def test_pwa_source_giant_task_pins_session_to_max():
    runner = _FakeRunner()
    assert apply_rgz_session_override(
        runner, PWA_SESSION_KEY, "Это гигантская задача, нужен декомпоз"
    ) is True
    assert runner.overrides == {PWA_SESSION_KEY: MAX_REASONING_CONFIG}


def test_pwa_source_all_four_rgz_phrases_trigger():
    for phrase in ("гигантский план", "гигантская задача",
                   "гигантская миссия", "гигантский проект"):
        assert rgz_trigger_detected(f"нужен {phrase} по продвижению") is True, phrase


def test_pwa_source_plain_request_does_not_pin():
    runner = _FakeRunner()
    assert apply_rgz_session_override(
        runner, PWA_SESSION_KEY, "Обнови статус по отчёту"
    ) is False
    assert runner.calls == []


def test_pwa_source_multichat_isolation_only_triggered_session_pins():
    """Only the PWA chat that carried the trigger gets pinned."""
    runner = _FakeRunner()
    assert apply_rgz_session_override(
        runner, PWA_SESSION_KEY, "гигантская задача по приложенному брифу"
    ) is True
    # The other PWA chat stays untouched.
    assert apply_rgz_session_override(
        runner, PWA_SESSION_KEY_2, "привет, просто вопрос"
    ) is False
    assert set((k, tuple(sorted(v.items()))) for k, v in runner.calls) == {
        (PWA_SESSION_KEY, tuple(sorted(MAX_REASONING_CONFIG.items())))
    }


# ---------------------------------------------------------------------------
# 2. PWA source is never an exclusion (no source allow/deny gate)
# ---------------------------------------------------------------------------

def test_pwa_source_key_variants_all_trigger():
    """Every PWA/source-shaped session key triggers; nothing blocks on source."""
    variants = [
        "agent:main:yadisk_bridge:dm:chat-1",
        "agent:main:yadisk_bridge:group:space-9",
        "agent:main:pwa:dm:chat-2",
        "agent:main:api_server:dm:chat-3",
        "agent:main:telegram:dm:125805788",
    ]
    for key in variants:
        runner = _FakeRunner()
        assert apply_rgz_session_override(
            runner, key, "Составь гигантский план"
        ) is True, key
        assert runner.overrides.get(key) == MAX_REASONING_CONFIG, key


def test_override_function_has_no_source_argument():
    """The RGZ hook takes (runner, session_key, text) — there is no source
    parameter and therefore no per-source gating anywhere in the call path."""
    sig = inspect.signature(apply_rgz_session_override)
    params = list(sig.parameters)
    assert params == ["runner", "session_key", "text"], params


# ---------------------------------------------------------------------------
# 3. «подумай» marker fires on the PWA wire (fast-lane catalogue)
# ---------------------------------------------------------------------------

def test_pwa_podumay_marker_slows_and_originates_tg_prefix():
    d = decide_slow_mode(text="подумай: разбери этот отчёт")
    assert d.slow_mode is True
    assert d.source == "tg-prefix"


def test_pwa_podumay_strips_and_keeps_rgz_phrase() -> None:
    """The «подумай» marker and the RGZ phrase coexist on the PWA wire."""
    text = "подумай над гигантской задачей по брифу"
    d = decide_slow_mode(text=text)
    assert d.slow_mode is True
    stripped = strip_telegram_slow_prefix(text)
    assert stripped == "над гигантской задачей по брифу"


def test_pwa_multimodal_rgz_phrase_never_crashes() -> None:
    """The PWA bridge sends plain-string text, so a multimodal list payload is
    the api_server/OWUI shape — not the primary PWA wire. This locks in that
    the RGZ hook never crashes on non-string content: on this branch the
    coercion is ``str(text)``, whose repr still embeds the phrase text (so the
    trigger actually fires); whatever the outcome, a PWA turn must never break
    on the trigger check."""
    multimodal = [
        {"type": "text", "text": "Составь гигантский план внедрения"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    # str() repr contains "гигантский план" -> regex matches -> fires. Never raises.
    assert rgz_trigger_detected(multimodal) is True
    # contains_always_slow_phrase requires a string on this branch.
    assert contains_always_slow_phrase("Составь гигантский план внедрения") is True


def test_pwa_plain_string_rgz_phrase_triggers() -> None:
    """The PWA wire delivers plain-string text; the RGZ phrase fires on it."""
    assert rgz_trigger_detected("Составь гигантский план внедрения") is True
    assert rgz_trigger_detected("Нужна гигантская задача по брифу") is True


def test_pwa_plain_fast_request_not_slow() -> None:
    """Absence of markers keeps the fast lane (D1) — no accidental slowdown."""
    d = decide_slow_mode(text="Скажи просто OK")
    assert d.slow_mode is False
    assert d.source == "no-marker"


# ---------------------------------------------------------------------------
# 4. Wire form is exactly reasoning_effort=max
# ---------------------------------------------------------------------------

def test_max_reasoning_config_is_exact_wire_form() -> None:
    """MAX_REASONING_CONFIG must translate to POST-body reasoning_effort=max."""
    assert MAX_REASONING_CONFIG == {"enabled": True, "effort": "max"}
    # Downstream wire translation treats effort "max" as the top-level
    # reasoning_effort in the POST body (skill: wiring guarantees
    # reasoning_config{enabled:true, effort:max} -> POST reasoning_effort: max).
    assert MAX_REASONING_CONFIG["enabled"] is True
    assert MAX_REASONING_CONFIG["effort"] == "max"


def test_pwa_pinned_session_override_serialises_to_max_effort() -> None:
    """The session override is the /reasoning --session field; when a PWA
    session carries it, the effort resolves to max (same as an explicit
    command)."""
    runner = _FakeRunner()
    apply_rgz_session_override(runner, PWA_SESSION_KEY, "гигантская задача X")
    override = runner.overrides[PWA_SESSION_KEY]
    assert override.get("enabled") is True
    assert override.get("effort") == "max"
