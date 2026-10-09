"""Deterministic per-turn model routing for Hermes gateway.

Routing is based on the requested work, not prompt length. Explicit user and
session choices always win over automatic policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class RouteDecision:
    tier: Optional[str]
    provider: str
    model: str
    reason: str
    explicit: bool = False


_EXPLICIT_CHOICE = re.compile(
    r"(?:^|\b)(?:используй(?:те)?|выбери(?:те)?|переключи(?:сь|те)?(?:\s+на)?|"
    r"ответь(?:те)?\s+(?:через|на)|запусти(?:те)?\s+(?:через|на)|"
    r"use|choose|switch\s+to|run\s+(?:with|on)|answer\s+with)\s+"
    r"(?P<alias>[a-zа-я0-9_.:/+-]+)", re.IGNORECASE,
)
_FAST_EXACT = {"ок", "окей", "да", "нет", "спасибо", "понял", "понятно", "yes", "no", "ok", "thanks"}
_FAST = re.compile(r"\b(?:быстро|побыстрее|fast|quick(?:ly)?|tl;dr)\b|одним словом|да или нет|без (?:подробностей|объяснений)", re.I)
_CRITICAL = re.compile(
    r"\b(?:юридическ\w*\s+(?:заключени\w*|вывод\w*|мнение\w*)|правов\w*\s+(?:заключени\w*|вывод\w*)|"
    r"необратим\w*|удали(?:ть|те)\b|опубликуй\w*|перевод\w*\s+денег|подпиш\w*\s+(?:договор|контракт)|"
    r"legal\s+(?:opinion|conclusion)|irreversible|delete\b|publish\b)\b", re.I)
_XLARGE = re.compile(
    r"\b(?:юридическ\w*|правов\w*|норматив\w*|регламент\w*|договор\w*|точн\w*\s+ссылк\w*|"
    r"нескольк\w*\s+(?:источник\w*|первичн\w*\s+источник\w*|шаг\w*)|аудит\w*|верифицируй\w*|"
    r"legal|regulatory|contract|primary\s+sources?|audit|verify)\b", re.I)
_LARGE = re.compile(
    r"\b(?:найди|поищи|исследуй|сравни|проверь|проанализируй|создай|собери|измени|исправь|"
    r"настрой|установи|запусти|разверни|файл\w*|docx|xlsx|pptx|pdf|html|репозитор\w*|git|github|код\w*|тест\w*|скрипт\w*|"
    r"find|search|research|compare|check|analyse|analyze|create|build|modify|fix|configure|install|deploy|file|code|tests?)\b", re.I)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(x.get("text") or "") for x in value if isinstance(x, Mapping)).strip()
    return str(value or "").strip()


def _entry(config: Mapping[str, Any], tier: str) -> tuple[str, str]:
    models = config.get("models") if isinstance(config.get("models"), Mapping) else {}
    item = models.get(tier) if isinstance(models.get(tier), Mapping) else {}
    return str(item.get("provider") or config.get("provider") or "").strip(), str(item.get("model") or "").strip()


def _explicit_alias(text: str, config: Mapping[str, Any]) -> Optional[tuple[str, Mapping[str, Any]]]:
    aliases: dict[str, tuple[str, Mapping[str, Any]]] = {}
    explicit = config.get("explicit_models") if isinstance(config.get("explicit_models"), Mapping) else {}
    for name, entry in explicit.items():
        if isinstance(entry, Mapping):
            aliases[str(name).casefold()] = (str(name), entry)
    models = config.get("models") if isinstance(config.get("models"), Mapping) else {}
    for tier, entry in models.items():
        if isinstance(entry, Mapping) and entry.get("model"):
            model = str(entry["model"])
            aliases.setdefault(model.casefold(), (model, entry))
            aliases.setdefault(re.split(r"[/:-]", model.casefold())[-1], (model, entry))
    match = _EXPLICIT_CHOICE.search(text)
    if not match:
        return None
    candidate = match.group("alias").strip(".,!?;:()[]{}\"'").casefold()
    return aliases.get(candidate)


def classify_request(text: Any) -> tuple[str, str]:
    normalized = _text(text)
    folded = normalized.casefold().strip(" \t\r\n.!?,;:")
    if folded in _FAST_EXACT or _FAST.search(normalized):
        return "small", "very-fast or explicitly terse request"
    if _CRITICAL.search(normalized):
        return "critical", "irreversible action or legal conclusion requires critical review"
    if _XLARGE.search(normalized):
        return "xlarge", "legal, primary-source, audit, or multi-step verification work"
    if _LARGE.search(normalized):
        return "large", "tool-using, artifact, research, or implementation work"
    return "medium", "simple bounded task"


def route_turn(user_message: Any, config: Mapping[str, Any], *, current_model: str, current_provider: str,
               surface: Optional[str] = None, explicit_session_override: bool = False,
               has_attachments: bool = False) -> RouteDecision:
    """Resolve one gateway turn; automatic routing never overrides a user choice."""
    baseline = RouteDecision(None, str(current_provider or ""), str(current_model or ""), "adaptive routing disabled")
    if explicit_session_override:
        return RouteDecision(None, baseline.provider, baseline.model, "explicit session model override", True)
    if not isinstance(config, Mapping) or not bool(config.get("enabled")):
        return baseline
    surfaces = config.get("surfaces")
    if isinstance(surfaces, list) and surfaces and surface not in surfaces:
        return RouteDecision(None, baseline.provider, baseline.model, f"surface {surface or '<none>'} is outside adaptive routing scope")
    text = _text(user_message)
    chosen = _explicit_alias(text, config)
    if chosen:
        label, entry = chosen
        return RouteDecision(None, str(entry.get("provider") or baseline.provider), str(entry.get("model") or baseline.model), f"explicit user model choice: {label}", True)
    tier, reason = ("large", "attachment/media work requires tool-capable routing") if has_attachments else classify_request(text)
    provider, model = _entry(config, tier)
    if not model:
        return RouteDecision(None, baseline.provider, baseline.model, f"tier {tier} is not configured")
    return RouteDecision(tier, provider or baseline.provider, model, reason)
