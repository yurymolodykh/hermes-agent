from agent.adaptive_model_router import route_turn

CONFIG = {
    "enabled": True,
    "surfaces": ["telegram"],
    "models": {
        "small": {"provider": "letovo", "model": "deepseek-v4-flash"},
        "medium": {"provider": "openai-codex", "model": "gpt-5.6-terra"},
        "large": {"provider": "openai-codex", "model": "gpt-5.6-terra"},
        "xlarge": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "critical": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    },
    "explicit_models": {
        "terra": {"provider": "openai-codex", "model": "gpt-5.6-terra"},
        "sol": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "letovo": {"provider": "letovo", "model": "deepseek-v4-flash"},
    },
}


def route(text, **kwargs):
    return route_turn(text, CONFIG, current_model="gpt-5.6-terra", current_provider="openai-codex", surface="telegram", **kwargs)


def test_all_configured_tiers_are_reachable():
    assert route("Ответь одним словом").tier == "small"
    assert route("Объясни DNS").tier == "medium"
    assert route("Создай и проверь HTML-отчёт").tier == "large"
    assert route("Проведи аудит с точными ссылками на первичные источники").tier == "xlarge"
    assert route("Дай юридическое заключение перед необратимой публикацией").tier == "critical"


def test_tier_models_follow_policy():
    assert route("Ответь одним словом").model == "deepseek-v4-flash"
    assert route("Объясни DNS").model == "gpt-5.6-terra"
    assert route("Создай файл").model == "gpt-5.6-terra"
    assert route("Проведи нормативный аудит").model == "gpt-5.6-sol"
    assert route("Дай правовой вывод").model == "gpt-5.6-sol"


def test_explicit_user_and_session_choice_win():
    assert route("Используй Terra и проведи нормативный аудит").model == "gpt-5.6-terra"
    choice = route("Ответь одним словом", explicit_session_override=True)
    assert choice.model == "gpt-5.6-terra"
    assert choice.explicit


def test_provider_switch_for_small_tier_is_preserved():
    decision = route("Быстро: да или нет")
    assert (decision.provider, decision.model) == ("letovo", "deepseek-v4-flash")


def test_disabled_or_out_of_scope_policy_keeps_baseline():
    disabled = dict(CONFIG, enabled=False)
    decision = route_turn("создай файл", disabled, current_model="base", current_provider="base-provider", surface="telegram")
    assert (decision.model, decision.provider) == ("base", "base-provider")
    decision = route_turn("создай файл", CONFIG, current_model="base", current_provider="base-provider", surface="discord")
    assert (decision.model, decision.provider) == ("base", "base-provider")
