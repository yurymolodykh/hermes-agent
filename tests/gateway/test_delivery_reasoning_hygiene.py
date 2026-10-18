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


def test_condense_single_sentence_comma_joined_plan():
    """A long one-sentence 'сейчас сделаю X, потом Y, затем Z…' plan must be
    cut at the first sequence marker — it is an in-flight thought, not a final
    result (regression for t_9788fb90)."""
    plan = (
        "Сейчас сделаю полный аудит всех конфигураций сервера, затем обновлю "
        "все зависимости, потом перезапущу gateway и проверю, что всё работает "
        "корректно, после чего отправлю полный отчёт пользователю, если не будет "
        "ошибок."
    )
    assert len(plan) > 120  # above the interim floor
    out = _condense_interim(plan)
    assert len(out) < len(plan)
    assert out.startswith("Сейчас сделаю полный аудит всех конфигураций")
    assert "ошибок" not in out  # later stages dropped


def test_condense_single_sentence_no_sequence_marker():
    """A long comma-chain without an explicit sequence marker must not ride
    through verbatim — hard-cap it so only the short signal survives."""
    plan = (
        "Сейчас сделаю проверку всех конфигураций на сервере, обновлю базу "
        "данных, перезапущу gateway, сверю логи и сравню результаты с прошлым "
        "запуском, отправлю полный отчёт."
    )
    assert len(plan) > 120
    out = _condense_interim(plan)
    assert len(out) < len(plan)
    assert out.startswith("Сейчас сделаю")
    assert len(out) <= 220 + 2


def test_condense_keeps_single_sentence_short_plan():
    """A short one-sentence plan (under the floor) is left untouched."""
    plan = "Сейчас сделаю проверку конфигурации."
    assert _condense_interim(plan) == plan


# ---------------------------------------------------------------------------
# R6: съём служебного слоя (канон «деловой простой»)
# ---------------------------------------------------------------------------

def test_sanitize_drops_mission_header_segment():
    """Служебный сегмент шапки «**Модель/провайдер:** deepseek …» удаляется целиком."""
    text = (
        "✅ Kanban t_123abc done — Готово: Отчёт.\n"
        "**Модель/провайдер:** deepseek-v4-flash / custom:letovo (П.6).\n"
        "Итог: сделано."
    )
    out = sanitize_delivery_text(text)
    assert "t_123abc" not in out
    assert "deepseek" not in out
    assert "Модель/провайдер" not in out
    assert "Готово: Отчёт" in out
    assert "Итог: сделано" in out


def test_sanitize_drops_hex_ids_and_profiles():
    text = (
        "Резюме: отчёт готов, пути C:/Users/hermes/AppData/Local/Temp/x.html, "
        "канбан t_a1b2c3d4e5f6, профиль 05-web-researcher."
    )
    out = sanitize_delivery_text(text)
    assert "t_a1b2c3d4e5f6" not in out
    assert "05-web-researcher" not in out
    assert "C:/Users/hermes" not in out
    assert "отчёт готов" in out


def test_sanitize_keeps_subject_model_terms():
    """Предметные названия моделей/квантов НЕ удаляются (антирегрессия §Б1)."""
    text = "Отчёт Б: DeepSeek V4-Flash — лидер, 284B, 2-bit квант, 120 ГБ Mac."
    assert sanitize_delivery_text(text) == text


def test_sanitize_mission_cclean_empty_ok():
    """Полностью служебная строка сходит в пустоту без исключения."""
    out = sanitize_delivery_text("**Миссия:** t_x · **Контроллер:** 01-orchestrator")
    assert isinstance(out, str)


def test__strip_mission_slang_idempotent():
    from hermes_cli.delivery_policy import _strip_mission_slang
    t = "✅ Готово — Отчёт. t_abc 01-orchestrator C:/Users/x/y"
    once = _strip_mission_slang(t)
    twice = _strip_mission_slang(once)
    assert twice == once


