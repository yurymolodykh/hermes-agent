"""
Telegram delivery-policy primitives (D0 ``tg-delivery-hygiene``, задача F1).

После удаления контент-классификатора (R0(в), REFACTOR-DESIGN.md §(в)):
КЛАССИФИКАЦИИ в этом модуле больше нет — единственная точка решения
«доставлять или молчать» теперь ``gateway/delivery_engine.classify_event()``
(4 типа: live-status / final / needs-decision / aborted; всё прочее —
молчание + журнал).  Здесь остаётся только то, что движок и cron
потребляют как ПРИМИТИВЫ:

  * ``sanitize_delivery_text`` / ``_strip_mission_slang`` — санитизация
    исходящего текста (reasoning/interim-шум, служебный слой);
  * ``log_suppression`` — ЕДИНСТВЕННАЯ точка записи журнала подавлений
    (вызывается из gateway/delivery_engine.py и из временных fail-closed
    путей cron);
  * ``owui_telegram_users`` + tg-привязки (F3) — серверный реестр
    «OWUI-пользователь ⇄ TG-чат» (dual-lane);
  * ``verify_recipient`` / ``verify_subscription_recipient`` — ИНВ1
    (получатель == инициатор, fail-closed);
  * ``TargetRef`` / ``InitiatorRef`` / ``PolicyDecision`` — типы-примитивы.

Инварианты (NIGHT-PLAN.md §1–§5, спека `research/delivery-policy.md`):
  ИНВ1 — recipient == initiator: в TG-чат человека попадает только то,
         что породил именно он (его задача / подписка / сессия).
  ИНВ2 — доставляются только ценные классы: файлы/артефакты, финальные
         отчёты, требующие действий человека тревоги.  НЕ доставляются:
         промежуточные закрытия, служебные статусы, дешёвые рутинные
         отчёты без дельты.
  ИНВ3 — не ломать существующее: api_server/OWUI exact-origin, ``[SILENT]``,
         alert-once (blocked_config/drift) остаются как есть.

Крон-алерты (F4, alert-once контракт владельца 2026-10-18) больше НЕ
детектируются по тексту: scheduler классифицирует сбой структурно и
передаёт уже готовый класс в ``engine.handle_cron_alert`` ([aborted] /
[needs-decision]), персистентные alert-once биты остаются в cron.jobs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Set

from tools.ansi_strip import sanitize_display_text

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Санитизация текста доставки (reasoning / interim-шум)
# --------------------------------------------------------------------------
# ЕДИНАЯ точка очистки текста, который отправляется в OWUI (api_server) и
# Telegram.  Гарантирует, что ``reasoning_content`` и длинные interim-тексты
# «сейчас сделаю X…Y» не попадают в каналы доставки, даже если в payload
# (summary/result/handoff) они присутствуют.  Чистые эвристики, без LLM.

#: Reasoning-блоки, которые модели встраивают inline в основной текст.
#: Синхронизировано с agent/think_scrubber.py::_OPEN_TAG_NAMES и
#: cli.py _OPEN_TAGS/_CLOSE_TAGS.
_THINK_OPEN_RE = re.compile(
    r"(?:"
    r"<REASONING_SCRATCHPAD>|<thinking>|<THINKING>|<reasoning>|<reasoning>|<thought>|<Thought>"
    r"|─{3,}\s*thinking\s*─{3,}"
    r"|```(?:reasoning|thinking)"
    r")",
    re.IGNORECASE,
)
_THINK_CLOSE_RE = re.compile(
    r"(?:"
    r"</REASONING_SCRATCHPAD>|</thinking>|</THINKING>|</reasoning>|</thought>"
    r"|─{3,}\s*response\s*─{3,}"
    r"|```"
    r")",
    re.IGNORECASE,
)

#: Reasoning-поля, которые может тащить JSON/YAML-подобный payload
#: (``reasoning_content: …``, ``"reasoning": …`` и т.п.).
_REASONING_FIELD_RE = re.compile(
    r"(?m)^\s*(?:reasoning_content|reasoning|reasoning_details|"
    r"codex_reasoning_items|codex_message_items)"
    r"\s*[:=]\s*[\"']?[^\n]{0,200}\n?",
    re.IGNORECASE,
)

#: Ключевые слова interim-планирования «сейчас сделаю…» / «начинаю…».
#: Если текст БОЛЬШЕ лимита и начинается с такого штампа, он считается
#: незавершённым рассуждением вслух, а не финальным резюме, и обрезается.
_INTERIM_STAMPS: tuple[str, ...] = (
    "сейчас сделаю", "сейчас пробую", "сейчас проверю", "сейчас посмотрю",
    "сейчас попробую", "щас сделаю", "начинаю с", "приступаю к",
    "сейчас выясню", "сначала я", "сейчас гляну", "погнали",
    "сперва посмотрю", "давай начнём", "давай начнем",
)
_INTERIM_KEEP_FIRST_SENTENCE: int = 220

#: Нижняя граница длины «длинного» текста (для interim-подавления).
_INTERIM_MIN_LENGTH: int = 120

#: Символы-разделители строк U+2028 LINE SEPARATOR / U+2029 PARAGRAPH
#: SEPARATOR. Они вне диапазона C0 (0x00–0x1F) и вне базовых ``\n``/``\t``,
#: поэтому не покрываются ``tools.ansi_strip.sanitize_display_text``, но в
#: Telegram/OWUI ломают разметку сообщения (вставляют разрывы там, где их
#: не ждали). Кандидатный вектор из находки H-2 (K1 red team).
_U2028_U2029_RE = re.compile(r"[\u2028\u2029]")

#: Маркеры «плана на будущее»: будущее время глагола + последовательность
#: действий.  Только при наличии ОБОИХ текст считается незавершённым
#: рассуждением вслух (plan), а не отчётом о сделанном.
_FUTURE_TENSE_RE = re.compile(
    r"\b(?:сделаю|проверю|обновлю|настрою|перезапущу|посмотрю|выясню|"
    r"запущу|сверю|сравню|соберу|напишу|подготовлю|выполню|пройдусь|"
    r"гляну|грону|проведу|разберу|попробую|проверять|обновлять|настроить)\b",
)
_SEQUENCE_RE = re.compile(r"\b(?:потом|затем|далее|после|сперва|сначала)\b")


#: Строки-фрагменты служебного слоя (канон R5, стиль «деловой простой»):
#: удаляются из text-доставки, чтобы владельцу не утекали внутренние
#: коды/сленг/локальные пути. МЯГКИЙ денylist — только заведомо служебное.
_MISSION_HEADER_SEG_RE = re.compile(
    r"(?:\*\*)?(?:"
    r"Миссия|Контроллер|Контур|Модель[/ ]провайдер|Модель[/]провайдер|Код[ \t]*задачи|assignee"
    r"|Метаданные|Провенанс|Сентинел|Пиннинг"
    r")(?:\*\*)?[ \t]*[:：][^\n§]*(?:\n|$)",
    re.IGNORECASE,
)
_MISSION_TOKEN_RE = re.compile(
    r"(?:"
    r"\bt_[A-Za-z0-9_]+\b"
    r"|\bМодель[/ ]провайдер\b|\bassignee\b|\bsентинел\b|\bканбан\b|\bРГЗ\b"
    r"|\bГермес\b"
    r"|\b[0-9a-f]{12,}\b"
    r"|\b\d{2}-[a-zA-Z][\w-]*\b(?![0-9]+-bit)"
    r"|C:[/\\]Users[/\\][\w./\\\-]+"
    r"|\[[ \t]*служ[ \t]*\.[ \t]*\]"
    r")",
    re.IGNORECASE,
)


def _strip_mission_slang(text: str) -> str:
    """Удалить явно служебные токены из текста доставки (канон R5/R6).

    Применяется как финальный гигиенический шаг перед отправкой в каналы.
    Мягкий денylist: убирает заведомо служебные метки (коды задач, имена
    профилей, локальные пути, внутренний сленг), сохраняя предметный
    деловой текст. НЕ удаляет словарь делового языка (названия моделей как
    предмет, цифры, факты), чтобы не искажать содержательные сообщения.
    """
    out = _MISSION_HEADER_SEG_RE.sub("", text)
    out = _MISSION_TOKEN_RE.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r",[ \t]*,", ",", out)
    out = re.sub(r"[ \t]*,", ",", out)
    out = re.sub(r",[ \t]*$", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def sanitize_delivery_text(text: Any, *, max_chars: Optional[int] = None) -> str:
    """Убрать reasoning-блоки, обрезать long interim-тексты и вычистить
    управляющие символы из текста доставки.

    Применяется СРАЗУ ПЕРЕД отправкой в OWUI (api_server) и Telegram:
     1. Коэрсия в строку (не-строки → ``""``).
     2. Удаление reasoning/thinking-блоков (``…``, ``<reasoning>…``,
        fenced ```reasoning…``` и т.п.) — случаи 1–3 из
        ``agent_runtime_helpers.strip_think_blocks``.
     3. Удаление полей ``reasoning_content`` / ``reasoning``, если они
        попали в текст как JSON/YAML-подобные строки.
     4. Interim-подавление: если текст начинается со штампа «сейчас сделаю…»
        И длиннее ``_INTERIM_MIN_LENGTH`` — оставляем только первый абзац /
        первое предложение (короткий сигнал «работа идёт»), обрезанный до
        ``_INTERIM_KEEP_FIRST_SENTENCE``.  Это канал «что сделано…» без
        простыни рассуждений о том, что ещё планируется.
     5. Гигиена управляющих символов (находка H-2, K1 red team): C0 control
        (0x00–0x1F) и ANSI/ECMA-48 escape-последовательности удаляются через
        ``tools.ansi_strip.sanitize_display_text`` (тот же канонический seam,
        что и ``session_recap``/``console_engine``); ``\\r`` нормализуется в
        ``\\n``; легитимные ``\\n``/``\\t`` сохраняются.  Дополнительно
        удаляются разделители строк U+2028/U+2029, которые вне диапазона C0
        и не покрываются канонической функцией, но ломают разметку TG/OWUI.
     6. Съём служебного слоя (канон R6): коды задач/сленг/локальные пути.
     7. Опциональный общий лимит ``max_chars`` (мягкий, на символ).

    Функция ЧИСТАЯ (без side-эффектов) и не бросает исключений — очистка
    никогда не должна ломать доставку.  Нормальные деловые сообщения
    (кириллица/латиница/цифры/пробелы/``\\n``/``\\t``) проходят без изменений.
    """
    if not isinstance(text, str):
        if text is None:
            return ""
        try:
            text = str(text)
        except Exception:
            return ""
    if not text:
        return ""

    # 2. Reasoning/thinking-блоки.
    stripped = _strip_think_blocks(text)
    # 3. JSON/YAML-подобные reasoning-поля.
    stripped = _REASONING_FIELD_RE.sub("", stripped)
    # 4. Interim-подавление.
    stripped = _condense_interim(stripped)
    # 5. Гигиена управляющих символов: C0 + ANSI/ECMA-48 (канон),
    #    затем U+2028/U+2029 (вне диапазона канона).
    stripped = sanitize_display_text(stripped)
    stripped = _U2028_U2029_RE.sub("", stripped)
    # 6. Съём служебного слоя (канон R6): коды задач/сленг/локальные пути.
    stripped = _strip_mission_slang(stripped)
    # 7. Общий лимит.
    if max_chars is not None and max_chars > 0 and len(stripped) > max_chars:
        stripped = stripped[:max_chars]
    stripped = stripped.strip()
    return stripped


def _strip_think_blocks(text: str) -> str:
    """Remove inline reasoning/thinking blocks while preserving visible text.

    Handles the same four shapes as ``agent.agent_runtime_helpers.
    strip_think_blocks`` (closed pairs, unterminated open at boundary,
    stray orphan tags, tag variants) using the shared tag vocabulary.
    """
    if not text:
        return ""
    # Closed pairs first: from the first open tag to its close tag.
    while True:
        m_open = _THINK_OPEN_RE.search(text)
        if not m_open:
            break
        start = m_open.start()
        rest = text[m_open.end():]
        m_close = _THINK_CLOSE_RE.search(rest)
        if m_close:
            # Remove open…close inclusive, keep surrounding text.
            text = text[:start] + rest[m_close.end():]
        else:
            # Unterminated open block: drop everything from the open tag on
            # (matches the block-boundary handling in the agent scrubber).
            text = text[:start]
            break
    # Stray orphan close tags.
    text = _THINK_CLOSE_RE.sub("", text)
    # Snip blank runs left by removed blocks (but preserve paragraphs).
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _condense_interim(text: str) -> str:
    """Condense long 'сейчас сделаю X…Y' planning prose.

    When the text starts with an interim stamp AND is longer than the floor,
    AND reads as a forward-looking plan (future-tense markers + sequencing),
    it is an in-flight thought, not a final result — keep only a short
    'work in progress' signal (first sentence, bounded to
    ``_INTERIM_KEEP_FIRST_SENTENCE`` chars). A long text that already
    reports done work (past-tense markers) is left intact so real results
    are never lost by over-condensation.
    """
    if not text:
        return text
    low = text.lstrip().lower()
    if len(text) <= _INTERIM_MIN_LENGTH:
        return text
    if not any(low.startswith(stamp) for stamp in _INTERIM_STAMPS):
        return text
    future_matches = list(_FUTURE_TENSE_RE.finditer(low))
    has_sequence = bool(re.search(_SEQUENCE_RE, low))
    if not future_matches:
        # No forward-looking intent at all — a completed narrative stays intact.
        return text
    if not (has_sequence or len(future_matches) >= 2):
        # One short forward verb with no sequencing is not a long plan.
        return text
    # First paragraph.
    first = text.strip()
    para = first.split("\n", 1)[0]
    # First sentence within the paragraph.
    had_sentence_split = False
    for sep in ("。", ". ", "! ", "? ", " — ", "…", " — "):
        if sep in para:
            para = para.split(sep, 1)[0] + ("。" if sep == "。" else ". ")
            had_sentence_split = True
            break
    para = para.strip()
    if had_sentence_split:
        # Multi-sentence interim block: the first sentence IS the short
        # "работа идёт" signal.
        if len(para) > _INTERIM_KEEP_FIRST_SENTENCE:
            para = para[:_INTERIM_KEEP_FIRST_SENTENCE].rstrip() + "…"
        return para
    # Single (unbroken) sentence plan — e.g. "сейчас сделаю X, потом Y,
    # затем Z…".  No sentence separator exists, so the whole paragraph is one
    # long forward-looking plan: cut at the first sequence marker so only the
    # first stage survives (a short "работа идёт" signal).  When no sequence
    # marker exists (pure comma chain), cut at the SECOND future-tense verb so
    # a long plan never rides through verbatim.
    m = _SEQUENCE_RE.search(para)
    if m and m.start() > 0:
        para = para[: m.start()].rstrip(" ,;:") + "…"
    else:
        verbs = list(_FUTURE_TENSE_RE.finditer(para))
        if len(verbs) >= 2:
            para = para[: verbs[1].start()].rstrip(" ,;:") + "…"
    if len(para) > _INTERIM_KEEP_FIRST_SENTENCE:
        para = para[:_INTERIM_KEEP_FIRST_SENTENCE].rstrip() + "…"
    return para


# --------------------------------------------------------------------------
# Серверная привязка OWUI-пользователь ⇄ TG-чат (T2, спека §8.1)
# --------------------------------------------------------------------------

#: Конфиг-ключ и env-переменная с привязкой ``{owui_user_id: tg_chat_id}``.
_BINDING_CFG_KEY = "openwebui_telegram_users"
_BINDING_ENV = "HERMES_OPENWEBUI_TELEGRAM_USERS"

#: Синк-файл привязок (F3, ``tools/owui_tg_binding_sync.py``): JSON-источник
#: соответствий «OWUI→TG», записываемый синк-джобой из webui.db +
#: channel_directory.json (неоднозначность не матчится, fail-closed).
#: Приоритет НИЖЕ явных источников: записи env/cfg/load_config не
#: перекрываются файлом; файл дополняет только отсутствующие user_id.
_BINDING_FILE_CFG_KEY = "openwebui_telegram_bindings_file"
_BINDING_FILE_ENV = "HERMES_OPENWEBUI_TELEGRAM_BINDINGS_FILE"
_BINDING_FILE_NAME = "tg_bindings.json"


def _binding_file_path(cfg: Optional[dict] = None) -> Optional[str]:
    """Путь к синк-файлу привязок: env → cfg → <hermes_home>/tg_bindings.json."""
    env_path = os.environ.get(_BINDING_FILE_ENV, "").strip()
    if env_path:
        return env_path
    if isinstance(cfg, dict):
        val = cfg.get(_BINDING_FILE_CFG_KEY)
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home() / _BINDING_FILE_NAME)
    except Exception:
        return None


def _read_binding_file(path: Optional[str]) -> Dict[str, str]:
    """Прочитать синк-файл привязок; при любой проблеме — ``{}`` (fail-closed).

    Ожидаемая схема: ``{"bindings": {owui_user_id: tg_chat_id}, ...}``.
    Некорректный/нечитаемый/не-JSON файл не поднимает исключений и не
    открывает лейн: возвращается пустой mapping (OWUI-only).
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.warning(
            "delivery-policy: binding file %s unreadable (%s) — OWUI-only, "
            "fail-closed", path, exc,
        )
        return {}
    bindings = data.get("bindings") if isinstance(data, dict) else None
    if not isinstance(bindings, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in bindings.items()
        if str(k).strip() and str(v).strip()
    }


def owui_telegram_users(cfg: Optional[dict] = None) -> Dict[str, str]:
    """Серверная привязка «TG-чат принадлежит данному OWUI-пользователю» (T2 §8.1).

    ЕДИНСТВЕННЫЙ источник прав перетекания OWUI→TG.  Отсутствие ключа /
    отсутствие записи пользователя = OWUI-only (fail-closed, V8 до T2).
    Ни client-заголовки, ни home-фолбэки прав НЕ дают.

    Приоритет источников (первый решающий побеждает):
      1. env ``HERMES_OPENWEBUI_TELEGRAM_USERS`` — JSON-объект
         ``{owui_user_id: tg_chat_id}`` (устойчив к перезаписи config.yaml).
      2. Per-call ``cfg`` dict (``openwebui_telegram_users``).
      3. ``load_config()`` (тот же ключ).
      4. F3 (t_0aedc91c): синк-файл ``tg_bindings.json`` (env/cfg-переопределение
         пути ``openwebui_telegram_bindings_file`` / ``HERMES_OPENWEBUI_TELEGRAM_BINDINGS_FILE``;
         по умолчанию ``<hermes_home>/tg_bindings.json``) — дополняет только
         отсутствующие user_id.  Записи явных источников (1–3) файлом НЕ
         перекрываются; испорченный файл = ``{}`` по этому источнику.
    Источники читаются только серверной стороной (scheduler), никогда
    из client-запросов.

    Возвращает mapping ``{str(owui_user_id): str(tg_chat_id)}``; при
    не-JSON / не-объекте / отсутствии — ``{}`` (fail-closed, без исключений).
    """
    env_raw = os.environ.get(_BINDING_ENV, "").strip()
    if env_raw:
        try:
            parsed = json.loads(env_raw)
        except Exception:
            logger.error(
                "delivery-policy: %s is not valid JSON — binding ignored "
                "(OWUI-only, fail-closed)", _BINDING_ENV,
            )
            return {}
        if not isinstance(parsed, dict):
            logger.error(
                "delivery-policy: %s must be a JSON object — binding ignored "
                "(OWUI-only, fail-closed)", _BINDING_ENV,
            )
            return {}
        return {str(k): str(v) for k, v in parsed.items()}
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}
    merged: Dict[str, str] = {}
    raw = (cfg or {}).get(_BINDING_CFG_KEY) if isinstance(cfg, dict) else None
    if isinstance(raw, dict):
        merged.update({str(k): str(v) for k, v in raw.items()})
    elif raw is not None:
        # Привязка в явном конфиге есть, но не-объект — ошибка конфига:
        # fail-closed по этому источнику, синк-файл халатность не маскирует.
        return {}
    # F3: синк-файл дополняет отсутствующие user_id (самый низкий приоритет).
    for uid, chat in _read_binding_file(_binding_file_path(cfg)).items():
        merged.setdefault(uid, chat)
    return merged


# --------------------------------------------------------------------------
# Решения и журнал подавлений
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    """Результат классификации: доставить или подавить с reason."""

    action: str  # "deliver" | "suppress"
    reason: str = ""  # короткий машинный код причины (для журнала)
    detail: str = ""  # человекочитаемое пояснение

    @property
    def suppressed(self) -> bool:
        return self.action == "suppress"

    @property
    def delivered(self) -> bool:
        return self.action == "deliver"


def deliver(reason: str = "default", detail: str = "") -> PolicyDecision:
    return PolicyDecision("deliver", reason, detail)


def suppress(reason: str = "", detail: str = "") -> PolicyDecision:
    return PolicyDecision("suppress", reason, detail)


_SUPPRESSION_LOG_NAME = "delivery-policy-suppressed.log"


def log_suppression(
    app: str,
    who: str,
    target: str,
    decision: PolicyDecision,
    snippet: str = "",
    extra: Optional[dict] = None,
) -> None:
    """Запись подавления в logger + файл журнала (JSON-lines).

    ``app`` — источник ("cron" | "kanban"); ``who`` — инициатор/получатель
    (для трассировки); ``target`` — куда собиралась доставка
    (``platform:chat_id``); ``snippet`` — обрезанный фрагмент контента.
    Никогда не бросает исключений: журнал не должен ломать доставку.
    """
    # Short log line: reason is the machine code, logger attaches detail.
    logger.info(
        "delivery-policy suppress: app=%s who=%s target=%s reason=%s%s",
        app, who, target, decision.reason or "unspecified",
        f" ({decision.detail})" if decision.detail else "",
    )
    record: Dict[str, Any] = {
        "ts": _utc_now_iso(),
        "app": app,
        "who": who,
        "target": target,
        "reason": decision.reason or "unspecified",
        "detail": decision.detail,
        "snippet": (snippet or "")[:300],
    }
    if extra:
        record.update(extra)
    try:
        from hermes_constants import get_hermes_home

        log_path = get_hermes_home() / "logs" / _SUPPRESSION_LOG_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # never raise from the journal
        logger.debug("delivery-policy journal write failed: %s", exc)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Проверка получателя (спека §3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetRef:
    """Минимальная адресная информация цели доставки."""

    platform: str
    chat_id: str
    user_id: str = ""
    thread_id: str = ""

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Optional[TargetRef]":
        if not d:
            return None
        platform = d.get("platform")
        chat_id = d.get("chat_id")
        if not platform or chat_id is None:
            return None
        return cls(
            str(platform).lower(),
            str(chat_id),
            str(d.get("user_id") or ""),
            str(d.get("thread_id") or "") if d.get("thread_id") else "",
        )


@dataclass(frozen=True)
class InitiatorRef:
    """Инициатор (кто породил контекст доставки)."""

    platform: Optional[str]
    chat_id: Optional[str]
    user_id: str = ""
    thread_id: str = ""

    @classmethod
    def from_origin(cls, origin: Optional[dict]) -> "Optional[InitiatorRef]":
        if not origin:
            return None
        platform = origin.get("platform")
        chat_id = origin.get("chat_id")
        if not platform or chat_id is None:
            return None
        return cls(
            str(platform).lower(),
            str(chat_id),
            str(origin.get("user_id") or origin.get("owner_id") or ""),
            str(origin.get("thread_id") or "") if origin.get("thread_id") else "",
        )


def verify_recipient(
    initiator: Optional[InitiatorRef],
    target: Optional[TargetRef],
    home_channel: Optional[str] = None,
    allowed_users: Optional[Sequence[str]] = None,
    owui_to_tg: Optional[Mapping] = None,
) -> bool:
    """ИНВ1: получатель == инициатор (fail-closed).

    Правила (спека §3):
      * target не Telegram  -> True (политика применяется ТОЛЬКО к TG).
      * initiator отсутствует или нет platform/chat_id:
            False, КРОМЕ случая доставки в заявленный чат владельца
            (админ-доставка в home, строка матрицы «01-orchestrator →
            125805788 -> deliver при user in ALLOWED») — целевой chat_id
            входит в allowed_users владельца.
      * одна платформа: chat_id совпадает, ИЛИ target=home и инициатор в
            allowed_users (владелец получает админ-доставки).
      * кросс-платформа (напр. api_server -> TG home): только если
            initiator.user_id входит в allowed_users владельца.
      * T2 (спека §8.2.3): при заданной серверной привязке ``owui_to_tg``
            (непустой mapping ``{owui_user_id: tg_chat_id}``) кросс-платформа
            api_server→telegram проходит ТОЛЬКО если
            ``target.chat_id == owui_to_tg[initiator.user_id]``.  Сама
            серверная привязка (F3, H-5 red team t_d0bcecda) — достаточная
            авторизация маршрута в СОБСТВЕННЫЙ bound-чат пользователя:
            ALLOWED/home-гейт на bound-чат не накладывается, иначе второй
            (и последующий) привязанный пользователь голодает, когда
            ALLOWED/home настроены только на первого (инициатора).
            Изоляция сохраняется конструктивно: отсечение по
            ``target != bound_chat`` и наличие самой записи привязки
            (не-авторизованные/непривязанные пользователи — False).
            Пустая привязка (привязка не сконфигурирована) сохраняет
            прежнее V1-поведение (F1/F3 не меняются).
    """
    if target is None or not target.platform:
        return False
    if target.platform != "telegram":
        # Политика — только про Telegram; остальные платформы не трогаем.
        return True
    allowed: Set[str] = {str(u).strip() for u in (allowed_users or ()) if str(u).strip()}

    # Админ-доставка (V3): нет известного инициатора (origin отсутствует /
    # не имеет адреса), но target — явный чат владельца из ALLOWED.  Джоб
    # сконфигурирован оператором с hard-coded telegram:<owner_chat> — это
    # админ-доставка в home («01-orchestrator → 125805788 -> deliver при
    # user in ALLOWED»).  Не открывает дверь чужим: без известного
    # инициатора и не-allowed-чата возвращаем False.
    known_initiator = initiator is not None and bool(
        initiator.platform and initiator.chat_id is not None
    )
    if not known_initiator:
        return target.chat_id in allowed

    if initiator.platform == target.platform:
        if initiator.chat_id == target.chat_id:
            return True
        # target — home владельца, инициатор — один из allowed: владелец
        # получает админ-доставки даже со своей secondary-учётки.
        if target.chat_id == home_channel and initiator.chat_id in allowed:
            return True
        return False

    # Кросс-платформенный фолбэк (V1: origin=api_server, target=TG home).
    # Только владелец (user_id in ALLOWED) проходит; чужой api_server-user
    # (OWUI) — fail-closed, в home владельца не льём.
    if initiator.platform != target.platform:
        if initiator.platform == "api_server" and owui_to_tg:
            # T2 (спека §8.2.3): кросс-платформа api_server→telegram
            # адресуется ТОЛЬКО собственным чатом привязки инициатора.
            binding: Mapping = {str(k): str(v) for k, v in dict(owui_to_tg).items()}
            bound_chat = binding.get(str(initiator.user_id or ""))
            if bound_chat is None:
                # Не в привязке → verf=False → TG suppressed даже для
                # ценного контента (спека §8.3).
                return False
            if str(target.chat_id) != bound_chat:
                # Перекрёстная утечка в чужой TG-чат конструктивно невозможна.
                return False
            # H-5 (red team t_d0bcecda): серверная привязка сама по себе —
            # достаточная авторизация маршрута в СОБСТВЕННЫЙ bound-чат
            # пользователя (target == bound_chat уже проверено выше).  Ранее
            # здесь требовалось ``bound_chat == home_channel or bound_chat in
            # allowed``, что голодало второго (и последующих) привязанных
            # пользователей, когда ALLOWED/home настроены только на первого
            # (инициатора).  Изоляция неизменна: чужие bound-чаты и
            # непривязанные пользователи по-прежнему False.
            return True
        if (
            target.chat_id == home_channel
            and initiator.user_id
            and str(initiator.user_id) in allowed
        ):
            return True
        return False

    return False


def verify_subscription_recipient(
    subscription: Optional[dict],
    allowed_users: Optional[Sequence[str]] = None,
    *,
    task_tg_owner: Optional[str] = "",
) -> bool:
    """Подписка kanban: доставляет только владельцу задачи (ИНВ1 для V4, D0 §3).

    К1 RF3: подписка ДРУГОГО (пусть и разрешённого) пользователя на чужую
    задачу не должна доставлять ему события.  Поэтому недостаточно членства
    в ``allowed_users`` — подписчик должен быть ВЛАДЕЛЬЦЕМ задачи.

    ``task_tg_owner`` — TG-идентичность владельца ветки (пользователь,
    создавший корень ветки через /kanban create: ``SELECT user_id`` из
    telegram-подписки корня ветки).  Это НЕ profile-slug (``created_by`` /
    ``assignee`` хранят имена профилей, а не TG-id), поэтому сравнивать
    ``sub.user_id`` с ними нельзя — такие сравнения всегда ломали бы
    легитимного владельца (см. F3 T1).

    Когда владелец неизвестен (``task_tg_owner`` пуст) — остаётся прежняя
    проверка allowlist (не ломаем канал, где tg-владелец не разрешим).

    api_server-подписки возвращаются как есть: они верифицируются серверным
    реестром (``openwebui_origin.py``) и не имеют TG-user_id для сравнения.
    """
    if not subscription:
        return False
    platform = str(subscription.get("platform") or "").lower()
    if platform != "telegram":
        return True  # api_server-подписки верифицируются серверным реестром
    sub_user = str(subscription.get("user_id") or subscription.get("chat_id") or "")
    allowed: Set[str] = {str(u).strip() for u in (allowed_users or ()) if str(u).strip()}
    if not (bool(sub_user) and sub_user in allowed):
        return False
    owner = str(task_tg_owner or "").strip()
    if owner:
        # D0 §3: telegram-подписка доставляет ТОЛЬКО владельцу ветки, а не
        # любому allow-пользователю. Fail-closed.
        return sub_user == owner
    return True
