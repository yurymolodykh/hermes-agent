"""SOUL seed for a person's own profile (``01-<login>``) and the personalization questionnaire.

Why this module exists: ``create_profile()`` with no flags seeds ``DEFAULT_SOUL_MD`` — one
sentence of generic text ("You are Hermes Agent, built by Nous Research"), so a new person's
profile starts with no role and no working rules. ``--clone-config`` is the other extreme: it
copies the orchestrator's whole SOUL, including its routing table over the shared specialist
profiles, its execution modes, the Kanban protocol and the review matrix — instructions a user's
own assistant must not follow.

So a personal root gets a *clone with the service sections removed*: tone, invariants, source
discipline, memory rules and the reply contract stay; the orchestrator's routing, modes, board
protocol and review matrix do not. What remains is then narrowed by the questionnaire
(:data:`QUESTIONNAIRE_FILENAME`), which the person fills in on first entry:
:func:`apply_questionnaire` rewrites only the sections the answers name, byte-for-byte elsewhere.

The same transformation used to live in an ops script; it lives here so the platform's create
path, the API and the CLI all seed the same text and the ops file is only a CLI wrapper.

This module is stdlib-only and never imports the profile stack at import time, so seeding a
profile cannot cycle back into ``hermes_cli.profiles``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

PERSONAL_ROOT_PREFIX = "01-"

#: Written into a personal root's own directory next to SOUL.md.
QUESTIONNAIRE_FILENAME = "SOUL-QUESTIONNAIRE.md"

#: Bundled fallback source (kept sections only) — used when no orchestrator SOUL is readable,
#: e.g. on an install with no ``01-orchestrator`` profile. Same shape as a filtered source.
_BUNDLED_BASE = Path(__file__).parent / "hermes_cli" / "data" / "user_soul_base.md"
_BUNDLED_QUESTIONNAIRE = Path(__file__).parent / "hermes_cli" / "data" / "soul-questionnaire.md"

#: Sections owned by the orchestrator's service role. Never carried into a user's SOUL.
STRIP_SECTIONS = frozenset({
    "Выбор режима выполнения",
    "Режимы работы (триаж запроса)",
    "Маршрутизация",
    "Матрица проверки",
    "Self-check первой подачи (P6)",
    "Checkpoint марафонов (P7)",
    "Модель и handoff",
    "Канон принципов",
    "Чистый ответ владельцу (канон styleguide §(г), §А2.31–33)",
    "Исполнимость проверок и показ результата",
    "Источники и RAG",
})

#: Order in which the generated SOUL is assembled. Sections absent from the source are skipped;
#: the last four are generated from the defaults below and are where the questionnaire writes.
_SECTION_ORDER = (
    "Роль",
    "Границы",
    "Как вести работу",
    "Инварианты",
    "Системное мышление (ядро)",
    "Проверка результата",
    "Источники и проверка фактов",
    "Память и навыки",
    "Ответ пользователю",
    "Общие профили-специалисты",
)

BOUNDARY_CHOICES: dict[str, str] = {
    "no_external_uploads": "Не отправляй файлы и данные пользователя на внешние сервисы без прямого поручения.",
    "ask_before_outbound": "Перед действием, которое меняет что-то вовне (письма, публикации, оплаты), спроси разрешение.",
    "ask_before_delete": "Перед удалением чего-либо спроси.",
    "no_sensitive_memory": "Не сохраняй чувствительные данные в память.",
}

BOUNDARIES_DEFAULT = (
    "- Особых границ не задано: действуй по инвариантам ниже и спрашивай перед необратимыми действиями."
)

WORK_DEFAULT = (
    "- Большую задачу начинай с короткого плана, если не попросили иначе.",
    "- Если не получается — скажи сразу, что именно и что уже попробовал.",
    "- В отчёте: что сделано, что проверено, что осталось.",
    "- О замеченной проблеме говори сам, не дожидаясь вопроса.",
)

#: Prefixes of lines the questionnaire owns — stripped before re-applying so a second run of the
#: same answers cannot accumulate duplicates (idempotent apply).
_OWNED_LINE_PREFIXES = (
    "- Длина ответа:",
    "- Формат ответа:",
    "- При неопределённости:",
    "- Обращайся к пользователю:",
    "- Не отправляй файлы и данные пользователя",
    "- Перед действием, которое меняет что-то вовне",
    "- Перед удалением чего-либо спроси",
    "- Не сохраняй чувствительные данные в память",
    "- Особых границ не задано",
    "- Большую задачу начинай с короткого плана",
    "- Начинай работу сразу",
    "- Если не получается — скажи сразу",
    "- Пробуй другие способы",
    "- В отчёте: что сделано",
    "- О замеченной проблеме говори сам",
    "- Предлагать улучшения:",
    "- Как обращаться в ответе:",
    "Обращение к пользователю:",
)

SPECIALISTS_SECTION = (
    "## Общие профили-специалисты",
    "",
    "Кроме тебя на этой машине есть общие профили-специалисты: поиск внешних источников, документы, визуалы, расчёты, проверка результата.",
    "Это общий ресурс, а не чужие вещи: к ним можно обращаться с задачами по их теме и пользоваться их результатами; менять их настройки нельзя.",
    "",
)


def normalize_login(value: Any) -> str:
    """A login usable as ``01-<login>``: ``[A-Za-z0-9._-]``, no leading dot, max 64 chars."""
    slug = str(value or "").strip()
    if not slug or slug.startswith("."):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", slug):
        return ""
    return slug


def root_login(profile_name: Any) -> str:
    """The login ``01-<login>`` claims, else ``""``. Same rule as ``hermes_acl_profiles``."""
    text = str(profile_name or "").strip()
    if not text.startswith(PERSONAL_ROOT_PREFIX):
        return ""
    return normalize_login(text[len(PERSONAL_ROOT_PREFIX):])


def _split_sections(text: str) -> dict[str, list[str]]:
    """``title -> body lines`` for every ``## `` section (body excludes the heading line)."""
    out: dict[str, list[str]] = {}
    title: Optional[str] = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                out[title] = body
            title, body = line[3:].strip(), []
        elif line.startswith("# "):
            continue
        elif title is not None:
            body.append(line)
    if title is not None:
        out[title] = body
    return out


def _orchestrator_soul_path() -> Optional[Path]:
    """The orchestrator's SOUL in this install, if it has one.

    Deliberately not configurable by environment variable: the source is a machine fact (where the
    orchestrator profile lives), and a knob nobody sets would only add a way to seed a user's SOUL
    from an arbitrary file.
    """
    try:
        from hermes_constants import get_default_hermes_root
    except ImportError:  # bare script checkout: only the bundled base is reachable
        return None
    candidate = get_default_hermes_root() / "profiles" / "01-orchestrator" / "SOUL.md"
    return candidate if candidate.is_file() else None


def source_text(source_path: Optional[Path] = None) -> str:
    """The text the clone is derived from: *source_path*, else the orchestrator's SOUL, else bundled.

    A missing or unreadable explicit source falls through to the next rung rather than raising:
    seeding a profile is not worth failing over a moved file.
    """
    for path in (source_path, _orchestrator_soul_path()):
        if path is None:
            continue
        try:
            return Path(path).read_text(encoding="utf-8-sig")
        except OSError:
            continue
    return _BUNDLED_BASE.read_text(encoding="utf-8-sig")


def _clean_body(body: Iterable[str]) -> str:
    """Drop orchestrator-specific residue from a kept section.

    The three replacements are the ones the ops script made: profile numbers become a plain word,
    ``01-orchestrator`` becomes «ассистент», and absolute profile paths collapse to ``~/``.
    """
    text = "\n".join(body)
    text = re.sub(r"\b(0[2-9]|1[0-5])-[a-z][a-z0-9-]+", "специалист", text)
    text = text.replace("`01-orchestrator`", "ассистент")
    text = re.sub(r"/home/hermes/\.hermes/profiles/[0-9a-z_-]+/", "~/", text)
    text = text.replace(" — `needs_source`", "")
    text = text.replace(
        "При любом триггере — загрузи канон: `~/durable/rgz/system-speed-2x/design/"
        "soul-tier2-apply-package/references/systems-thinking-core.md` (пять сдвигов, "
        "пять фильтров, правило значимого решения, правило меры, справочник "
        "анти-паттернов); глубже — навык `systems-thinking-cognitive-base`.",
        "При любом триггере разбирай глубже: где петли, где задержки, что здесь рычаг "
        "и что не даст эффекта.",
    )
    text = text.replace(" (токен-налог)", "")
    # Duplicated verbatim in the dedicated «Проверка результата» section below.
    text = text.replace(
        "- После изменения проверяй заявленный конечный эффект. Числа, размеры, хеши, "
        "количества и статусы получай инструментом.\n", "")
    # Two closing rules of the same section: one paragraph keeps the SOUL inside its budget.
    text = text.replace(
        "\n\nПравило меры: рутина и линейные задачи — по существу, без системной карты.",
        " Правило меры: рутина и линейные задачи — по существу, без системной карты.")
    return text.strip()


#: The orchestrator's memory rules (its provider, its retired MEMORY.md, «only routing skills»)
#: are service policy; a person's assistant gets the general rule instead.
MEMORY_SECTION = (
    "Запоминай устойчивое: кто ты, как предпочитаешь работать, термины и факты среды. "
    "Не запоминай содержимое файлов, прогресс задач и временные результаты — это живёт в "
    "рабочей папке, а не в памяти.\n\n"
    "Если при работе над задачей выяснилось, как её делать повторяемо (порядок шагов, "
    "подводные камни, предпочтения по этому классу работы) — запиши это в навык, а не в "
    "память: навык загружается только тогда, когда нужен."
)

#: The orchestrator's acceptance-and-gates checklist is a delivery protocol; the user's version
#: keeps only what applies to any work — check the result and name what stayed unchecked.
RESULT_CHECK_SECTION = (
    "- После изменения проверяй заявленный конечный эффект: числа, размеры, хеши, количества "
    "и статусы получай инструментом, а не по памяти.\n"
    "- Если проверка невозможна — скажи прямо, что именно осталось непроверенным и почему. "
    "Отсутствие проверки не выдавай за успех.\n"
    "- Разделяй, что проверено инструментом, что выведено логически и что осталось "
    "предположением."
)

REPLY_SECTION = (
    "Отвечай по-русски, если формат прямо не требует другого языка. Не показывай внутренние "
    "рассуждения, журнал инструментов и промежуточные обещания. Дай сам запрошенный результат "
    "либо краткое резюме, проверку, остаточные ограничения и точный путь к артефакту.\n\n"
    "Не добавляй лишние приветствия, благодарности и предложения продолжить работу. Строку "
    "`✅ Работа завершена` используй только после фактического выполнения и не добавляй в "
    "строгие JSON/машинные форматы."
)


def _kept_sections(text: str) -> dict[str, str]:
    """``title -> cleaned body`` for the sections that carry over from *text*."""
    out: dict[str, str] = {}
    for title, body in _split_sections(text).items():
        if title == "Роль" or title in STRIP_SECTIONS or title == "Источники и RAG":
            continue
        cleaned = _clean_body(body)
        if not cleaned:
            continue
        if "не ставь такой тест блокирующим" in cleaned:
            cleaned = "\n".join(
                ln for ln in cleaned.splitlines() if "не ставь такой тест блокирующим" not in ln
            ).strip()
        out[title] = cleaned
    out["Память и навыки"] = MEMORY_SECTION
    out["Проверка результата"] = RESULT_CHECK_SECTION
    out["Ответ пользователю"] = REPLY_SECTION
    # The orchestrator's RAG section is competition terminology; the user's version is narrower.
    out.setdefault(
        "Источники и проверка фактов",
        "Не выдавай найденное за подтверждённое. Кандидат из поиска или памяти — не факт: проверь "
        "первоисточник и назови локатор. Если прямого основания нет — так и скажи, не достраивая "
        "ответ по догадке.",
    )
    return out


def build_user_soul(login: str, source: Optional[str] = None) -> str:
    """The default SOUL for ``01-<login>``: the clone, minus the orchestrator's service role."""
    slug = normalize_login(login) or str(login or "").strip()
    sections = _kept_sections(source_text() if source is None else source)

    role = (
        f"Ты — персональный ассистент {slug}. Помогаешь с задачами: разбираешься в фактах, "
        "ведёшь работу до результата, говоришь прямо о том, что проверено, а что нет. "
        "Не изображаешь работу: если что-то не удалось — называешь причину и ограничение."
    )
    sections["Роль"] = role
    sections.setdefault("Границы", BOUNDARIES_DEFAULT)
    sections.setdefault("Как вести работу", "\n".join(WORK_DEFAULT))

    parts = [f"# SOUL — {slug}", "", "Пользователь Hermes. Ниже — поведение по умолчанию; его можно уточнить опросником или переписать вручную.", ""]
    for title in _SECTION_ORDER:
        if title == "Общие профили-специалисты":
            parts.extend(SPECIALISTS_SECTION)
            continue
        body = sections.get(title)
        if not body:
            continue
        parts.extend([f"## {title}", "", body, ""])
    return "\n".join(parts).rstrip() + "\n"


def personal_soul_for(profile_name: Any) -> Optional[str]:
    """Generated SOUL for an ``01-<login>`` name; ``None`` for anything else.

    ``None`` is the signal create paths use to leave ``DEFAULT_SOUL_MD`` alone: a profile outside
    the personal-root namespace must keep exactly the behaviour it had before this module.
    """
    login = root_login(profile_name)
    if not login:
        return None
    return build_user_soul(login)


def questionnaire_text() -> str:
    """The personalization questionnaire written beside a new personal root's SOUL."""
    return _BUNDLED_QUESTIONNAIRE.read_text(encoding="utf-8")


def seed_questionnaire(profile_dir: Path, *, only_personal: bool = False) -> Optional[Path]:
    """Write the questionnaire into *profile_dir* unless the person already edited one.

    ``only_personal`` restricts the write to a ``01-<login>`` directory (the create path passes
    it, because it holds no name of its own): a profile outside the personal namespace must not
    grow a file it has never had.
    """
    directory = Path(profile_dir)
    if only_personal and not root_login(directory.name):
        return None
    target = directory / QUESTIONNAIRE_FILENAME
    if not target.exists():
        target.write_text(questionnaire_text(), encoding="utf-8")
    return target


def seed_personal_soul(profile_dir: Path, profile_name: Any) -> Optional[Path]:
    """Seed SOUL.md and the questionnaire for a personal root; ``None`` when not one."""
    soul = personal_soul_for(profile_name)
    if soul is None:
        return None
    soul_path = Path(profile_dir) / "SOUL.md"
    soul_path.write_text(soul, encoding="utf-8")
    seed_questionnaire(profile_dir)
    return soul_path


# ────────────────────────────────────────────────────────────────────────────
# Applying the questionnaire
# ────────────────────────────────────────────────────────────────────────────

_ADDRESS_FORMS = {"ты": "на «ты»", "вы": "на «вы»"}


def _strip_owned(body: list[str]) -> list[str]:
    out: list[str] = []
    for line in body:
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _OWNED_LINE_PREFIXES):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def _answer(answers: Mapping[str, Any], key: str) -> str:
    value = answers.get(key)
    return value.strip() if isinstance(value, str) else ""


def _replace_sections(text: str, replacements: Mapping[str, str]) -> str:
    """Swap the body of named sections; every other byte of *text* stays as it was."""
    if not replacements:
        return text
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    emitted: set[str] = set()
    while index < len(lines):
        line = lines[index]
        if line.startswith("## ") and line[3:].strip() in replacements:
            title = line[3:].strip()
            out.extend([line, "", replacements[title], ""])
            emitted.add(title)
            index += 1
            while index < len(lines) and not lines[index].startswith("## "):
                index += 1
            continue
        out.append(line)
        index += 1
    # A section the SOUL did not have yet is appended rather than silently dropped.
    for title, body in replacements.items():
        if title in emitted:
            continue
        out.extend([f"## {title}", "", body, ""])
    return "\n".join(out).rstrip() + "\n"


def questionnaire_memory_entries(answers: Mapping[str, Any]) -> list[str]:
    """Facts about the person that belong in the profile's memory, not in its SOUL.

    Blocks 5–6 of the questionnaire (domain precision, terms, trusted and untrusted sources, and
    the free-form block) plus the work area and usual requests: stable facts, useless as
    instructions, so they ride in memory where the memory tool's budget applies.
    """
    pairs = (
        ("area", "Область работы"),
        ("frequent_requests", "Часто просит"),
        ("precise_domain", "Тема, где нужна особая точность"),
        ("terms", "Свои термины"),
        ("trusted_sources", "Доверенные источники"),
        ("untrusted_sources", "Источники, не считающиеся доказательством"),
        ("extra", "О себе"),
    )
    out: list[str] = []
    for key, label in pairs:
        value = _answer(answers, key)
        if value:
            out.append(f"{label}: {value}")
    return out


#: ``answer key -> (position in «Как вести работу», {answer prefix: replacement bullet})``.
#: Table-driven so one more choice never adds a branch ladder (CC stays flat per question).
_WORK_CHOICES: dict[str, tuple[int, dict[str, str]]] = {
    "planning": (0, {
        "сразу": "- Начинай работу сразу, без предварительного плана; план — только если попросят.",
        "план": "- Большую задачу начинай с короткого плана и покажи его до работы.",
    }),
    "failure_mode": (1, {
        "сказать": "- Если не получается — скажи сразу, что именно и что уже попробовал.",
        "пробовать": "- Пробуй другие способы, а если не вышло — скажи прямо, где остановился.",
        "бросить": "- Если не получается — остановись и скажи об этом, не доводя до срыва срока.",
    }),
    "reporting": (2, {
        "только итог": "- В отчёте: только итог, без пересказа процесса.",
        "подробный": "- Отчёт веди подробно: шаги, проверки, что осталось открытым.",
    }),
    "suggest_improvements": (3, {
        "да": "- О замеченной проблеме говори сам и предлагай, что улучшить.",
        "нет": "- Улучшения не предлагай, если не спросили.",
        "только": "- Предлагать улучшения: только если спросят.",
    }),
}


def _section_role(answers: Mapping[str, Any], body: list[str]) -> Optional[str]:
    """«Роль»: the copied paragraph plus one line naming how the person is addressed."""
    display_name = _answer(answers, "display_name")
    address_form = _answer(answers, "address_form").lower()
    if not display_name and address_form not in _ADDRESS_FORMS:
        return None
    parts = [p for p in (display_name, _ADDRESS_FORMS.get(address_form, "")) if p]
    base = "\n".join(_strip_owned(body)).strip()
    return (base + "\n\n" + "Обращение к пользователю: " + ", ".join(parts) + ".").strip()


def _section_boundaries(answers: Mapping[str, Any]) -> Optional[str]:
    """«Границы»: exactly the ticks and the custom rule the person gave."""
    ticks = (str(k).strip() for k in (answers.get("boundaries") or []))
    bullets = [f"- {BOUNDARY_CHOICES[k]}" for k in ticks if k in BOUNDARY_CHOICES]
    custom = _answer(answers, "boundary_custom")
    if custom:
        bullets.append(f"- {custom}")
    return "\n".join(bullets) if bullets else None


#: «Как вести работу» is a four-slot template: the default bullets, each replaceable by one
#: printed choice or by a free-typed answer. These are the exact lines the questionnaire owns —
#: stripped before re-applying, so a second run rewrites slots instead of appending duplicates.
_WORK_OWNED_LINES = frozenset(
    list(WORK_DEFAULT)
    + [text for _index, choices in _WORK_CHOICES.values() for text in choices.values()]
)

#: Prefix of a free-typed slot line («- Перед работой: …»).
_WORK_OWNED_PREFIXES = tuple(f"- {label}:" for label in (
    "Перед работой", "Если не получается", "В отчёте", "Про улучшения",
))


def _strip_work(body: list[str]) -> list[str]:
    """The slots a person did not answer, with the questionnaire-owned lines removed."""
    out = [
        line for line in body
        if line.strip() and line.strip() not in _WORK_OWNED_LINES
        and not line.strip().startswith(_WORK_OWNED_PREFIXES)
    ]
    while out and not out[-1].strip():
        out.pop()
    return out


def _section_work(answers: Mapping[str, Any], body: list[str]) -> Optional[str]:
    """«Как вести работу»: four slots — the default bullets, each replaced by its answer.

    A free-typed answer no printed choice matches is still written, verbatim, onto the slot it
    belongs to: dropping it would make the questionnaire promise a knob it does not have. The
    slots are rebuilt from the table every time (not from the previous text), so applying the same
    answers twice rewrites them instead of stacking variants of the same slot.
    """
    labels = ("Перед работой", "Если не получается", "В отчёте", "Про улучшения")
    lines: list[str] = list(WORK_DEFAULT)
    touched = False
    for slot, label in enumerate(labels):
        key = next((k for k, (index, _c) in _WORK_CHOICES.items() if index == slot), "")
        answer = _answer(answers, key) if key else ""
        if not answer:
            continue
        lowered = answer.lower()
        match = next((t for prefix, t in _WORK_CHOICES[key][1].items() if lowered.startswith(prefix)), "")
        if match:
            touched = touched or match != lines[slot]
            lines[slot] = match
        else:
            lines[slot] = f"- {label}: {answer}"
            touched = True
    extras = [line for line in _strip_work(body) if line.strip() not in _WORK_OWNED_LINES
              and not line.strip().startswith(_WORK_OWNED_PREFIXES)]
    return "\n".join(lines + extras) if touched else None


#: «Ответ пользователю»: ``answer key -> (bullet label, {printed choice: rendered line})``.
#: A printed choice gets its own wording (a bare echo of "как угодно" instructs nothing); any
#: other non-empty answer is kept verbatim, so a free-typed preference is never silently dropped
#: — that silent drop is exactly how ``address_style`` shipped as a question that did nothing.
_REPLY_LINES: dict[str, tuple[str, dict[str, str]]] = {
    "answer_length": ("- Длина ответа", {}),
    "answer_format": ("- Формат ответа", {}),
    "uncertainty": ("- При неопределённости", {}),
    "display_name": ("- Обращайся к пользователю", {}),
    "address_style": ("- Как обращаться в ответе", {
        "по имени": "называй пользователя по имени в начале ответа",
        "без обращения": "без обращения — сразу к делу, без имени и приветствий",
        "как угодно": "обращение не важно — выбирай по обстановке",
    }),
}


def _reply_bullet(key: str, value: str) -> str:
    """One rendered bullet: the printed choice's own wording, else the answer as given."""
    label, choices = _REPLY_LINES[key]
    answer = value.lower()
    rendered = next((text for prefix, text in choices.items() if answer.startswith(prefix)), "")
    return f"{label}: {rendered}" if rendered else f"{label}: {value}"


def _section_reply(answers: Mapping[str, Any], body: list[str]) -> Optional[str]:
    """«Ответ пользователю»: the copied paragraphs plus one bullet per answered preference."""
    bullets = [
        _reply_bullet(key, value)
        for key in _REPLY_LINES
        if (value := _answer(answers, key))
    ]
    if not bullets:
        return None
    base = "\n".join(_strip_owned(body)).strip()
    return (base + "\n\n" + "\n".join(f"{b}." for b in bullets)).strip()


def apply_questionnaire(text: str, answers: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Rewrite only the sections *answers* speaks about; return ``(soul, report)``.

    An empty answer leaves its line — and therefore its section — untouched, which is the
    questionnaire's stated rule («пустой ответ = оставить по умолчанию»). Sections that receive
    no answer at all are not in ``report["changed_sections"]`` and stay byte-identical.
    """
    sections = _split_sections(text)
    builders = (
        ("Роль", lambda: _section_role(answers, sections.get("Роль", []))),
        ("Границы", lambda: _section_boundaries(answers)),
        ("Как вести работу", lambda: _section_work(answers, sections.get("Как вести работу", []))),
        ("Ответ пользователю", lambda: _section_reply(answers, sections.get("Ответ пользователю", []))),
    )
    replacements: dict[str, str] = {}
    for title, build in builders:
        body = build()
        if body:
            replacements[title] = body

    new_text = _replace_sections(text, replacements)
    report: dict[str, Any] = {"changed_sections": [], "unchanged": []}
    for title in _SECTION_ORDER:
        if title == "Общие профили-специалисты":
            continue
        (report["changed_sections"] if title in replacements else report["unchanged"]).append(title)
    report["memory_entries"] = questionnaire_memory_entries(answers)
    return new_text, report


def main(argv: Optional[list[str]] = None) -> int:
    """CLI wrapper: ``python -m hermes_acl_soul --login alice [--profile 01-alice|--out FILE]``."""
    ap = argparse.ArgumentParser(description="SOUL клон для пользовательского профиля 01-<login>")
    ap.add_argument("--login", required=True, help="логин пользователя (без @ и домена)")
    ap.add_argument("--profile", help="имя профиля; если задано, пишет прямо в него")
    ap.add_argument("--out", help="путь к файлу результата")
    ap.add_argument("--questionnaire", action="store_true", help="напечатать опросник вместо SOUL")
    args = ap.parse_args(argv)

    if args.questionnaire:
        sys.stdout.write(questionnaire_text())
        return 0

    text = build_user_soul(args.login)
    if args.profile:
        from hermes_constants import get_default_hermes_root

        profile_dir = get_default_hermes_root() / "profiles" / args.profile
        if not profile_dir.is_dir():
            print(f"профиль не существует: {profile_dir}", file=sys.stderr)
            return 2
        target = profile_dir / "SOUL.md"
        target.write_text(text, encoding="utf-8")
        seed_questionnaire(profile_dir)
    else:
        target = Path(args.out) if args.out else Path(f"/tmp/SOUL-{args.login}.md")
        target.write_text(text, encoding="utf-8")
    print(target)
    return 0


#: Fields the questionnaire file declares but whose answers are facts for the profile's memory,
#: not instructions for its SOUL (blocks 5–6 and where the person works).
MEMORY_ONLY_KEYS = frozenset({
    "area", "frequent_requests", "precise_domain", "terms",
    "trusted_sources", "untrusted_sources", "extra",
})

#: Printed choices whose rendering lands on the default wording, so the SOUL stays byte-identical.
#: Explicit so the pairing test can tell a deliberate no-op from a question that does nothing:
#: ``failure_mode = «сказать сразу»`` *is* the default «Если не получается — скажи сразу…».
#: Everything else printed must change the SOUL.
SAME_AS_DEFAULT = frozenset({("failure_mode", "сказать сразу")})

#: Backticked placeholder a free-text question leaves for the person to fill.
_BLANK = "________________"


def questionnaire_answer_keys(text: Optional[str] = None) -> list[str]:
    """Every request field the questionnaire prints, read from the file itself.

    A test that pinned the implementation's own key table would stay green while the printed
    questionnaire promised a knob that does nothing (``address_style`` shipped exactly that way).
    So the interface is parsed here, unfiltered: a question line carries its field in brackets
    after the printed choices, a tick box ends with its field. A key the implementation does not
    know therefore lands in the result and the pairing test fails — which is the point.
    """
    source = questionnaire_text() if text is None else text
    keys: list[str] = []
    for line in source.splitlines():
        if line.lstrip().startswith("- [ ]"):
            spans = re.findall(r"`([^`]+)`", line)
            candidates = [spans[-1]] if spans and re.fullmatch(r"[a-z][a-z0-9_]+", spans[-1]) else []
        else:
            candidates = re.findall(r"\(`([a-z][a-z0-9_]+)`", line)
        for key in candidates:
            if key not in keys:
                keys.append(key)
    return keys


def questionnaire_printed_choices(text: Optional[str] = None) -> dict[str, list[str]]:
    """``key -> printed choices`` as the questionnaire file shows them (empty for free text)."""
    source = questionnaire_text() if text is None else text
    out: dict[str, list[str]] = {}
    for line in source.splitlines():
        bracketed = re.findall(r"\(`([a-z][a-z0-9_]+)`", line)
        if not bracketed:
            continue
        head = line.split(f"`{bracketed[0]}`", 1)[0]
        spans = re.findall(r"`([^`]+)`", head)
        choices = [c.strip() for span in spans for c in span.split("/") if c.strip()]
        out.setdefault(bracketed[0], [c for c in choices if c != _BLANK])
    return out


if __name__ == "__main__":
    raise SystemExit(main())
