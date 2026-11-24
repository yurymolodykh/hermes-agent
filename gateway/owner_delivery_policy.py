"""Owner-facing kanban delivery policy: decisions and final deliverables only.

The owner's Telegram chat carries exactly three kinds of messages: the assistant's
own reply inside a conversation, the finished deliverables of a card, and requests
that need the owner's decision. Everything else the notifier can observe — a card
crashing, timing out, being commented on, moving through review — is machine flow
and stays on the board.

This module is the pure decision layer: no I/O except the best-effort suppression
journal, so the notifier (and its tests) can ask "would this event reach the owner?"
without an adapter, a database or a clock.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

# Deliverable file types the owner accepts in chat. Source, log and diff artifacts stay
# on disk: they are re-readable evidence for the board, not a message.
DEFAULT_ARTIFACT_EXTENSIONS = (
    ".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".svg", ".webp", ".zip", ".csv", ".txt",
)

# Block kinds that are a genuine owner decision: nobody else can move the card.
_DECISION_BLOCK_KINDS = frozenset({"needs_input", "capability"})

_SUPPRESSION_LOG_NAME = "delivery-policy-suppressed.log"
_DELIVERY_LOG_NAME = "delivery-journal.log"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerDeliveryConfig:
    """``kanban.owner_delivery_policy`` — enabled by default, Telegram only."""

    enabled: bool = True
    platforms: tuple[str, ...] = ("telegram",)
    artifact_extensions: tuple[str, ...] = DEFAULT_ARTIFACT_EXTENSIONS

    @classmethod
    def from_config(cls, kanban_cfg: Optional[Mapping[str, Any]]) -> OwnerDeliveryConfig:
        """Read the policy block; anything malformed degrades to the shipped defaults."""
        raw = (kanban_cfg or {}).get("owner_delivery_policy")
        if not isinstance(raw, Mapping):
            return cls()
        enabled = raw.get("enabled", True)
        enabled = bool(enabled) if isinstance(enabled, bool) else True
        platforms = _string_tuple(raw.get("platforms"), cls.platforms)
        extensions = _string_tuple(raw.get("artifact_extensions"), cls.artifact_extensions, normalize_ext=True)
        return cls(enabled=enabled, platforms=platforms, artifact_extensions=extensions)

    def covers(self, platform: str) -> bool:
        """Whether this policy governs ``platform`` (case-insensitive)."""
        return self.enabled and (platform or "").strip().lower() in {
            p.strip().lower() for p in self.platforms if p and p.strip()
        }


def _string_tuple(value: Any, default: Sequence[str], *, normalize_ext: bool = False) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [part for part in value.replace(";", ",").split(",")]
    if not isinstance(value, (list, tuple)):
        return tuple(default)
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        if normalize_ext:
            text = text.lower()
            if not text.startswith("."):
                text = "." + text
        out.append(text)
    return tuple(out) or tuple(default)


def filter_artifacts(paths: Iterable[Any], extensions: Optional[Sequence[str]] = None) -> list[str]:
    """Keep only deliverable paths whose extension is whitelisted; order preserved.

    A DIAGNOSTIC, not a gate: :func:`classify_kanban_event` and the notifier no longer discard a
    card's artifact because of its suffix (a producer's ``.py`` script is a result like a PDF).
    This stays so the caller can name, in the delivery journal, exactly which files the owner's
    extension list would have dropped.
    """
    allowed = {ext.lower() for ext in (extensions if extensions is not None else DEFAULT_ARTIFACT_EXTENSIONS)}
    kept: list[str] = []
    for path in paths or ():
        if not isinstance(path, str) or not path.strip():
            continue
        suffix = Path(path.strip()).suffix.lower()
        if suffix in allowed and path not in kept:
            kept.append(path)
    return kept


def artifact_paths(paths: Iterable[Any]) -> list[str]:
    """Every usable artifact path, unfiltered by extension (order preserved, de-duplicated).

    The board's deliverable rule is about WHO the work belongs to, not about suffixes: a card's
    script, log, JSON or archive is team material exactly as a PDF is.
    """
    out: list[str] = []
    for path in paths or ():
        if not isinstance(path, str) or not path.strip():
            continue
        if path not in out:
            out.append(path)
    return out


def offlist_artifacts(paths: Iterable[Any], extensions: Optional[Sequence[str]] = None) -> list[str]:
    """The paths the owner's extension whitelist would have dropped — journalled, never hidden."""
    raw = artifact_paths(paths)
    kept = set(filter_artifacts(raw, extensions))
    return [path for path in raw if path not in kept]


def _payload_value(payload: Optional[Mapping[str, Any]], *keys: str) -> str:
    for key in keys:
        value = (payload or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _block_kind(payload: Optional[Mapping[str, Any]]) -> str:
    return _payload_value(payload, "kind", "block_kind").lower()


def classify_kanban_event(
    *,
    kind: str,
    payload: Optional[Mapping[str, Any]] = None,
    task: Any = None,
    has_open_ancestor: bool = False,
    artifacts: Optional[Iterable[Any]] = None,
    extensions: Optional[Sequence[str]] = None,
) -> tuple[Optional[str], str]:
    """``(category, reason)`` for one kanban event; category ∈ ``decision|final|None``.

    ``reason`` is the machine-readable why, written to the suppression journal for every
    event that returns ``None`` so nothing disappears silently.
    """
    kind = (kind or "").strip()
    payload = payload or {}
    block_kind = _block_kind(payload)

    if kind == "blocked":
        if block_kind in _DECISION_BLOCK_KINDS:
            return "decision", f"blocked:{block_kind}"
        if not block_kind:
            # Untyped block (legacy generic ``kanban_block()``): nothing auto-continues it.
            # Only ``dependency`` is re-queued by the core (``_route_block`` in
            # hermes_cli/kanban_db.py sends it back to ``todo``); every other block —
            # including one with no kind at all — waits for a human. That is a decision.
            return "decision", "blocked:untyped"
        return None, f"service:blocked:{block_kind}"
    if kind == "block_loop_detected":
        if block_kind == "needs_input":
            return "decision", "block_loop_detected:needs_input"
        return None, "service:block_loop_detected"
    if kind == "status":
        status = _payload_value(payload, "status").lower()
        if status == "blocked" and block_kind in _DECISION_BLOCK_KINDS:
            return "decision", f"status:blocked:{block_kind}"
        return None, f"service:status:{status or 'unknown'}"
    if kind == "gave_up":
        if payload.get("sticky") is True:
            # A sticky give-up is parked on purpose (``bounded_continue`` writes
            # ``sticky`` when it applied its own policy) and ``recompute_ready`` holds it
            # for an operator forever. ``spawn_auto_blocked`` lands here too. Nobody
            # resumes it automatically — the owner has to.
            return "decision", "gave_up:sticky_park"
        return None, "service:gave_up"
    if kind in {"crashed", "timed_out"}:
        return None, f"service:{kind}"
    if kind == "completed":
        if has_open_ancestor:
            return None, "service:intermediate_card"
        raw_artifacts = artifacts
        if raw_artifacts is None:
            # Accept the paths straight off the event payload: that is where the handoff
            # writes them, and a caller that has the payload must not also have to unwrap it.
            raw_artifacts = (payload or {}).get("artifacts") or ()
        # An artifact's SUFFIX is not a delivery gate: the card's script/log/JSON is team
        # material exactly as a PDF is. The owner's extension list no longer decides whether a
        # completion carries a deliverable; it only annotates the journal (:func:`offlist_artifacts`).
        kept = artifact_paths(
            raw_artifacts if isinstance(raw_artifacts, (list, tuple)) else ())
        summary = _payload_value(payload, "summary")
        if not summary and task is not None:
            summary = str(getattr(task, "result", "") or "").strip()
        if kept:
            offlist = offlist_artifacts(raw_artifacts if isinstance(raw_artifacts, (list, tuple)) else (),
                                        extensions)
            reason = f"final:{len(kept)}_artifact(s)"
            if offlist:
                # Not a drop — the file still rides out — but the journal should say that the
                # owner's "deliverables only" list would have hidden it, so a future tightening
                # has the evidence rather than silence.
                reason = f"{reason}:offlist={len(offlist)}"
            return "final", reason
        if summary:
            return "final", "final:summary"
        return None, "service:empty_completed"
    if kind in {"review_requested", "changes_requested", "commented", "archived", "unblocked"}:
        return None, f"service:{kind}"
    return None, f"service:{kind or 'unknown'}"


# --- suppression journal -------------------------------------------------------------


def suppression_log_path() -> Path:
    """``<HERMES_HOME>/logs/delivery-policy-suppressed.log`` (read at call time, profile-aware)."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / _SUPPRESSION_LOG_NAME


def delivery_log_path() -> Path:
    """``<HERMES_HOME>/logs/delivery-journal.log`` — what WAS delivered, and why.

    Separate from the suppression log on purpose: a file that rode out is not a suppressed event,
    and counting it as one would make the "how much did we hide" figure a lie.
    """
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / _DELIVERY_LOG_NAME


def record_delivered(
    *,
    task_id: str,
    event_kind: str,
    platform: str,
    chat_id: str,
    reason: str,
    path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Append one jsonl row to the DELIVERY journal; never raises.

    Same shape as :func:`record_suppressed` so one reader can scan both, and the same rule: a
    journal failure is a logging problem, never a delivery problem.
    """
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())),
        "task_id": task_id,
        "event_kind": event_kind,
        "platform": platform,
        "chat_id": chat_id,
        "reason": reason,
    }
    try:
        target = Path(path) if path is not None else delivery_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        logger.debug("kanban owner policy: cannot append delivery journal: %s", exc)
        return False


def record_suppressed(
    *,
    task_id: str,
    event_kind: str,
    platform: str,
    chat_id: str,
    reason: str,
    path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Append one jsonl row to the suppression journal; never raises.

    Returns whether the row landed — a journal failure is a logging problem and must
    not turn into a delivery problem.
    """
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())),
        "task_id": task_id,
        "event_kind": event_kind,
        "platform": platform,
        "chat_id": chat_id,
        "reason": reason,
    }
    try:
        target = Path(path) if path is not None else suppression_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:  # journal unavailable is a logging problem, not a delivery one
        logger.debug("kanban owner policy: cannot append suppression journal: %s", exc)
        return False
