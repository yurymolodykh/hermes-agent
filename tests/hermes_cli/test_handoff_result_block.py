"""Tests for the structured handoff result-block contract (H3, t_084230a5).

Covers parsing, detection, canonical rendering, chat-line extraction, and the
metadata-diff helper. All pure string tests — no DB, no disk.
"""

from __future__ import annotations

import pytest

from hermes_cli.handoff_result_block import (
    BLOCK_MARKER,
    chat_line,
    has_result_block,
    metadata_diff,
    parse_result_block,
    render_result_block,
)

CANONICAL = """RESULT-BLOCK v1
FACTS:
- p50 короткого вопроса 25 мин (было 40)
- 3 артефакта в workspace
PATHS:
- C:/tmp/report.md
LIMITATIONS:
- ручная проверка на экране пользователя недоступна (manual_review)
"""


def test_detects_canonical_block():
    assert has_result_block(CANONICAL)
    assert not has_result_block("Просто summary без маркера")
    assert not has_result_block(None)
    assert not has_result_block("")


def test_detects_inline_block():
    inline = "RESULT-BLOCK v1 FACTS: a; b PATHS: p"
    assert has_result_block(inline)


@pytest.mark.parametrize(
    "header",
    [
        "RESULT-BLOCK v1",
        "result-block v2",
        " Result-Block V1 ",
    ],
)
def test_header_case_insensitive(header):
    text = header + "\nFACTS:\n- x"
    assert has_result_block(text)


def test_parse_canonical():
    parsed = parse_result_block(CANONICAL)
    assert parsed is not None
    assert parsed["FACTS"] == ["p50 короткого вопроса 25 мин (было 40)", "3 артефакта в workspace"]
    assert parsed["PATHS"] == ["C:/tmp/report.md"]
    assert parsed["LIMITATIONS"] == ["ручная проверка на экране пользователя недоступна (manual_review)"]


def test_parse_legacy_returns_none():
    assert parse_result_block("Просто summary") is None
    assert parse_result_block("Первая строка\nвторая строка") is None


def test_parse_empty_body_after_header():
    assert parse_result_block("RESULT-BLOCK v1\n") == {}


def test_parse_missing_sections():
    text = "RESULT-BLOCK v1\nFACTS:\n- fact1\nPATHS:\n"
    parsed = parse_result_block(text)
    assert parsed["FACTS"] == ["fact1"]
    # PATHS пустая → секция опущена
    assert "PATHS" not in parsed


def test_parse_inline():
    inline = "RESULT-BLOCK v1 FACTS: a; b PATHS: C:/tmp/x LIMITATIONS: none"
    parsed = parse_result_block(inline)
    assert parsed["FACTS"] == ["a", "b"]
    assert parsed["PATHS"] == ["C:/tmp/x"]
    assert parsed["LIMITATIONS"] == ["none"]


def test_render_canonical_normalizes():
    messy = "RESULT-BLOCK v1\nFACTS:\n- a\n - a \nPATHS:\n-  C:/x  \n- c:/x\nLIMITATIONS:\n- l1"
    rendered = render_result_block(messy)
    assert rendered is not None
    # дедуп: дубликат 'a' убран; 'C:/x' и 'c:/x' — один путь (Windows регистр не значим)
    assert rendered.count("- a") == 1
    assert "- C:/x" in rendered
    assert "- c:/x" not in rendered


def test_render_legacy_none():
    assert render_result_block("обычный текст") is None


def test_render_roundtrip_canonical():
    rendered = render_result_block(CANONICAL)
    assert rendered is not None
    assert rendered.startswith("RESULT-BLOCK v1")
    assert "FACTS:" in rendered
    assert "PATHS:" in rendered
    assert "LIMITATIONS:" in rendered
    # секции в каноническом порядке
    assert rendered.index("FACTS:") < rendered.index("PATHS:") < rendered.index("LIMITATIONS:")


def test_chat_line_legacy_is_first_line():
    assert chat_line("Отчёт готов\nПродолжение") == "Отчёт готов"
    assert chat_line(None) is None
    assert chat_line("") is None


def test_chat_line_block_prefers_first_fact():
    assert chat_line(CANONICAL) == "p50 короткого вопроса 25 мин (было 40)"


def test_chat_line_block_summary_override():
    text = "RESULT-BLOCK v1\nFACTS:\n- f1\nSUMMARY: Краткое резюме\n- f2"
    # SUMMARY строка вне секции — только как сводка для чата
    assert chat_line(text).startswith("Краткое резюме")


def test_chat_line_block_no_facts_uses_summary():
    text = "RESULT-BLOCK v1\nSUMMARY: Готово всё\nLIMITATIONS:\n- l"
    assert chat_line(text) == "Готово всё"


def test_metadata_diff_defaults():
    assert metadata_diff(None) == {}
    assert metadata_diff({}) == {}
    assert metadata_diff({"model": "deepseek"}) == {}


def test_metadata_diff_structured():
    meta = {
        "changed_files": ["a.py", "b.py"],
        "tests_run": 12,
        "findings": ["fixed x"],
        "artifacts": ["C:/tmp/r.md"],
        "limitations": [],
        "evidence": ["cmd out"],
    }
    diff = metadata_diff(meta)
    assert diff["changed_files"] == ["a.py", "b.py"]
    assert diff["tests_run"] == ["12"]
    assert diff["findings"] == ["fixed x"]
    assert diff["artifacts"] == ["C:/tmp/r.md"]
    # пустые списки не попадают
    assert "limitations" not in diff


def test_metadata_diff_scalar_string():
    assert metadata_diff({"changed_files": "single.py"})["changed_files"] == ["single.py"]
