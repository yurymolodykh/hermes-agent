"""Структурированный «результат-блок» для handoff (раздел Н3, РГЗ system-speed-2x, карточка t_084230a5).

Контракт: ``summary``/``metadata`` завершённого kanban-задания должны нести
компактный структурированный результат, а не пересказ всей работы. Формат:

    RESULT-BLOCK v1
    FACTS:
    - <подтверждённый факт с числом/единицей>
    - ...
    PATHS:
    - <абсолютный путь артефакта>
    LIMITATIONS:
    - <открытое ограничение / uncertainty / manual_review>

Секции опциональны, но по контракту рекомендуется заполнять все три.
Порядок секций фиксирован: FACTS, PATHS, LIMITATIONS (парсер не зависит от
порядка, но шаблон для записи — такой).

Парсер реализует и компактную inline-форму для однострочных handoff::

    RESULT-BLOCK v1 FACTS: f1; f2 PATHS: p1 LIMITATIONS: l1

Функции модуля:
  - ``parse_result_block(text)`` — разбор текста (summary) в dict секций.
  - ``has_result_block(text)`` — детектор формата (надёжный, по маркеру первой
    строки/маркера RESULT-BLOCK).
  - ``render_result_block(text)`` — каноническая пере-рендерка уже существующего
    блока (нормализация пробелов, дедуп буллетов), либо ``None``, если текст не
    в контрактном формате (легаси — рендерится как раньше).
  - ``chat_line(text)`` — первая строка-факт/сводка для канала-нотификатора:
    если это результат-блок, возвращает первую FACTS-строку (или строку
    ``SUMMARY:``, если она есть); иначе первую непустую строку исходника.
  - ``metadata_diff(metadata)`` — какие структурированные ключи в ``metadata``
    НЕ покрыты результа-блоком summary (для аддитивного, без дублирования,
    рендера в ``build_worker_context``).

Формат задуман backward-compatible: если текст не содержит маркера
``RESULT-BLOCK``, все функции возвращают ``None`` / легаси-значения и
существующее поведение не меняется.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

BLOCK_MARKER = "RESULT-BLOCK"
VERSION = "v1"

# Секции в каноническом порядке.
_SECTIONS = ("FACTS", "PATHS", "LIMITATIONS")

# Заголовок секции в начале строки (регистр не важен, допускаются пробелы).
_SECTION_HEADER_RE = re.compile(r"^\s*(FACTS|PATHS|LIMITATIONS)\s*:\s*$", re.IGNORECASE)
# Буллет.
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
# Открывающий маркер блока: может быть сам по себе или с inline-контентом.
_BLOCK_HEADER_RE = re.compile(r"^\s*RESULT-BLOCK(?:\s+(v\d+))?\s*$", re.IGNORECASE)
# текст, начинающийся с маркера (без перевода строки после него) → inline-форма.
# ``[ \t]+`` между версией и контентом гарантирует, что inline-контент лежит на
# той же строке, что и маркер (многострочный канон уходит в блочный парсер).
_INLINE_RE = re.compile(
    r"^\s*RESULT-BLOCK(?:\s+(v\d+))?[ \t]+((?:FACTS|PATHS|LIMITATIONS)\s*:.*)$",
    re.IGNORECASE,
)

# Ключи metadata, которые контракт считает «структурированными фактами» блока.
# Это те же имена, что рекомендует kanban_complete (changed_files/tests_run/
# findings) + артефакты/ограничения контракта.
METADATA_STRUCTURED_KEYS = (
    "changed_files",
    "tests_run",
    "findings",
    "artifacts",
    "limitations",
    "evidence",
)


def has_result_block(text: Optional[str]) -> bool:
    """True, когда ``text`` начинается с маркера ``RESULT-BLOCK`` (канон или inline)."""
    if not text:
        return False
    head = text.lstrip()
    return head.upper().startswith(BLOCK_MARKER)


def _split_inline(rest: str) -> Dict[str, List[str]]:
    """Разобрать inline-хвост ``FACTS: a; b PATHS: p LIMITATIONS: l``."""
    sections: Dict[str, List[str]] = {}
    # Ищем вхождения заголовков секций в хвосте (первая строка).
    parts = re.split(r"\s+(?=(?:FACTS|PATHS|LIMITATIONS)\s*:)", rest, flags=re.IGNORECASE)
    for part in parts:
        m = re.match(r"^\s*(FACTS|PATHS|LIMITATIONS)\s*:\s*(.*)$", part, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        name = m.group(1).upper()
        body = m.group(2).strip()
        items = [x.strip() for x in re.split(r"[;]", body) if x.strip()]
        sections.setdefault(name, []).extend(items)
    return sections


def parse_result_block(text: Optional[str]) -> Optional[Dict[str, List[str]]]:
    """Разобрать текст handoff в структурированный результат-блок.

    Возвращает ``{"FACTS": [...], "PATHS": [...], "LIMITATIONS": [...]}``
    (пустые секции могут отсутствовать), либо ``None``, если текст не в
    контрактном формате — легаси-текст, который рендерится по-старому.
    """
    if not has_result_block(text):
        return None
    body = text.lstrip()
    # Inline-форма: маркер и сразу первая секция в той же строке.
    m = _INLINE_RE.match(body)
    if m:
        return _split_inline(m.group(2))

    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    lines = body.splitlines()
    started = False
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if not started:
            if _BLOCK_HEADER_RE.match(s):
                started = True
            continue
        hm = _SECTION_HEADER_RE.match(s)
        if hm:
            current = hm.group(1).upper()
            continue
        bm = _BULLET_RE.match(s)
        if bm and current is not None:
            val = bm.group(1).strip()
            if val:
                sections.setdefault(current, []).append(val)
        # Строки без секции и без буллета игнорируем (переносимая строка факта
        # вне секции не считается частью блока).
    if not sections:
        return {}
    return sections


def _dedup(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for it in items:
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


def render_result_block(text: Optional[str]) -> Optional[str]:
    """Каноническая пере-рендерка результат-блока.

    Возвращает ``None`` для легаси-текста (без маркера); иначе — нормализованный
    блок в каноническом виде (секции FACTS/PATHS/LIMITATIONS, дедуп буллетов).
    """
    parsed = parse_result_block(text)
    if parsed is None:
        return None
    lines = [f"{BLOCK_MARKER} {VERSION}"]
    for name in _SECTIONS:
        items = _dedup(parsed.get(name, []))
        if not items:
            continue
        lines.append(f"{name}:")
        lines.extend(f"- {it}" for it in items)
    return "\n".join(lines)


def chat_line(text: Optional[str]) -> Optional[str]:
    """Краткая строка для канального нотификатора.

    Для результат-блока — первая FACTS-строка (а если есть строка
    ``SUMMARY:``, то она); для легаси — первая непустая строка. ``None`` при
    пустом входе. Результат не превышает ``max_chars``.
    """
    if not text:
        return None
    body = text.strip()
    if not body:
        return None
    parsed = parse_result_block(body) if has_result_block(body) else None
    if parsed is None:
        return body.splitlines()[0] or None
    # SUMMARY: — опциональная строка-сводка сразу после FACTS (не секция).
    for ln in body.splitlines():
        s = ln.strip()
        if s.upper().startswith("SUMMARY:"):
            v = s.split(":", 1)[1].strip()
            if v:
                return v
    facts = parsed.get("FACTS") or []
    if facts:
        return facts[0]
    return body.splitlines()[0] or None


def metadata_diff(metadata: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Какие структурированные ключи metadata контрактно несут информацию.

    Используется рендерером ``build_worker_context``, чтобы при наличии
    результат-блока в summary не дублировать уже покрытые факты полным
    JSON-дампом metadata, а доносить только то, чего в блоке нет.
    """
    if not isinstance(metadata, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for key in METADATA_STRUCTURED_KEYS:
        val = metadata.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            items = [str(x) for x in val if str(x).strip()]
        elif isinstance(val, (int, float)):
            items = [str(val)]
        elif isinstance(val, str) and val.strip():
            items = [val]
        else:
            items = []
        if items:
            out[key] = items
    return out
