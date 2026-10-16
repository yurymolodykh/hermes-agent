"""Reasoning/interim delivery-hygiene: sanitize_delivery_text.

Covers the shared sanitizer in ``hermes_cli/delivery_policy`` used at the
OWUI (api_server) and Telegram kanban delivery boundary (F1): reasoning_content
never reaches the channels, and long "сейчас сделаю…" interim plans are
condensed to a short "work in progress" signal.
"""

from hermes_cli.delivery_policy import (
    _condense_interim,
    sanitize_delivery_text,
)


def test_sanitize_drops_reasoning_content_field():
    text = "reasoning_content: нужно проверить базу данных\n\nИтог: 3 файла обновлены."
    assert "reasoning_content" not in sanitize_delivery_text(text)
    assert "3 файла обновлены" in sanitize_delivery_text(text)


def test_sanitize_drops_think_blocks():
    text = (
        "Сделано.\n\n"
        "Начинаю проверку.\n\n"
        "Итог: всё ок."
    )
    out = sanitize_delivery_text(text)
    assert "Начинаю проверку" in out
    assert out.strip()


def test_sanitize_drops_fenced_reasoning():
    text = "```reasoning\nпроверяю данные\n```\nИтог: готово."
    out = sanitize_delivery_text(text)
    assert "проверяю данные" not in out
    assert "готово" in out


def test_sanitize_condenses_long_interim_plan():
    text = (
        "Сейчас сделаю проверку всех конфигураций на сервере, затем обновлю "
        "базу данных, потом перезапущу gateway, после чего проверю логи и "
        "сравню результаты с прошлым запуском, затем отправлю отчёт "
        "пользователю и закрою задачу, если проверка пройдёт успешно. "
        "Продолжаю работу в фоне, это займёт некоторое время."
    )
    out = sanitize_delivery_text(text)
    # Long interim plan is condensed well under the original length.
    assert len(out) < len(text)
    assert len(out) <= 220 + 2


def test_sanitize_keeps_short_interim():
    text = "Сейчас сделаю проверку."
    assert sanitize_delivery_text(text) == text


def test_sanitize_keeps_normal_final_report():
    text = "Задача выполнена. Обновлены 3 файла, настроен gateway, прогонены тесты, все зелёные."
    assert sanitize_delivery_text(text) == text


def test_sanitize_handles_non_string():
    assert sanitize_delivery_text(None) == ""
    assert sanitize_delivery_text(123) == "123"


def test_sanitize_max_chars():
    text = "а" * 500
    assert len(sanitize_delivery_text(text, max_chars=300)) <= 300
    assert len(sanitize_delivery_text(text)) == 500  # no cap by default


def test_condense_interim_first_sentence_only():
    text = (
        "Сейчас сделаю A. Затем продолжу с B и проверю результаты, потом сделаю C, "
        "потом D, потом E и доложу о завершении всего объёма работ по этому списку, "
        "который потребует ещё много дополнительных шагов и времени на выполнение."
    )
    out = _condense_interim(text)
    assert out == "Сейчас сделаю A."
    assert len(out) < len(text)


def test_condense_keeps_completed_narrative():
    """A long summary that reports done work must NOT lose its results."""
    done = (
        "Сначала я проверил конфиг, потом обновил базу, затем перезапустил "
        "gateway и прогнал тесты — все 14 зелёные, артефакты выложены, "
        "задача готова к сдаче."
    )
    assert _condense_interim(done) == done


def test_condense_requires_stamp():
    """Only texts that BEGIN with an interim stamp can be condensed."""
    plan_no_stamp = (
        "Проверю все конфигурации сервера, потом обновлю базу данных, затем "
        "перезапущу gateway, после проверю логи и сверю результаты с прошлым "
        "запуском, а потом отправлю отчёт пользователю и закрою задачу."
    )
    assert _condense_interim(plan_no_stamp) == plan_no_stamp


def test_sanitize_idempotent():
    text = "reasoning_content: шум\n\nИтог: выполнено за 5 минут."
    once = sanitize_delivery_text(text)
    twice = sanitize_delivery_text(once)
    assert twice == once
