"""Fast-lane profile routing: «подумай» must fire when it opens ANY user message.

Owner regression (OWUI/PWA): «очень часто сообщения со словом ”подумай”
отправляются по ошибке на профиль быстрых ответов» and «это были случаи когда
в сессии были приложены файлы». Root cause (confirmed by reading api_server):

- api_server aggregates the texts of ALL user messages of a request into one
  newline-joined ``fast_lane_probe`` (:func:`_build_fast_lane_probe`).
- The TG «подумай»/«думай» marker (``telegram_prefix_marks_slow``) only
  matched ``^`` of the JOINED string, so a «подумай» opening any message
  AFTER an earlier plain/file message was silently lost → slow_mode=False
  → ``route_profile`` returned 11-fast-answers.

The fix makes the marker fire per message FRAGMENT (the probe is split back
into its per-message texts on ``\\n``), so «в начале своего сообщения»
always fires regardless of where in the multi-message request it sits, while
mid-sentence / non-imperative occurrences (``я думаю так``, ``надо подумать``,
``подумать``, ``подумайка``, ``подумай-ка``) stay fast.

These tests lock in the decision + the profile route that api_server's
``_run_agent`` would compute (``route_profile(resolve_fast_path(decision))``).
"""

from __future__ import annotations

from agent.fast_lane import (
    decide_slow_mode,
    resolve_fast_path,
    route_profile,
)
from gateway.platforms.api_server import _build_fast_lane_probe


def _route(messages):
    """Reproduce the api_server _run_agent selection for a request.

    ``messages`` is the conversation list (role/content, content may be a
    multimodal list-of-parts). Returns the profile route api_server would
    execute under — ``None`` = full lane, ``"11-fast-answers"`` = fast lane.
    """
    probe = _build_fast_lane_probe(messages) or ""
    decision = decide_slow_mode(probe, model_options={})
    decision = resolve_fast_path(decision, probe)
    return route_profile(decision), decision


# ---------------------------------------------------------------------------
# The owner regression: «подумай» opening a NON-first user message
# ---------------------------------------------------------------------------

def test_podumai_in_second_user_message_is_slow():
    """Plain message first, «подумай» second -> SLOW (full lane).

    OWUI splits a text+attachment post into separate user messages; the
    marker that opens a later message must NOT be lost by the join.
    """
    route, d = _route([
        {"role": "user", "content": "посмотри вложение"},
        {"role": "user", "content": "подумай над этим"},
    ])
    assert d.slow_mode is True, "«подумай» opening the 2nd message must be slow"
    assert d.source == "tg-prefix"
    assert route is None, f"expected full lane (None), got {route!r}"


def test_file_attached_first_then_podumai_is_slow():
    """File message first (multimodal), «подумай» second -> SLOW.

    Operator control case: «это были случаи когда в сессии были приложены
    файлы». The file contributes no probe text; the standalone «подумай ...»
    message must still fire.
    """
    route, d = _route([
        {"role": "user", "content": [
            {"type": "file", "text": "[документ приложен]", "file": {"id": "f1"}},
        ]},
        {"role": "user", "content": "подумай: разбери приложенный документ"},
    ])
    assert d.slow_mode is True
    assert d.source == "tg-prefix"
    assert route is None, f"expected full lane (None), got {route!r}"


def test_image_first_then_podumai_is_slow():
    """Image message first, «подумай что на картинке» second -> SLOW."""
    route, d = _route([
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x/i.png"}},
        ]},
        {"role": "user", "content": "Подумай что тут на картинке"},
    ])
    assert d.slow_mode is True
    assert route is None


def test_greeting_then_podumai_is_slow():
    """«Привет!» then «Подумай над задачей» -> SLOW."""
    route, d = _route([
        {"role": "user", "content": "Привет!"},
        {"role": "user", "content": "Подумай над задачей"},
    ])
    assert d.slow_mode is True
    assert route is None


def test_podumai_after_assistant_turn_is_slow():
    """A multi-message request with assistant messages between -> SLOW.

    The probe only aggregates user messages, so an assistant turn between two
    user messages must not affect the marker. «подумай» opens the final user
    fragment -> slow.
    """
    route, d = _route([
        {"role": "user", "content": "дай справку"},
        {"role": "assistant", "content": "Вот справка..."},
        {"role": "user", "content": "подумай глубже и разложи по шагам"},
    ])
    assert d.slow_mode is True
    assert route is None


# ---------------------------------------------------------------------------
# Per-message/fragment semantics on the joined probe
# ---------------------------------------------------------------------------

def test_marker_fires_when_it_opens_any_fragment_of_joined_probe():
    """A probe recovered from the session store fires per fragment."""
    # This is exactly the probe api_server builds for the multi-message case.
    probe = "обнови отчёт по прибыли\nподумай как оптимизировать"
    d = decide_slow_mode(probe, model_options={})
    assert d.slow_mode is True
    assert d.source == "tg-prefix"


def test_mid_fragment_podumai_stays_fast():
    """«подумай» NOT at fragment start in ANY fragment -> fast.

    «в середине обычной фразы» is an explicit non-goal per the operator
    clarification: «НЕ требовать срабатывания на “я думаю/что ты думаешь”».
    """
    for text in (
        "я думаю так",
        "что ты думаешь",
        "Разбери этот вопрос и подумай",
        "посмотри это и подумай потом",
        "надо подумать",
    ):
        d = decide_slow_mode(text, model_options={})
        assert d.slow_mode is False, f"{text!r} must stay fast"


def test_podumaika_and_hyphen_forms_stay_fast():
    """Partial/derived forms never fire (word-boundary contract preserved)."""
    for text in ("подумайка над этим", "подумай-ка над этим", "подумать"):
        d = decide_slow_mode(text, model_options={})
        assert d.slow_mode is False, f"{text!r} must stay fast"
        route, _ = _route([{"role": "user", "content": text}])
        assert route == "11-fast-answers", f"{text!r} routes fast"


# ---------------------------------------------------------------------------
# Session-continuation: probe merges stored history
# ---------------------------------------------------------------------------

def test_probe_builder_merges_stored_history():
    """The state.db history shape (role/content dicts) feeds the probe."""
    stored_history = [
        {"role": "user", "content": "подумай над стратегией"},
        {"role": "assistant", "content": "Готово."},
        {"role": "user", "content": "теперь добавь детали"},
    ]
    request = [{"role": "user", "content": "ещё деталей"}]

    merged = "\n".join(
        _p for _p in (
            _build_fast_lane_probe(stored_history),
            _build_fast_lane_probe(request),
        ) if _p
    )
    assert merged == "подумай над стратегией\nтеперь добавь детали\nещё деталей"
    d = decide_slow_mode(merged, model_options={})
    assert d.slow_mode is True, "«подумай» stored in earlier session msg must fire"
    assert d.source == "tg-prefix"


def test_probe_builder_skips_non_user_roles():
    probe = _build_fast_lane_probe([
        {"role": "system", "content": "Ты — помощник."},
        {"role": "user", "content": "подумай"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "ещё"},
    ])
    assert probe == "подумай\nещё"
    assert decide_slow_mode(probe, model_options={}).slow_mode is True


def test_probe_builder_empty_and_none():
    assert _build_fast_lane_probe(None) is None
    assert _build_fast_lane_probe([]) is None
    assert _build_fast_lane_probe([
        {"role": "assistant", "content": "hi"},
        {"role": "system", "content": "you"},
    ]) is None


def test_probe_builder_multimodal_text_only():
    probe = _build_fast_lane_probe([
        {"role": "user", "content": [
            {"type": "text", "text": "подумай над этим"},
            {"type": "file", "text": "[файл]", "file": {"id": "f"}},
        ]},
    ])
    assert probe == "подумай над этим"
    assert decide_slow_mode(probe, model_options={}).slow_mode is True


# ---------------------------------------------------------------------------
# Regression: unchanged markers
# ---------------------------------------------------------------------------

def test_single_podumai_still_slow():
    route, d = _route([{"role": "user", "content": "подумай над этим"}])
    assert d.slow_mode is True
    assert d.source == "tg-prefix"
    assert route is None


def test_giant_task_phrase_still_slow():
    route, d = _route([{"role": "user", "content": "это гигантская задача: спланируй"}])
    assert d.slow_mode is True
    assert d.source == "phrase"
    assert route is None


def test_deepthink_effort_high_still_slow():
    d = decide_slow_mode(
        "обычные слова",
        model_options={"reasoning": {"enabled": True, "effort": "high"}},
    )
    assert d.slow_mode is True
    assert d.source == "api-deepthink"


def test_plain_request_still_fast():
    route, d = _route([{"role": "user", "content": "обнови отчёт по прибыли"}])
    assert d.slow_mode is False
    assert route == "11-fast-answers"
