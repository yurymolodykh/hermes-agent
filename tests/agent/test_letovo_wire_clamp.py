"""Wire-level clamp for the letovo endpoint (incident 2026-10-31, HTTP 400).

The defect: a reasoning level that the letovo backend does not serve
(``medium``/``minimal``) reached the wire as a top-level ``reasoning_effort``
and the request failed with

    HTTP 400 "DeepSeek V4.1 reasoning_effort must be low, high, xhigh, max,
    or an integer within [1, 100] in chat_template_kwargs"

It surfaced because ``provider: letovo`` has no bundled provider profile, so it
resolves to the generic ``custom`` profile, which clamped against the widest
OpenAI-compatible vocabulary (which contains ``medium``) instead of the domain
the endpoint actually serves.

Operator acceptance criterion (2026-10-31): a session whose reasoning_config
still carries a foreign ``medium`` pin must NOT put ``medium`` on the wire.
"""
from __future__ import annotations

import pytest

LETOVO_BASE = "https://llm.letovo.site/openai"


@pytest.fixture(scope="module")
def custom_profile():
    from providers import get_provider_profile

    prof = get_provider_profile("custom")
    assert prof is not None, "the custom provider profile must resolve"
    return prof


def _emit(prof, effort: str, base_url: str = LETOVO_BASE, model: str = "deepseek-v4-flash"):
    extra_body, top_level = prof.build_api_kwargs_extras(
        reasoning_config={"effort": effort, "enabled": True},
        model=model,
        base_url=base_url,
    )
    return extra_body, top_level


class TestLetovoDomainDetection:
    def test_letovo_host_is_recognised(self):
        from agent.reasoning_effort import LETOVO_EFFORTS, LETOVO_OVERRIDES, letovo_wire_domain

        assert letovo_wire_domain(base_url=LETOVO_BASE) == (LETOVO_EFFORTS, LETOVO_OVERRIDES)

    def test_host_match_ignores_case_and_trailing_slash(self):
        from agent.reasoning_effort import letovo_wire_domain

        assert letovo_wire_domain(base_url="https://LLM.LETOVO.SITE/openai/") is not None
        assert letovo_wire_domain(base_url="https://llm.letovo.site") is not None

    def test_other_hosts_are_not_claimed(self):
        from agent.reasoning_effort import letovo_wire_domain

        for url in (
            "https://api.openai.com/v1",
            "https://other.example.test/openai",
            "https://notletovo.site/openai",
            "",
            None,
        ):
            assert letovo_wire_domain(base_url=url) is None, url


class TestLetovoWireClamp:
    """The operator's AC: no rejected level survives to the wire."""

    @pytest.mark.parametrize(
        "session_effort,expected",
        [
            ("medium", "high"),
            ("minimal", "low"),
            ("ultra", "max"),
            ("xhigh", "xhigh"),
            ("none", "none"),
            ("low", "low"),
            ("high", "high"),
            ("max", "max"),
        ],
    )
    def test_session_pin_is_clamped_on_the_wire(self, custom_profile, session_effort, expected):
        _extra, top = _emit(custom_profile, session_effort)
        assert top.get("reasoning_effort") == expected

    @pytest.mark.parametrize("rejected", ["medium", "minimal"])
    def test_rejected_level_never_reaches_the_wire(self, custom_profile, rejected):
        _extra, top = _emit(custom_profile, rejected)
        assert top.get("reasoning_effort") != rejected
        assert rejected not in str(top)

    def test_foreign_pin_beats_nothing_but_never_leaks(self, custom_profile):
        # Incident shape: session carries medium, profile config says low.
        # Whatever the profile wants, medium must not be what is sent.
        for attempted in ("medium", "minimal"):
            _extra, top = _emit(custom_profile, attempted)
            assert top["reasoning_effort"] in {"low", "high", "xhigh", "max", "none"}

    def test_bespoke_level_is_omitted_not_forwarded(self, custom_profile):
        # Closed domain: an unknown name has no safe spelling -> omit the field
        # so the backend applies its own default instead of answering 400.
        _extra, top = _emit(custom_profile, "turbo")
        assert "reasoning_effort" not in top

    def test_disabled_sends_none(self, custom_profile):
        extra_body, top = custom_profile.build_api_kwargs_extras(
            reasoning_config={"effort": "high", "enabled": False},
            model="deepseek-v4-flash",
            base_url=LETOVO_BASE,
        )
        assert top.get("reasoning_effort") == "none"
        assert extra_body.get("think") is False

    def test_non_letovo_endpoint_keeps_generic_clamp(self, custom_profile):
        # Regression guard: the letovo branch must not change behaviour for
        # other custom endpoints, which legitimately serve medium.
        _extra, top = _emit(
            custom_profile, "medium", base_url="https://api.glm.example/v1"
        )
        assert top.get("reasoning_effort") == "medium"
