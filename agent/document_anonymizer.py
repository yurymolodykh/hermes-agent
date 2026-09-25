"""Local, document-scoped anonymization before document content reaches an LLM.

This module deliberately does *not* expose a generic outbound-message redactor:
ordinary chat text must remain byte-for-byte unchanged.  Callers opt in only
for uploaded/attached document bytes or explicitly marked document-context
blocks.  Extraction and replacement happen locally and fail closed.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DOCUMENT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log",
        ".pdf", ".doc", ".docx", ".docm", ".rtf", ".odt", ".ods", ".odp",
        ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".epub",
    }
)
_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log"})
_SOURCE_EXTENSIONS = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".css", ".scss", ".sql", ".sh", ".ps1"}
)
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})

# Currency is mandatory unless the number contains a grouped thousands
# separator.  This avoids turning dates, clause numbers and IDs into sums.
_MONEY_RE = re.compile(
    r"(?<![\w])(?:"
    r"(?:[$€£₽]\s*)?\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+(?:[.,]\d{1,2})?"
    r"|(?:[$€£₽]\s*)?\d+(?:[.,]\d{1,2})?\s*(?:руб(?:л(?:ей|я|ь)?)?\.?|р\.|₽|RUB|USD|EUR|доллар(?:ов|а)?|евро)"
    r")(?:\s*(?:руб(?:л(?:ей|я|ь)?)?\.?|р\.|₽|RUB|USD|EUR|доллар(?:ов|а)?|евро))?",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-яЁё]{2,}(?![\w-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)")
_PASSPORT_RE = re.compile(r"(?i)(?:паспорт\s*(?:серия\s*)?)?\b\d{2}\s?\d{2}\s?\d{6}\b")
_SNILS_RE = re.compile(r"(?i)(?:СНИЛС\s*[:№]?\s*)?\b\d{3}[- ]?\d{3}[- ]?\d{3}[ -]?\d{2}\b")
_INN_RE = re.compile(r"(?i)(?:ИНН\s*[:№]?\s*)\d{10,12}\b")
_ACCOUNT_RE = re.compile(r"(?i)(?:(?:р/?с|расч[её]тный сч[её]т|сч[её]т)\s*[:№]?\s*)\d{20}\b")
# Privacy-biased Russian full-name heuristic. Requiring three title-cased words
# avoids ordinary prose while covering nominative and common inflected forms.
_RU_FULL_NAME_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё0-9-])(?:[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+){2}[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?(?![A-Za-zА-Яа-яЁё0-9-])")
_RU_SURNAME_INITIALS_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё0-9-])[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+(?:[А-ЯЁ]\.[ ]?){1,2}(?![A-Za-zА-Яа-яЁё0-9-])")
_RU_SCHOOL_RE = re.compile(r"(?i)\b(?:ГБОУ|ГБПОУ|МБОУ|МОУ|МАОУ|Школа)\s+(?:Школа\s*)?№?\s*\d+[\w-]*")
_RU_INITIAL_NAME_RE = re.compile(r"(?<![\w-])(?:[А-ЯЁ]\.[ ]?){1,2}[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?(?![\w-])")
_LATIN_FULL_NAME_RE = re.compile(
    r"(?<![\w-])[A-Z][a-z]+(?:-[A-Z][a-z]+)?(?:\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)?){1,3}(?![\w-])"
)
_LABELED_NAME_RE = re.compile(
    r"(?i)(?P<label>\b(?:ФИО|получатель|заказчик|исполнитель|сотрудник)\s*[:№-]?\s*)"
    r"(?P<name>[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})"
)
_FORM_NAME_RE = re.compile(
    r"(?i)(?P<label>\(?\s*(?:ФИО|Ф\.\s*И\.)\s*\)?\s*)[_\s]*"
    r"(?P<name>[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})"
)
_LABELED_PII_RE = re.compile(
    r"(?im)(?P<label>\b(?:дата рождения|адрес(?: регистрации| проживания)?|место рождения)\s*[:№-]?\s*)"
    r"(?P<value>[^\n;]{3,200})"
)
_OPENWEBUI_SOURCE_RE = re.compile(r"(?is)(<source\b(?P<attrs>[^>]*)>)(?P<body>.*?)(</source>)")
_OPENWEBUI_ATTR_RE = re.compile(
    r'''(?is)(?P<name>[A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')'''
)
_OPENWEBUI_RESOURCE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_OPENWEBUI_ATTACHED_FILE_RE = re.compile(r"(?is)<file\b(?P<attrs>[^>]*)/?>")


@dataclass
class _Tokens:
    people: dict[str, str] = field(default_factory=dict)
    organizations: dict[str, str] = field(default_factory=dict)
    sums: dict[str, str] = field(default_factory=dict)

    def person(self, value: str) -> str:
        key = " ".join(value.casefold().split())
        return self.people.setdefault(key, f"person{len(self.people) + 1}")

    def organization(self, value: str) -> str:
        key = " ".join(value.casefold().split())
        return self.organizations.setdefault(key, f"organization{len(self.organizations) + 1}")

    def amount(self, value: str) -> str:
        key = re.sub(r"\s+", " ", value.casefold()).strip().rstrip(".")
        return self.sums.setdefault(key, f"SUM{len(self.sums) + 1}")


def document_anonymization_enabled() -> bool:
    """Resolve the feature flag; env override is useful for tests/workers."""
    env = os.getenv("HERMES_DOCUMENT_ANONYMIZATION")
    if env is not None:
        return env.strip().casefold() not in _FALSE_VALUES
    try:
        from hermes_cli.config import load_config_readonly

        privacy = (load_config_readonly().get("privacy") or {})
        return bool(privacy.get("anonymize_documents", False))
    except Exception:
        return False


def is_document_path(path: str | Path) -> bool:
    return Path(path).suffix.casefold() in DOCUMENT_EXTENSIONS


def anonymize_document_text(text: str) -> str:
    """Replace detected personal data and monetary amounts deterministically."""
    tokens = _Tokens()

    def person(match: re.Match[str]) -> str:
        return tokens.person(match.group(0))

    def labeled(match: re.Match[str]) -> str:
        return f"{match.group('label')}{tokens.person(match.group('name'))}"

    def labeled_pii(match: re.Match[str]) -> str:
        return f"{match.group('label')}{tokens.person(match.group('value'))}"

    def organization(match: re.Match[str]) -> str:
        return tokens.organization(match.group(0))

    result = _EMAIL_RE.sub(person, text)
    result = _PHONE_RE.sub(person, result)
    result = _SNILS_RE.sub(person, result)
    result = _INN_RE.sub(person, result)
    result = _ACCOUNT_RE.sub(person, result)
    result = _PASSPORT_RE.sub(person, result)
    result = _LABELED_PII_RE.sub(labeled_pii, result)
    result = _RU_SCHOOL_RE.sub(organization, result)
    result = _FORM_NAME_RE.sub(labeled, result)
    result = _LABELED_NAME_RE.sub(labeled, result)
    result = _RU_SURNAME_INITIALS_RE.sub(person, result)
    result = _RU_INITIAL_NAME_RE.sub(person, result)
    result = _RU_FULL_NAME_RE.sub(person, result)
    result = _LATIN_FULL_NAME_RE.sub(person, result)
    def amount(match: re.Match[str]) -> str:
        numeric = re.search(r"\d[\d \u00a0\u202f.,]*", match.group(0))
        key = numeric.group(0).rstrip(".,") if numeric else match.group(0)
        return tokens.amount(key)

    result = _MONEY_RE.sub(amount, result)
    return result


def anonymize_document_blocks(text: str) -> str:
    """Anonymize known attached-document blocks, never ordinary message text."""
    marker = "--- Attached Context ---"
    if marker not in text:
        return text
    prefix, suffix = text.split(marker, 1)
    return prefix + marker + anonymize_document_text(suffix)


def _openwebui_source_attributes(raw_attributes: str) -> dict[str, str]:
    """Parse only quoted Open WebUI source attributes; never trust paths from it."""
    values: dict[str, str] = {}
    for match in _OPENWEBUI_ATTR_RE.finditer(raw_attributes):
        value = match.group("double") if match.group("double") is not None else match.group("single")
        values[match.group("name").casefold()] = html.unescape(value or "")
    return values


def _openwebui_uploads_root() -> Path:
    """Return the co-located Open WebUI upload directory without accepting a client path."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "openwebui" / "uploads"


def _sanitized_openwebui_upload(attributes: dict[str, str]) -> str | None:
    """Extract the real local upload, when referenced by a safe Open WebUI UUID.

    Open WebUI normally sends an extracted RAG block to the OpenAI-compatible
    backend rather than document bytes.  On a co-located installation, use the
    upload store as the authoritative local source.  The user-controlled source
    tag can select only one basename under that fixed root; traversal, symlinks,
    unsupported types, and extraction failures all fall back to the already
    supplied block instead of exposing raw bytes or a host path to the model.
    """
    resource_id = attributes.get("resource-id", "").strip()
    if not _OPENWEBUI_RESOURCE_ID_RE.fullmatch(resource_id):
        return None
    root = _openwebui_uploads_root()
    try:
        root = root.resolve(strict=True)
    except OSError:
        return None
    candidates = list(root.glob(f"{resource_id}_*"))
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    try:
        resolved = candidate.resolve(strict=True)
        if resolved.parent != root or not resolved.is_file() or not is_document_path(resolved):
            return None
        return sanitized_document_text(resolved)
    except Exception:
        return None


def anonymize_openwebui_attachment_tags(text: str) -> str:
    """Expand Open WebUI's ``<attached_files>`` references from the local upload store.

    Some Open WebUI helper requests carry the original attachment as a compact
    ``<file id=UUID .../>`` descriptor rather than a ``<source>`` block.  The
    descriptor is not document content, so passing it through unchanged makes
    the agent see only a filename.  Resolve only a UUID-named file under the
    fixed local upload root and insert its locally anonymized text; malformed or
    missing references remain unchanged and never select an arbitrary path.
    """
    if not document_anonymization_enabled() or "<attached_files" not in text.casefold():
        return text

    def expand(match: re.Match[str]) -> str:
        attributes = _openwebui_source_attributes(match.group("attrs"))
        resource_id = attributes.get("id", "").strip()
        clean = _sanitized_openwebui_upload({"resource-id": resource_id})
        if clean is None:
            return match.group(0)
        return "\n[Анонимизированное содержимое вложенного документа]\n" + clean + "\n"

    return _OPENWEBUI_ATTACHED_FILE_RE.sub(expand, text)


def anonymize_openwebui_source_blocks(text: str) -> str:
    """Redact OpenWebUI document blocks, preferring the co-located upload bytes.

    This makes uploads available to the local anonymizer even though Open WebUI
    sends only RAG-extracted text to the API.  Only anonymized text crosses the
    model boundary; neither the original path nor original bytes are exposed.
    """
    if not document_anonymization_enabled() or "<source" not in text.casefold():
        return text

    def redact(match: re.Match[str]) -> str:
        attributes = _openwebui_source_attributes(match.group("attrs"))
        clean = _sanitized_openwebui_upload(attributes)
        if clean is None:
            clean = anonymize_document_text(match.group("body"))
        return '<source anonymized="true">' + clean + match.group(4)

    return _OPENWEBUI_SOURCE_RE.sub(redact, text)


def anonymize_openwebui_content(content: Any) -> Any:
    """Apply source-block redaction to OpenAI text content shapes."""
    if isinstance(content, str):
        return anonymize_openwebui_source_blocks(content)
    if isinstance(content, list):
        return [
            {
                **part,
                "text": anonymize_openwebui_source_blocks(str(part.get("text") or "")),
            }
            if isinstance(part, dict)
            and str(part.get("type") or "").casefold() in {"text", "input_text", "output_text"}
            else part
            for part in content
        ]
    return content


def extract_document_text_local(path: str | Path) -> str:
    """Extract locally. No network fallback is permitted."""
    path = Path(path)
    if path.suffix.casefold() in _TEXT_EXTENSIONS:
        raw = path.read_bytes()
        if len(raw) > 25 * 1024 * 1024:
            raise ValueError("document exceeds the 25 MiB anonymization limit")
        for encoding in ("utf-8-sig", "utf-16", "cp1251"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("text document encoding is not supported")
    from tools.read_extract import extract_document_text

    extracted = extract_document_text(str(path))
    if "EXTRACTION COVERAGE WARNING" in extracted:
        raise ValueError("document has pages without an extractable text layer")
    if not extracted.strip():
        raise ValueError("document contains no extractable text")
    return extracted


def sanitized_document_text(path: str | Path) -> str:
    return anonymize_document_text(extract_document_text_local(path))


def sanitized_document_bytes(data: bytes, filename: str) -> str:
    """Extract and anonymize an uploaded document without persisting raw bytes."""
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("document exceeds the 25 MiB anonymization limit")
    suffix = Path(filename).suffix.casefold()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise ValueError("unsupported document type")
    if suffix in _TEXT_EXTENSIONS:
        for encoding in ("utf-8-sig", "utf-16", "cp1251"):
            try:
                return anonymize_document_text(data.decode(encoding))
            except UnicodeDecodeError:
                continue
        raise ValueError("text document encoding is not supported")
    from tools.read_extract import extract_document_bytes

    extracted = extract_document_bytes(data, filename)
    if "EXTRACTION COVERAGE WARNING" in extracted or not extracted.strip():
        raise ValueError("document does not have a complete extractable text layer")
    return anonymize_document_text(extracted)


def _is_document_attachment(path: str, mime: str, message_type: Any) -> bool:
    if is_document_path(path):
        return True
    if Path(path).suffix.casefold() in _SOURCE_EXTENSIONS:
        return False
    mime = (mime or "").casefold()
    if mime.startswith(("text/", "application/pdf")):
        return True
    value = str(getattr(message_type, "value", message_type) or "").casefold()
    return value.endswith("document") or value == "file"


def sanitize_document_event(message_text: str, event: Any) -> str:
    """Replace attached documents with anonymous text and hide original paths.

    Non-document media is preserved. Any extraction/anonymization failure is
    fail-closed: the original path/content is removed from the model-visible
    event and a neutral warning is added instead.
    """
    if not document_anonymization_enabled():
        return message_text

    paths = list(getattr(event, "media_urls", None) or [])
    mimes = list(getattr(event, "media_types", None) or [])
    kept_paths: list[str] = []
    kept_mimes: list[str] = []
    blocks: list[str] = []
    result = message_text
    saw_document = False

    for index, path in enumerate(paths):
        mime = str(mimes[index] if index < len(mimes) else "")
        if not _is_document_attachment(str(path), mime, getattr(event, "message_type", None)):
            kept_paths.append(path)
            kept_mimes.append(mime)
            continue
        saw_document = True
        try:
            raw = extract_document_text_local(path)
            clean = anonymize_document_text(raw)
            if raw and raw in result:
                # Adapters may prefix an exact copy of textual document bytes.
                # Replace only those bytes, preserving the authored caption.
                result = result.replace(raw, clean)
            elif raw and ("\ufeff" + raw) in result:
                result = result.replace("\ufeff" + raw, clean)
            else:
                blocks.append(f"[Анонимизированное содержимое документа {index + 1}]\n{clean}")
        except Exception as exc:
            blocks.append(
                "[Документ не передан модели: локальная анонимизация не выполнена "
                f"({type(exc).__name__}).]"
            )

    if not saw_document:
        return message_text

    event.media_urls = kept_paths
    if hasattr(event, "media_types"):
        event.media_types = kept_mimes
    return "\n\n".join(part for part in [result, *blocks] if part).strip()
