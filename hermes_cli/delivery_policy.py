"""
Telegram delivery policy gate (D0 ``tg-delivery-hygiene``, задача F1).

ЕДИНАЯ точка принятия решения о том, доставлять ли сообщение в Telegram
из общего кода Hermes (cron-доставка и kanban-уведомления).  Классификатор
НЕ-LLM: чистые эвристики по содержимому + проверка получателя (fail-closed).

Инварианты (NIGHT-PLAN.md §1–§5, спека `research/delivery-policy.md`):
  ИНВ1 — recipient == initiator: в TG-чат человека попадает только то,
         что породил именно он (его задача / подписка / сессия).
  ИНВ2 — доставляются только ценные классы: файлы/артефакты, финальные
         отчёты, требующие действий человека тревоги.  НЕ доставляются:
         промежуточные закрытия, служебные статусы, дешёвые рутинные
         отчёты без дельты.
  ИНВ3 — не ломать существующее: api_server/OWUI exact-origin, ``[SILENT]``,
         alert-once (blocked_config/drift) остаются как есть.

Политика включается конфиг-флагом ``telegram_delivery_policy: on|off``
(по умолчанию ``on``).  Все подавления ОБЯЗАНЫ логироваться с reason —
и в logger, и в файл журнала подавлений (grep-доступно).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Конфиг-флаг
# --------------------------------------------------------------------------

#: Ключи, в которых ищется ``telegram_delivery_policy`` (в порядке приоритета).
_FLAG_KEYS: Sequence[str] = ("telegram_delivery_policy",)
_ENV_FLAG = "HERMES_TELEGRAM_DELIVERY_POLICY"


def policy_enabled(cfg: Optional[dict] = None) -> bool:
    """Read the ``telegram_delivery_policy`` flag (default ON).

    Precedence (first decisive value wins):
      1. Env ``HERMES_TELEGRAM_DELIVERY_POLICY`` — override allows tests and
         operators to flip the gate deterministically without touching config.
      2. Per-call ``cfg`` dict (``telegram_delivery_policy``).
      3. ``load_config()`` (same key at top level).
      4. Default ON.

    Any value other than a case-insensitive ``"off"`` means the policy is
    active (default on, per requirement).
    """
    env_val = os.environ.get(_ENV_FLAG, "").strip().lower()
    if env_val:
        return env_val != "off"
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}
    if isinstance(cfg, dict):
        for key in _FLAG_KEYS:
            val = cfg.get(key)
            if val is not None:
                # YAML 1.1 parsers turn an unquoted ``off`` into boolean False
                # (and ``on`` into True): handle both the literal strings and
                # the parsed booleans so ``telegram_delivery_policy: off`` in
                # config.yaml really disables the gate.
                if val is False:
                    return False
                return str(val).strip().lower() != "off"
    return True


# --------------------------------------------------------------------------
# Серверная привязка OWUI-пользователь ⇄ TG-чат (T2, спека §8.1)
# --------------------------------------------------------------------------

#: Конфиг-ключ и env-переменная с привязкой ``{owui_user_id: tg_chat_id}``.
_BINDING_CFG_KEY = "openwebui_telegram_users"
_BINDING_ENV = "HERMES_OPENWEBUI_TELEGRAM_USERS"


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
    Оба источника читаются только серверной стороной (scheduler), никогда
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
    raw = (cfg or {}).get(_BINDING_CFG_KEY) if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


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
# Эвристика "рутинный отчёт без дельты" (спека §6а)
# --------------------------------------------------------------------------

#: Дешёвые текстовые штампы no-delta (распознаются после JSON-попытки).
_LOW_STAMPS: Sequence[str] = (
    "copied: 0",
    "nothing to do",
    "no changes",
    "похоже в норме",
    "0 changed",
    "already up to date",
)


def is_routine_report(content: Optional[str]) -> bool:
    """True для дешёвых рутинных отчётов без дельты (mirror-rag и подобные).

    Сначала пробуем распарсить как JSON счётчика (скрипты-зеркала печатают
    ``{"copied": N, "missing_in_kb": M, "errors": E}``): если всё нулевое —
    рутина.  Иначе — текстовые штампы (§6а спеки).
    """
    if not content:
        return False
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            if (
                obj.get("copied", 0) == 0
                and obj.get("missing_in_kb", 0) == 0
                and obj.get("errors", 0) == 0
            ):
                return True
    except Exception:
        pass
    low = content.lower()
    return any(stamp in low for stamp in _LOW_STAMPS)


# --------------------------------------------------------------------------
# Детекторы системных алертов (согласовано с cron/scheduler.py)
# --------------------------------------------------------------------------

_BLOCKED_CONFIG_MARKER = "[blocked_config]"
_DRIFT_SKIP_MARKER = "[drift_skip]"
_BLOCKED_CONFIG_SILENT = "[blocked_config_silent]"
_DRIFT_SKIP_SILENT = "[drift_skip_silent]"

#: Маркеры, которыми scheduler помечает финальный текст системной тревоги.
_ALERT_PREFIXES: Sequence[str] = (
    "⛔ Cron",       # blocked_config (scheduler.py ~6920–6926)
    "⚠️ Cron",       # drift_skip (scheduler.py ~6940–6943)
)
_FAIL_TEXT_STAMPS: Sequence[str] = (
    "cron job failed",
    "cron failed",
    "job failed after",
    "run failed",
    "delivery failed",
)


def detect_system_alert(content: Optional[str]) -> Optional[str]:
    """Вернуть имя системной тревоги в ``content`` или None.

    Согласовано с фактическими маркерами scheduler.py: блокировки конфига,
    drift-пропуски и тексты провалов cron, которые ДОЛЖНЫ доставляться
    (alert-once контракт) независимо от прочих эвристик.
    """
    if not content:
        return None
    for marker in (_BLOCKED_CONFIG_MARKER, _DRIFT_SKIP_MARKER):
        if marker in content:
            return "blocked_config" if "blocked" in marker else "drift_skip"
    low = content.strip().lower()
    for prefix in _ALERT_PREFIXES:
        if low.startswith(prefix.lower()):
            return "cron_alert"
    for stamp in _FAIL_TEXT_STAMPS:
        if stamp in low[:400]:
            return "cron_failure"
    return None


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
            ``target.chat_id == owui_to_tg[initiator.user_id]`` и этот чат
            ∈ ALLOWED/home.  Пустая привязка (привязка не сконфигурирована)
            сохраняет прежнее V1-поведение (F1/F3 не меняются).
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
            return bound_chat == home_channel or bound_chat in allowed
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


# --------------------------------------------------------------------------
# Классификатор контента (спека §2 — порядок веток критичен)
# --------------------------------------------------------------------------

#: События, требующие действий человека — доставлять всегда (§2).
HUMAN_ACTION_KINDS: Set[str] = {
    "blocked",
    "needs_input",
    "capability",
    "review_requested",
    "crashed",
    "gave_up",
    "block_loop_detected",
    "timed_out",
}

#: Рутинные статусные закрытия — подавлять без дельты (§2 п.5).
ROUTINE_EVENT_KINDS: Set[str] = {"completed", "status", "unblocked", "archived"}


def classify(content: Optional[str], context: Optional[dict] = None) -> PolicyDecision:
    """Вернуть решение для ``content`` с учётом ``context``.

    ``context`` (опциональные ключи):
      kind            "cron" | "kanban" | ... (по умолчанию "cron")
      event_kind      для kanban-событий (completed/blocked/status/...)
      no_agent        bool: джоб без LLM (mirror-rag, watchdog)
      completed_terminal  bool: явный маркер ФИНАЛА (task.status=="archived"
                            либо закрытие корня/ветки). НЕ «есть summary»:
                            промежуточная суб-задача с summary — НЕ терминал
                            (K1 RF2). Вычисляет вызывающий (kanban_watchers).
      artifacts       list[str]: артефакты, заявленные в payload (kanban)
      has_media       bool: в контенте есть MEDIA:/attachment (extract_media)
      initiator       InitiatorRef | None
      target          TargetRef | None
      home_channel    str | None
      allowed_users   list[str] | None
      system_alert    str | None (предвычисленный детект)
    """
    ctx = context or {}
    content = (content or "").strip()
    kind = ctx.get("kind", "cron")
    event_kind = ctx.get("event_kind")
    artifacts: List[str] = list(ctx.get("artifacts") or ())

    # ---- 0. Каноническая тишина (подавление; уже частично обработана) ----
    if not content:
        return suppress("empty stdout/content")

    # ---- 1. Жёсткие системные алерты - доставка БЕЗ условий ----
    system_alert = ctx.get("system_alert") or detect_system_alert(content)
    if system_alert:
        return deliver("system_alert", detail=f"alert={system_alert}")

    # ---- 2. Класс «требует действий человека» - всегда ----
    if event_kind in HUMAN_ACTION_KINDS:
        return deliver("human_action", detail=f"event_kind={event_kind}")

    # ---- 3. Артефакты (файлы/отчёты) - всегда по ИНВ2 ----
    has_media = bool(ctx.get("has_media")) or bool(
        re.search(r"\bMEDIA\s*[:：]\s*\S+", content or "", re.IGNORECASE)
    )
    if has_media or artifacts:
        return deliver("artifacts", detail=f"media={has_media} artifacts={len(artifacts)}")

    # ---- 6. Дешёвый рутинный отчёт без дельты (no_agent watchdogs) ----
    # Вынесено ВЫШЕ §2.4 (final reports): для no_agent-джобов (mirror-rag,
    # limits-monitor) рутинный no-delta должен подавляться, а не проходить
    # как «final report» только потому, что джоб завершился терминально.
    # Спека §5 допускает «при отсутствии артефактов отсечку “без дельты” по
    # решению F1-реализации».  Для агентных джобов это не мешает: у них
    # флаг no_agent=False и рутина не удаляется этим правилом.
    if ctx.get("no_agent") and is_routine_report(content):
        return suppress("routine no-delta report")

    # ---- 4. Финальные (итоговые) отчёты - ИНВ2 ----
    if kind == "cron":
        if ctx.get("completed_terminal"):
            return deliver("final_report")
        if not ctx.get("no_agent"):
            return deliver("agent_final_response")

    # ---- 5. Рутинное закрытие/статус без дельты - подавить (ИНВ2) ----
    if event_kind in ROUTINE_EVENT_KINDS:
        if not ctx.get("completed_terminal"):
            return suppress("routine status close", detail=f"event_kind={event_kind}")

    # ---- 7. Всё остальное - доставлять только при выполненном verify ----
    if verify_recipient(
        ctx.get("initiator"),
        ctx.get("target"),
        ctx.get("home_channel"),
        ctx.get("allowed_users"),
        owui_to_tg=ctx.get("owui_to_tg"),
    ):
        return deliver("default")
    return suppress("recipient mismatch")


def default_context(
    *,
    kind: str = "cron",
    initiator: Optional[InitiatorRef] = None,
    target: Optional[TargetRef] = None,
    home_channel: Optional[str] = None,
    allowed_users: Optional[Sequence[str]] = None,
    owui_to_tg: Optional[Mapping] = None,
    **extra: Any,
) -> dict:
    """Собрать базовый context с общими полями для классификатора."""
    ctx: Dict[str, Any] = {
        "kind": kind,
        "initiator": initiator,
        "target": target,
        "home_channel": home_channel,
        "allowed_users": allowed_users,
        "owui_to_tg": owui_to_tg,
    }
    ctx.update(extra)
    return ctx
